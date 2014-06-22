#! /usr/bin/env python

import os
import sys
import time
import errno
import select
import argparse
import unittest
import subprocess
from pprint import pformat
import mock
from mock import call, sentinel


DESCRIPTION = """
Run COMMANDs and annotate each line of their output with fd, timestamp,
and pid to fully disambiguate all output.  The input to the parent iomux
process is sent to the first command.  The iomux parent process waits
for all children to exit.  The exit value is 0 if all children exited
with 0, otherwise it is the first non-zero child's status.
"""

DEFAULT_TEMPLATE = '{time} {pid} {stream} {message}'
ISO8601 = '%Y-%m-%d %H:%M:%S%z'
SELECT_INTERVAL = 1.3
BUFSIZE = 2**14


def main(args = sys.argv[1:]):
    iomux = IOMux()
    opts = parse_args(iomux, args)
    sys.exit(opts.mainfunc(opts))


# Argument parsing:
def parse_args(iomux, args):
    p = argparse.ArgumentParser(
        description=DESCRIPTION,
        formatter_class=argparse.RawTextHelpFormatter)

    maingroup = p.add_argument_group(title='Command execution')

    maingroup.add_argument('COMMANDS',
                           nargs=argparse.REMAINDER,
                           help="Commands and arguments to run. Separate commands with `--'.")

    testgroup = p.add_argument_group(title='Unit testing')

    testgroup.add_argument('--unit-test',
                           action='store_const',
                           const=run_unit_tests_with_coverage,
                           help='Run internal unit tests with coverage reporting.')

    testgroup.add_argument('--unit-test-without-coverage',
                           action='store_const',
                           const=run_unit_tests_without_coverage,
                           help='Run internal unit tests without coverage analysis.')

    if len(args) == 0:
        args = ['--help']

    opts = p.parse_args(args)
    opts.mainfunc = None

    for optname in ['--unit-test', '--unit-test-without-coverage', 'COMMANDS']:
        dest = optname.lstrip('-').replace('-', '_')
        optval = getattr(opts, dest)
        if optval:
            if optname == 'COMMANDS':
                optval = iomux.run
            if opts.mainfunc is None:
                optval.optionname = optname
                opts.mainfunc = optval
            else:
                p.error('%r and %r are mutually exclusive.' % (opts.mainfunc.optionname, optname))

    if opts.mainfunc is iomux.run:
        # Split subcommands into separate arguments lists, and check
        # for empty commands (which are a usage error).
        cmds = []
        cmd = []

        def push_command():
            if cmd:
                cmds.append(list(cmd))
                cmd[:] = []
            else:
                p.error('Empty COMMANDS are invalid')

        if opts.COMMANDS[:1] == ['--']:
            opts.COMMANDS = opts.COMMANDS[1:]

        for cmdarg in opts.COMMANDS:
            if cmdarg == '--':
                push_command()
            else:
                cmd.append(cmdarg)
        push_command()

        opts.COMMANDS = cmds

    return opts



# Main application:
class ProcessManager (object):
    def __init__(self, iomanager, outwriter, stdinsinkbuffer):
        self._iom = iomanager
        self._out = outwriter
        self._insink = stdinsinkbuffer
        self._procs = {} # { pid -> proc }

    def start_subprocess(self, args):
        p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if self._procs:
            os.close(p.stdin.fileno())
        else:
            self._iom.add_sink(p.stdin.fileno(), self._insink)

        writers = []
        for streamtag in '*OE':
            fmtwriter = MessageFormatter(
                self._out,
                DEFAULT_TEMPLATE,
                time=Timestamper(),
                pid=lambda : p.pid,
                stream=lambda t=streamtag: t)

            linewriter = LineBuffer(fmtwriter)

            writers.append(linewriter)

        [infowriter, outwriter, errwriter] = writers

        self._iom.add_source(p.stdout.fileno(), outwriter)
        self._iom.add_source(p.stderr.fileno(), errwriter)

        infowriter.write_data('Launched: %r\n' % (args,))

        self._procs[p.pid] = p

    def process_events_and_cleanup_processes(self):
        iocont = self._iom.process_events()

        (pid, status) = os.waitpid(-1, os.WNOHANG)
        while (pid, status) != (0, 0):
            # TODO: log exit status!
            del self._procs[pid]

            (pid, status) = os.waitpid(-1, os.WNOHANG)

        # Continue if there're open IO streams *or* running subprocesses:
        return iocont or len(self._procs) > 0


