#! /usr/bin/env python

import os
import sys
import time
import errno
import argparse
import unittest
import subprocess
from cStringIO import StringIO


DESCRIPTION = """
Run COMMANDs and annotate each line of their output with fd, timestamp,
and pid to fully disambiguate all output.  The input to the parent iomux
process is sent to the first command.  The iomux parent process waits
for all children to exit.  The exit value is 0 if all children exited
with 0, otherwise it is the first non-zero child's status.
"""

ISO8601 = '%Y-%m-%d %H:%M:%S%z'


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


class FormatWriter (object):
    def __init__(self, outstream, template, **paramgens):
        self._f = outstream
        self._tmpl = template
        self._pgens = paramgens

    def write(self, message):
        params = dict( (k, f()) for (k, f) in self._pgens.iteritems() )
        params['message'] = message
        data = self._tmpl.format(**params)
        self._f.write(data + '\n')

    def close(self):
        self._f.close()


class Timestamper (object):
    def __init__(self, format, _time=time.time):
        self.format = format
        self.time = _time

    def __call__(self):
        return time.strftime(self.format, time.gmtime(self.time()))



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



class CommandlineArgumentTests (unittest.TestCase):
    def test_parse_unit_test(self):
        opts = parse_args(['--unit-test'])
        self.assertIs(run_unit_tests_with_coverage, opts.mainfunc)

    def test_parse_unit_test_without_coverage(self):
        opts = parse_args(['--unit-test-without-coverage'])
        self.assertIs(run_unit_tests_without_coverage, opts.mainfunc)

    def test_exclusive_unittest_options(self):
        with StdoutCapture():
            self.assertRaises(SystemExit, parse_args, ['--unit-test', '--unit-test-without-coverage'])

    def test_exclusive_test_option_and_command(self):
        with StdoutCapture():
            self.assertRaises(SystemExit, parse_args, ['--unit-test', 'echo'])

    def test_colliding_arguments_in_subcommands(self):
        opts = parse_args(['echo', '--unit-test'])
        self.assertEqual([['echo', '--unit-test']], opts.COMMANDS)

    def test_no_args(self):
        with StdoutCapture() as helpcap:
            self.assertRaises(SystemExit, parse_args, ['--help'])

        with StdoutCapture() as noargscap:
            self.assertRaises(SystemExit, parse_args, [])

        self.assertEqual(helpcap.get_outputs(), noargscap.get_outputs())

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
        with StdoutCapture():
            self.assertRaises(SystemExit, parse_args, ['echo', '-n', 'foo', '--'])

    def test_empty_command_double_dashdash(self):
        with StdoutCapture():
            self.assertRaises(SystemExit, parse_args, ['cat', '--', '--', 'echo'])


class FormatWriterTests (unittest.TestCase):
    def test_format_writer(self):
        f = StringIO()

        def counter():
            counter.c += 1
            return counter.c
        counter.c = 0

        fw = FormatWriter(f, '{const} {counter} {message}', const=lambda : '<A constant>', counter=counter)
        fw.write('foo')
        fw.write('bar')

        self.assertEqual('<A constant> 1 foo\n<A constant> 2 bar\n', f.getvalue())

        fw.close()

        self.assertRaises(ValueError, f.getvalue) # operation on closed file.


class TimestamperTests (unittest.TestCase):
    def test_timestamper(self):
        ts = Timestamper(ISO8601, _time=lambda : 0)
        for _ in range(2):
            self.assertEqual('1970-01-01 00:00:00+0000', ts())


class StdoutCapture (object):
    def __enter__(self):
        self.realout = sys.stdout
        self.realerr = sys.stderr
        sys.stdout = self.capout = StringIO()
        sys.stderr = self.caperr = StringIO()
        return self

    def __exit__(self, *args):
        sys.stdout = self.realout
        sys.stderr = self.realerr

    def get_outputs(self):
        return (self.capout.getvalue(), self.caperr.getvalue())



if __name__ == '__main__':
    main()
