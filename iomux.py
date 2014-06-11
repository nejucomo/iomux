#! /usr/bin/env python

import os
import sys
import time
import errno
import argparse
import unittest
import subprocess
from mock import MagicMock, call, patch


DESCRIPTION = """
Run COMMANDs and annotate each line of their output with fd, timestamp,
and pid to fully disambiguate all output.  The input to the parent iomux
process is sent to the first command.  The iomux parent process waits
for all children to exit.  The exit value is 0 if all children exited
with 0, otherwise it is the first non-zero child's status.
"""

ISO8601 = '%Y-%m-%d %H:%M:%S%z'
SELECT_INTERVAL = 1.3
BUFSIZE = 2**14


def main(args = sys.argv[1:]):
    opts = parse_args(args)
    sys.exit(opts.mainfunc(opts))


# Argument parsing:
def parse_args(args):
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
                optval = run_iomux
            if opts.mainfunc is None:
                optval.optionname = optname
                opts.mainfunc = optval
            else:
                p.error('%r and %r are mutually exclusive.' % (opts.mainfunc.optionname, optname))

    if opts.mainfunc is run_iomux:
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
def run_iomux(opts):
    raise NotImplementedError(`run_iomux`)


class IOManager (object):
    def mainloop(self):
        raise NotImplementedError(`IOManager.mainloop`)


class WriteFileFilter (object):
    def __init__(self, outstream):
        self._f = outstream

    def write(self, data):
        self._f.write(data)

    def flush(self):
        self._f.flush()

    def close(self):
        self._f.close()


class FormatWriter (WriteFileFilter):
    def __init__(self, outstream, template, **paramgens):
        WriteFileFilter.__init__(self, outstream)
        self._tmpl = template
        self._pgens = paramgens

    def write(self, message):
        params = dict( (k, f()) for (k, f) in self._pgens.iteritems() )
        params['message'] = message
        data = self._tmpl.format(**params)
        self._f.write(data + '\n')


class Timestamper (object):
    def __init__(self, format):
        self.format = format

    def __call__(self):
        return time.strftime(self.format, time.gmtime(time.time()))


class LineBuffer (WriteFileFilter):
    def __init__(self, outstream):
        WriteFileFilter.__init__(self, outstream)
        self._buf = ''

    def write(self, data):
        lines = (self._buf + data).split('\n')
        self._buf = lines.pop()

        for line in lines:
            self._f.write(line + '\n')

    def flush(self):
        if self._buf:
            self._f.write(self._buf)
            self._buf = ''
        self._f.flush()


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



class MockingTestCase (unittest.TestCase):
    def _assertCallsEqual(self, mock, calls):
        self.assertEqual(mock.method_calls, calls)


class CommandlineArgumentTests (MockingTestCase):
    def test_parse_unit_test(self):
        opts = parse_args(['--unit-test'])
        self.assertIs(run_unit_tests_with_coverage, opts.mainfunc)

    def test_parse_unit_test_without_coverage(self):
        opts = parse_args(['--unit-test-without-coverage'])
        self.assertIs(run_unit_tests_without_coverage, opts.mainfunc)

    def test_exclusive_unittest_options(self):
        self._assertUsageError('--unit-test', '--unit-test-without-coverage')

    def test_exclusive_test_option_and_command(self):
        self._assertUsageError('--unit-test', 'echo')

    def test_colliding_arguments_in_subcommands(self):
        opts = parse_args(['echo', '--unit-test'])
        self.assertEqual([['echo', '--unit-test']], opts.COMMANDS)

    def test_no_args(self):
        (helpout, helperr) = self._captureStdout('--help')
        (noargsout, noargserr) = self._captureStdout()

        self.assertEqual(helpout.method_calls, noargsout.method_calls)
        self.assertEqual(helperr.method_calls, noargserr.method_calls)

    def test_no_dash_dash_echo(self):
        opts = parse_args(['echo'])
        self.assertIs(run_iomux, opts.mainfunc)
        self.assertEqual([['echo']], opts.COMMANDS)

    def test_multiple_commands(self):
        opts = parse_args(['echo', '-n', 'foo', '--', 'cat', 'bar'])
        self.assertIs(run_iomux, opts.mainfunc)
        self.assertEqual([['echo', '-n', 'foo'], ['cat', 'bar']], opts.COMMANDS)

    def test_leading_dashdash(self):
        opts = parse_args(['--', 'echo', '-n', 'foo', '--', 'cat', 'bar'])
        self.assertIs(run_iomux, opts.mainfunc)
        self.assertEqual([['echo', '-n', 'foo'], ['cat', 'bar']], opts.COMMANDS)

    def test_empty_command_trailing_dashdash(self):
        self._assertUsageError('echo', '-n', 'foo', '--')

    def test_empty_command_double_dashdash(self):
        self._assertUsageError('cat', '--', '--', 'echo')

    def _captureStdout(self, *args):
        with patch('sys.stdout') as out, patch('sys.stderr') as err:
            self.assertRaises(SystemExit, parse_args, args)
            return (out, err)

    def _assertUsageError(self, *args):
        (out, err) = self._captureStdout(*args)
        self._assertCallsEqual(out.write, [])
        self.failUnless(err.write.called)
        return (out, err)