class IOManager (object):
    def __init__(self):
        self._sources = {} # { readadblefd -> writablefile }
        self._sinks = {} # { writablefd -> SinkBuffer }

    def add_source(self, rfd, outfile):
        self._sources[rfd] = outfile

    def add_sink(self, wfd, sinkbuffer):
        self._sinks[wfd] = sinkbuffer

    def process_events(self):
        (rfds, wfds, efds) = select.select(
            self._sources.keys(),
            [ wfd for (wfd, sbuf) in self._sinks.iteritems() if sbuf.pending() ],
            [],
            SELECT_INTERVAL)
        assert efds == [], 'select() postcondition violation: efds == %r' % (efds,)

        for rfd in rfds:
            wfile = self._sources[rfd]
            buf = os.read(rfd, BUFSIZE)
            if buf:
                wfile.write(buf)
            else:
                wfile.close()
                del self._sources[rfd]

        for wfd in wfds:
            sinkbuf = self._sinks[wfd]
            data = sinkbuf.take()
            if data is None:
                os.close(wfd)
                del self._sinks[wfd]
            else:
                written = os.write(wfd, data)
                if written < len(data):
                    sinkbuf.put_back(data[written:])

        return len(self._sources) + len(self._sinks) > 0


class IOMux (object):
    def __init__(self, _IOManager=IOManager, _ProcessManager=ProcessManager):
        self._iom = _IOManager()
        self._iom.add_source(sys.stdin.fileno(), sentinel.stdinSourceHandler)
        self._iom.add_sink(sys.stdout.fileno(), sentinel.stdoutSinkHandler)

        self._pm = _ProcessManager(self._iom, sentinel.stdoutWriter, sentinel.stdinSinkBuffer)

    def run(self, commands):
        assert len(commands) > 0, 'No commands passed: %r' % (commands,)

        for command in commands:
            self._pm.start_subprocess(command)

        while self._pm.process_events_and_cleanup_processes():
            pass


class InputSplitter (object):
    def __init__(self, sinkbuffer, loglinebuffer):
        self._sbuf = sinkbuffer
        self._logbuf = loglinebuffer

    def write_data(self, data):
        self._sbuf.enqueue_data(data)
        self._logbuf.write_data(data)

    def finish(self):
        self._logbuf.finish()


class LineBuffer (object):
    """I enqueue data, then pass single lines to my downstream MessageFormatter."""
    def __init__(self, messageformatter):
        self._mf = messageformatter
        self._buf = ''

    def write_data(self, data):
        lines = (self._buf + data).split('\n')
        self._buf = lines.pop()

        for line in lines:
            # Note: messages do not contain '\n':
            self._mf.write_message(line)

    def finish(self):
        if self._buf:
            # BUG: There's no way for the iomux output reader to
            # distinguish between a final newline or a lack of one:
            self._mf.write_message(self._buf)
            self._buf = None


class MessageFormatter (object):
    """I format messages and pass the result to a sinkbuffer, according to param generaters."""
    def __init__(self, sinkbuffer, template, **paramgens):
        self._sbuf = sinkbuffer
        self._tmpl = template
        self._pgens = paramgens

    def write_message(self, message):
        params = dict( (k, f()) for (k, f) in self._pgens.iteritems() )
        params['message'] = message
        data = self._tmpl.format(**params)
        self._sbuf.enqueue_data(data + '\n')


class SinkBuffer (object):
    def __init__(self):
        self._buf = []

    # Consumer interface (for data going out of this process):
    def pending(self):
        return len(self._buf) > 0

    def take(self):
        assert self.pending(), 'Precondition violation: SinkBuffer.take() on empty buffer.'
        return self._buf.pop(0)

    def put_back(self, data):
        return self._buf.insert(0, data)

    # Producer interface (for data coming into this process):
    def enqueue_data(self, data):
        self._buf.append(data)


class Timestamper (object):
    def __init__(self, format=ISO8601):
        self.format = format

    def __call__(self):
        return time.strftime(self.format, time.gmtime(time.time()))


