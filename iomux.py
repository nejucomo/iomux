#! /usr/bin/env python

import os
import sys
import argparse
import unittest
import tempfile
import subprocess


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

    return p.parse_args(args)


def run_unit_tests_with_coverage(opts):
    prog = os.path.abspath(sys.argv[0])
    covargs = ['coverage', 'run', '--branch', prog, '--unit-test-without-coverage']

    covdir = tempfile.mkdtemp(prefix='coverage.', suffix='.iomux')
    os.chdir(covdir)
    print 'Generating coverage data and report in: %r' % (covdir,)

    print 'Running: %r' % (covargs,)
    status = subprocess.call(covargs)

    subprocess.call(['coverage', 'html', '--dir', '.'])
    return status


def run_unit_tests_without_coverage(opts):
    unittest.main(argv=sys.argv[:1], verbosity=2)



# Unit tests:
class CommandlineArgumentTests (unittest.TestCase):
    def test_parse_unit_test(self):
        opts = parse_args(['--unit-test'])
        self.assertIs(run_unit_tests_with_coverage, opts.mainfunc)

    def test_parse_unit_test_without_coverage(self):
        opts = parse_args(['--unit-test-without-coverage'])
        self.assertIs(run_unit_tests_without_coverage, opts.mainfunc)



if __name__ == '__main__':
    main()