class IOManagerTests (MockingTestCase):
    def setUp(self):
        self.m = IOManager()

    def test_empty_loop(self):
        self.assertEqual(0, self.m.mainloop())

    def test_run_once_timeout(self):
        with patch('select.select') as mockselect:
            rfd = 42
            mockout = MagicMock()
            mockselect.return_value = ([], [], [])

            self.m.add_source(rfd, mockout)
            self.m.run_once()

            self._assertCallsEqual(mockselect, [call([rfd], [], [], SELECT_INTERVAL)])
            self._assertCallsEqual(mockout, [])

    def test_read(self):
        with patch('select.select') as mockselect, patch('os.read') as mockread:
            rfd = 42
            mockout = MagicMock()
            mockselect.return_value = ([rfd], [], [])
            mockread.return_value = 'banana'

            self.m.add_source(rfd, mockout)
            self.m.run_once()

            self._assertCallsEqual(mockselect, [call([rfd], [], [], SELECT_INTERVAL)])
            self._assertCallsEqual(mockread, [call.read(rfd, BUFSIZE)])
            self._assertCallsEqual(mockout, [call.write('banana')])

    def test_read_close(self):
        with patch('select.select') as mockselect, patch('os.read') as mockread:
            rfd = 42
            mockout = MagicMock()
            mockselect.return_value = ([rfd], [], [])
            mockread.return_value = 'banana'

            self.m.add_source(rfd, mockout)
            self.m.run_once()

            self._assertCallsEqual(mockselect, [call([rfd], [], [], SELECT_INTERVAL)])
            self._assertCallsEqual(mockread, [call.read(rfd, BUFSIZE)])
            self._assertCallsEqual(mockout, [call.close()])


class WriteFileFilterTests (MockingTestCase):
    def test_writefilefilter(self):
        mockfile = MagicMock()

        wff = WriteFileFilter(mockfile)
        wff.write('foo')
        wff.write('bar')
        wff.flush()
        wff.write('quz')
        wff.close()

        self._assertCallsEqual(
            mockfile,
            [call.write('foo'),
             call.write('bar'),
             call.flush(),
             call.write('quz'),
             call.close()])


class FormatWriterTests (MockingTestCase):
    def test_format_writer(self):
        def counter():
            counter.c += 1
            return counter.c
        counter.c = 0

        f = MagicMock()

        fw = FormatWriter(f, '{const} {counter} {message}', const=lambda : '<A constant>', counter=counter)
        fw.write('foo')
        fw.write('bar')
        fw.close()

        self._assertCallsEqual(
            f,
            [call.write('<A constant> 1 foo\n'),
             call.write('<A constant> 2 bar\n'),
             call.close()])


class TimestamperTests (unittest.TestCase):
    def test_timestamper(self):
        with patch('time.time') as mocktime:
            mocktime.return_value = 0

            ts = Timestamper(ISO8601)
            for _ in range(2):
                self.assertEqual('1970-01-01 00:00:00+0000', ts())


class LineBufferTests (MockingTestCase):
    def test_flush_buffer(self):
        f = MagicMock()

        lb = LineBuffer(f)
        lb.write('foo')
        lb.write('bar\nquz')
        lb.flush()

        self._assertCallsEqual(
            f,
            [call.write('foobar\n'),
             call.write('quz'),
             call.flush()])

    def test_flush_empty_buffer(self):
        f = MagicMock()

        lb = LineBuffer(f)
        lb.write('foo')
        lb.write('bar\nquz\n')
        lb.flush()

        self._assertCallsEqual(
            f,
            [call.write('foobar\n'),
             call.write('quz\n'),
             call.flush()])



if __name__ == '__main__':
    main()