# Unit tests:
def run_unit_tests_with_coverage(opts):
    prog = os.path.abspath(sys.argv[0])
    covargs = ['coverage', 'run', '--branch', prog, '--unit-test-without-coverage']

    covdir = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'iomux.coverage')
    try:
        os.mkdir(covdir)
    except os.error, e:
        if e.errno != errno.EEXIST:
            raise

    os.chdir(covdir)
    print 'Generating coverage data and report in: %r' % (covdir,)

    print 'Running: %r' % (covargs,)
    status = subprocess.call(covargs)

    subprocess.call(['coverage', 'html', '--dir', '.'])
    return status


def run_unit_tests_without_coverage(opts):
    unittest.main(argv=sys.argv[:1], verbosity=2)


# Unit test helper code:
class EQCB (object):
    """Create an object where obj.__eq__(other) is defined as eqcb(other)."""
    def __init__(self, eqcb, repstr):
        self._eqcb = eqcb
        self._repstr = repstr

    def __eq__(self, other):
        return self._eqcb(other)

    def __repr__(self):
        return self._repstr

# An EQCB which is true whenever the comparee is of a given type:
ArgIsInstance = lambda T: EQCB(lambda x: isinstance(x, T), 'ArgIsInstance(%s)' % (T.__name__,))


class MockingTestCase (unittest.TestCase):
    def setUp(self):
        self._patchers = []

    def tearDown(self):
        for (p, _) in self._patchers:
            if p is not None:
                p.stop()

    def _patch(self, name):
        p = mock.patch(name)
        mockobj = p.start()
        self._patchers.append( (p, mockobj) )
        return mockobj

    def _make_mock(self):
        mockobj = mock.MagicMock()
        self._patchers.append( (None, mockobj) )
        return mockobj

    def _reset_mocks(self):
        for (_, m) in self._patchers:
            m.reset_mock()

    def _assertCallsEqual(self, mockobj, expectedcalls):
        mockcalls = mockobj._mock_mock_calls
        self.assertEqual(
            len(mockcalls), len(expectedcalls),
            'len(%s) == %r != len(%s) == %r' % (
                pformat(mockcalls), len(mockcalls),
                pformat(expectedcalls), len(expectedcalls)))

        for i, (mockcall, expectedcall) in enumerate(zip(mockcalls, expectedcalls)):
            self.assertEqual(
                mockcall, expectedcall,
                'Arg %d:\n%s\n  !=\n%s' % (i, pformat(mockcall), pformat(expectedcall)))


class CommandlineArgumentTests (MockingTestCase):
    def setUp(self):
        MockingTestCase.setUp(self)

        self.m_iomux = self._make_mock()
        self.m_iomux.run = sentinel.IOMux_run

    def _parse_args(self, args):
        result = parse_args(self.m_iomux, args)
        self._assertCallsEqual(self.m_iomux, [])
        return result

    def _capture_stdout_usage_info(self, args):
        assert type(args) is list, `args`
        out = self._patch('sys.stdout')
        err = self._patch('sys.stderr')
        self.assertRaises(SystemExit, self._parse_args, args)
        return (out, err)

    def _assert_usage_error(self, args):
        (out, err) = self._capture_stdout_usage_info(args)
        self._assertCallsEqual(out.write, [])
        self.failUnless(err.write.called)
        return (out, err)

    def test_parse_unit_test(self):
        opts = self._parse_args(['--unit-test'])
        self.assertIs(run_unit_tests_with_coverage, opts.mainfunc)

    def test_parse_unit_test_without_coverage(self):
        opts = self._parse_args(['--unit-test-without-coverage'])
        self.assertIs(run_unit_tests_without_coverage, opts.mainfunc)

    def test_exclusive_unittest_options(self):
        self._assert_usage_error(['--unit-test', '--unit-test-without-coverage'])

    def test_exclusive_test_option_and_command(self):
        self._assert_usage_error(['--unit-test', 'echo'])

    def test_colliding_arguments_in_subcommands(self):
        opts = self._parse_args(['echo', '--unit-test'])
        self.assertEqual([['echo', '--unit-test']], opts.COMMANDS)

    def test_no_args(self):
        (helpout, helperr) = self._capture_stdout_usage_info(['--help'])
        (noargsout, noargserr) = self._capture_stdout_usage_info([])

        self.assertEqual(helpout.method_calls, noargsout.method_calls)
        self.assertEqual(helperr.method_calls, noargserr.method_calls)

    def test_no_dash_dash_echo(self):
        opts = self._parse_args(['echo'])
        self.assertIs(self.m_iomux.run, opts.mainfunc)
        self.assertEqual([['echo']], opts.COMMANDS)

    def test_multiple_commands(self):
        opts = self._parse_args(['echo', '-n', 'foo', '--', 'cat', 'bar'])
        self.assertIs(self.m_iomux.run, opts.mainfunc)
        self.assertEqual([['echo', '-n', 'foo'], ['cat', 'bar']], opts.COMMANDS)

    def test_leading_dashdash(self):
        opts = self._parse_args(['--', 'echo', '-n', 'foo', '--', 'cat', 'bar'])
        self.assertIs(self.m_iomux.run, opts.mainfunc)
        self.assertEqual([['echo', '-n', 'foo'], ['cat', 'bar']], opts.COMMANDS)

    def test_empty_command_trailing_dashdash(self):
        self._assert_usage_error(['echo', '-n', 'foo', '--'])

    def test_empty_command_double_dashdash(self):
        self._assert_usage_error(['cat', '--', '--', 'echo'])


