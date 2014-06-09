#! /usr/bin/env python

import os
import errno
import sys
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


def main(args = sys.argv[1:]):
    opts = parse_args(args)
    sys.exit(opts.mainfunc(opts))


def parse_args(args):
    p = argparse.ArgumentParser(
        description=DESCRIPTION,
        formatter_class=argparse.RawTextHelpFormatter)

    group = p.add_mutually_exclusive_group(required=True)

    group.add_argument('--unit-test',
                       dest='mainfunc',
                       action='store_const',
                       const=run_unit_tests_with_coverage,
                       help='Run internal unit tests with coverage reporting.')

    group.add_argument('--unit-test-without-coverage',
                       dest='mainfunc',
                       action='store_const',
                       const=run_unit_tests_without_coverage,
                       help='Run internal unit tests without coverage analysis.')

    if len(args) == 0:
        args = ['--help']

    return p.parse_args(args)


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

    def test_exclusive_options(self):
        with StdoutCapture():
            self.assertRaises(SystemExit, parse_args, ['--unit-test', '--unit-test-without-coverage'])

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