class IOManagerTests (MockingTestCase):
    def setUp(self):
        MockingTestCase.setUp(self)

        self.m = IOManager()

    def test_process_events_timeout(self):
        m_select = self._patch('select.select')
        rfd = 42
        m_out = self._make_mock()
        m_select.return_value = ([], [], [])

        self.m.add_source(rfd, m_out)
        cont = self.m.process_events()

        self._assertCallsEqual(m_select, [call([rfd], [], [], SELECT_INTERVAL)])
        self._assertCallsEqual(m_out, [])
        self.assertEqual(True, cont)

    def test_read(self):
        m_select = self._patch('select.select')
        m_read = self._patch('os.read')

        rfd = 42
        m_out = self._make_mock()
        m_select.return_value = ([rfd], [], [])
        m_read.return_value = 'banana'

        self.m.add_source(rfd, m_out)
        cont = self.m.process_events()

        self._assertCallsEqual(m_select, [call([rfd], [], [], SELECT_INTERVAL)])
        self._assertCallsEqual(m_read, [call.read(rfd, BUFSIZE)])
        self._assertCallsEqual(m_out, [call.write('banana')])
        self.assertEqual(True, cont)

    def test_read_close(self):
        m_select = self._patch('select.select')
        m_read = self._patch('os.read')

        rfd = 42
        m_out = self._make_mock()
        m_select.return_value = ([rfd], [], [])
        m_read.return_value = ''

        self.m.add_source(rfd, m_out)
        cont = self.m.process_events()

        self._assertCallsEqual(m_select, [call([rfd], [], [], SELECT_INTERVAL)])
        self._assertCallsEqual(m_read, [call.read(rfd, BUFSIZE)])
        self._assertCallsEqual(m_out, [call.close()])
        self.assertEqual(False, cont)

    def test_write_complete(self):
        m_select = self._patch('select.select')
        m_write = self._patch('os.write')

        wfd = 42
        m_sinkbuffer = self._make_mock()
        m_sinkbuffer.pending.return_value = True
        m_sinkbuffer.take.return_value = 'foobar'
        m_write.return_value = 6
        m_select.return_value = ([], [wfd], [])

        self.m.add_sink(wfd, m_sinkbuffer)
        cont = self.m.process_events()

        self._assertCallsEqual(m_select, [call([], [wfd], [], SELECT_INTERVAL)])
        self._assertCallsEqual(
            m_sinkbuffer,
            [call.pending(),
             call.take()])
        self._assertCallsEqual(m_write, [call(wfd, 'foobar')])
        self.assertEqual(True, cont)

    def test_write_partial(self):
        m_select = self._patch('select.select')
        m_write = self._patch('os.write')

        wfd = 42
        m_sinkbuffer = self._make_mock()
        m_sinkbuffer.pending.return_value = True
        m_sinkbuffer.take.return_value = 'foobar'
        m_write.return_value = 3
        m_select.return_value = ([], [wfd], [])

        self.m.add_sink(wfd, m_sinkbuffer)
        cont = self.m.process_events()

        self._assertCallsEqual(m_select, [call([], [wfd], [], SELECT_INTERVAL)])
        self._assertCallsEqual(
            m_sinkbuffer,
            [call.pending(),
             call.take(),
             call.put_back('bar')])
        self._assertCallsEqual(m_write, [call(wfd, 'foobar')])
        self.assertEqual(True, cont)

    def test_write_close(self):
        m_select = self._patch('select.select')
        m_close = self._patch('os.close')

        wfd = 42
        m_sinkbuffer = self._make_mock()
        m_sinkbuffer.pending.return_value = True
        m_sinkbuffer.take.return_value = None
        m_select.return_value = ([], [wfd], [])

        self.m.add_sink(wfd, m_sinkbuffer)
        cont = self.m.process_events()

        self._assertCallsEqual(m_select, [call([], [wfd], [], SELECT_INTERVAL)])
        self._assertCallsEqual(
            m_sinkbuffer,
            [call.pending(),
             call.take()])
        self._assertCallsEqual(m_close, [call(wfd)])
        self.assertEqual(False, cont)


class ProcessManagerTests (MockingTestCase):
    def setUp(self):
        MockingTestCase.setUp(self)

        # Deterministic time sequence:
        def timectr():
            i = 0
            while True:
                yield i
                i += 1

        self._patch('time.time').side_effect = timectr()

        self.m_iom = self._make_mock()
        self.m_stdoutwriter = self._make_mock()
        self.m_stdinsink = self._make_mock()
        self.pm = ProcessManager(self.m_iom, self.m_stdoutwriter, self.m_stdinsink)

        self.argv1 = ['echo', 'hello', 'world']
        self.argv2 = ['date']

        m_proc1 = self._make_mock()
        m_proc1.pid = 1001
        m_proc1.stdin.fileno.return_value = sentinel.proc1_stdin
        m_proc1.stdout.fileno.return_value = sentinel.proc1_stdout
        m_proc1.stderr.fileno.return_value = sentinel.proc1_stderr
        self.m_proc1 = m_proc1

        m_proc2 = self._make_mock()
        m_proc2.pid = 1002
        m_proc2.stdin.fileno.return_value = sentinel.proc2_stdin
        m_proc2.stdout.fileno.return_value = sentinel.proc2_stdout
        m_proc2.stderr.fileno.return_value = sentinel.proc2_stderr
        self.m_proc2 = m_proc2

    def _subtest_start_subprocess_twice(self):
        m_Popen = self._patch('subprocess.Popen')
        m_close = self._patch('os.close')

        m_Popen.side_effect = [self.m_proc1, self.m_proc2]

        self.pm.start_subprocess(self.argv1)
        self.pm.start_subprocess(self.argv2)

        self._assertCallsEqual(
            m_Popen,
            [call(self.argv1, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE),
             call(self.argv2, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)])

        self._assertCallsEqual(
            self.m_proc1,
            [call.stdin.fileno(),
             call.stdout.fileno(),
             call.stderr.fileno()])

        self._assertCallsEqual(
            self.m_proc2,
            [call.stdin.fileno(),
             call.stdout.fileno(),
             call.stderr.fileno()])

        self._assertCallsEqual(
            m_close,
            [call(sentinel.proc2_stdin)])

        self._assertCallsEqual(
            self.m_iom,
            [call.add_sink(sentinel.proc1_stdin, self.m_stdinsink),
             call.add_source(sentinel.proc1_stdout, ArgIsInstance(LineBuffer)),
             call.add_source(sentinel.proc1_stderr, ArgIsInstance(LineBuffer)),
             call.add_source(sentinel.proc2_stdout, ArgIsInstance(LineBuffer)),
             call.add_source(sentinel.proc2_stderr, ArgIsInstance(LineBuffer))])

        self._assertCallsEqual(
            self.m_stdoutwriter,
            [call.write("1970-01-01 00:00:00+0000 1001 * Launching ['echo', 'hello', 'world']\n"),
             call.write("1970-01-01 00:00:01+0000 1002 * Launching ['date']\n")])

    def test_start_subprocess(self):
        self._subtest_start_subprocess_twice()

    def test_process_events_and_cleanup_processes_timeout_and_success_exits_and_io_completes_first(self):
        self._subtest_start_subprocess_twice()

        self.m_iom.process_events.side_effect = [True, False, False]

        m_waitpid = self._patch('os.waitpid')

        m_waitpid.side_effect = [
            # Loop 1:
            (0, 0),
            # Loop 2:
            (self.m_proc2.pid, 0),
            (0, 0),
            # Loop 3:
            (self.m_proc1.pid, 0),
            (0, 0),
            ]

        self._reset_mocks()
        self.assertEqual(True, self.pm.process_events_and_cleanup_processes())
        self._assertCallsEqual(self.m_iom, [call.process_events()])
        self._assertCallsEqual(
            m_waitpid,
            [call(-1, os.WNOHANG)])

        self._reset_mocks()
        self.assertEqual(True, self.pm.process_events_and_cleanup_processes())
        self._assertCallsEqual(self.m_iom, [call.process_events()])
        self._assertCallsEqual(
            m_waitpid,
            [call(-1, os.WNOHANG),
             call(-1, os.WNOHANG)])

        self._reset_mocks()
        self.assertEqual(False, self.pm.process_events_and_cleanup_processes())
        self._assertCallsEqual(self.m_iom, [call.process_events()])
        self._assertCallsEqual(
            m_waitpid,
            [call(-1, os.WNOHANG),
             call(-1, os.WNOHANG)])

    def test_process_events_and_cleanup_processes_timeout_and_success_exits_and_io_completes_last(self):
        self._subtest_start_subprocess_twice()

        self.m_iom.process_events.side_effect = [True, True, False]

        m_waitpid = self._patch('os.waitpid')

        m_waitpid.side_effect = [
            # Loop 1:
            (0, 0),
            # Loop 2:
            (self.m_proc2.pid, 0),
            (self.m_proc1.pid, 0),
            (0, 0),
            # Loop 3:
            (0, 0),
            ]

        self._reset_mocks()
        self.assertEqual(True, self.pm.process_events_and_cleanup_processes())
        self._assertCallsEqual(self.m_iom, [call.process_events()])
        self._assertCallsEqual(m_waitpid, [call(-1, os.WNOHANG)])

        self._reset_mocks()
        self.assertEqual(True, self.pm.process_events_and_cleanup_processes())
        self._assertCallsEqual(self.m_iom, [call.process_events()])
        self._assertCallsEqual(
            m_waitpid,
            [call(-1, os.WNOHANG),
             call(-1, os.WNOHANG),
             call(-1, os.WNOHANG)])

        self._reset_mocks()
        self.assertEqual(False, self.pm.process_events_and_cleanup_processes())
        self._assertCallsEqual(self.m_iom, [call.process_events()])
        self._assertCallsEqual(m_waitpid, [call(-1, os.WNOHANG)])


class IOMuxTests (MockingTestCase):
    def setUp(self):
        MockingTestCase.setUp(self)

        self.m_IOManager = self._make_mock()
        self.m_ProcessManager = self._make_mock()
        self.m_ProcessManager.return_value.process_events_and_cleanup_processes.side_effect = [False]
        self.iomux = IOMux(self.m_IOManager, self.m_ProcessManager)

    def test___init__behavior(self):
        self._assertCallsEqual(
            self.m_IOManager,
            [call(),
             call().add_source(sys.stdin.fileno(), ArgIsInstance(LineBuffer)),
             call().add_sink(sys.stdout.fileno(), ArgIsInstance(SinkBuffer))])

        self._assertCallsEqual(
            self.m_ProcessManager,
            [call(self.m_IOManager.return_value)])

    def test_run_no_commands(self):
        self._reset_mocks()

        self.assertRaises(AssertionError, self.iomux.run, [])

        self._assertCallsEqual(
            self.m_IOManager,
            [])

        self._assertCallsEqual(
            self.m_ProcessManager,
            [])


    def test_run_multiple_commands(self):
        self._reset_mocks()

        self.m_ProcessManager.return_value.process_events_and_cleanup_processes.side_effect = [
            True, True, False]

        argv0 = ['cat', '/etc/motd']
        argv1 = ['expr', '2', '+', '3']

        self.iomux.run([argv0, argv1])

        self._assertCallsEqual(
            self.m_ProcessManager,
            [call().start_subprocess(argv0),
             call().start_subprocess(argv1),
             call().process_events_and_cleanup_processes(),
             call().process_events_and_cleanup_processes(),
             call().process_events_and_cleanup_processes()])

        self._assertCallsEqual(
            self.m_IOManager,
            [])


class InputSplitterTests (MockingTestCase):
    def test_input_splitter(self):
        m_sbuf = self._make_mock()
        m_lbuf = self._make_mock()

        splitter = InputSplitter(m_sbuf, m_lbuf)

        splitter.write_data('foo')

        self._assertCallsEqual(
            m_sbuf,
            [call.enqueue_data('foo')])

        self._assertCallsEqual(
            m_lbuf,
            [call.write_data('foo')])

        self._reset_mocks()

        splitter.finish()

        self._assertCallsEqual(
            m_sbuf,
            [])

        self._assertCallsEqual(
            m_lbuf,
            [call.finish()])


class LineBufferTests (MockingTestCase):
    def test_flush_buffer(self):
        m_formatter = self._make_mock()

        lb = LineBuffer(m_formatter)
        lb.write_data('foo')
        self._assertCallsEqual(
            m_formatter,
            [])

        lb.write_data('bar\nquz')
        self._assertCallsEqual(
            m_formatter,
            [call.write_message('foobar')])

        lb.finish()
        self._assertCallsEqual(
            m_formatter,
            [call.write_message('foobar'),
             call.write_message('quz')])

    def test_finish_empty_buffer(self):
        m_formatter = self._make_mock()

        lb = LineBuffer(m_formatter)
        lb.write_data('foo')
        self._assertCallsEqual(
            m_formatter,
            [])

        lb.write_data('bar\nquz\n')
        self._assertCallsEqual(
            m_formatter,
            [call.write_message('foobar'),
             call.write_message('quz')])

        lb.finish()
        self._assertCallsEqual(
            m_formatter,
            [call.write_message('foobar'),
             call.write_message('quz')])


class MessageFormatterTests (MockingTestCase):
    def test_format_writer(self):
        def counter():
            counter.c += 1
            return counter.c
        counter.c = 0

        m_sinkbuf = self._make_mock()

        fw = MessageFormatter(
            m_sinkbuf,
            '{const} {counter} {message}',
            const=lambda : '<A constant>',
            counter=counter)

        fw.write_message('foo')
        fw.write_message('bar')

        self._assertCallsEqual(
            m_sinkbuf,
            [call.enqueue_data('<A constant> 1 foo\n'),
             call.enqueue_data('<A constant> 2 bar\n')])


class SinkBufferTests (MockingTestCase):
    def test_sink_buffer(self):
        sb = SinkBuffer()

        self.assertEqual(False, sb.pending())

        sb.enqueue_data('foo')
        self.assertEqual(True, sb.pending())

        sb.enqueue_data('bar\n')
        self.assertEqual(True, sb.pending())

        self.assertEqual('foo', sb.take())
        self.assertEqual(True, sb.pending())

        self.assertEqual('bar\n', sb.take())
        self.assertEqual(False, sb.pending())

        sb.put_back('ar\n')
        self.assertEqual(True, sb.pending())

        sb.enqueue_data('quz')
        sb.enqueue_data('wux')

        self.assertEqual('ar\n', sb.take())
        self.assertEqual(True, sb.pending())

        self.assertEqual('quz', sb.take())
        self.assertEqual(True, sb.pending())

        self.assertEqual('wux', sb.take())
        self.assertEqual(False, sb.pending())

        # Invariant violation:
        self.assertRaises(AssertionError, sb.take)


class TimestamperTests (MockingTestCase):
    def test_timestamper(self):
        m_time = self._patch('time.time')
        m_time.return_value = 0

        ts = Timestamper(ISO8601)
        for _ in range(2):
            self.assertEqual('1970-01-01 00:00:00+0000', ts())



if __name__ == '__main__':
    main()
