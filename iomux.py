#! /usr/bin/env python

import os
import sys
import argparse
import unittest
import tempfile
from coverage import coverage


DESCRIPTION = """
Run COMMANDs and annotate each line of their output with fd, timestamp,
and pid to fully disambiguate all output.  The input to the parent iomux
process is sent to the first command.  The iomux parent process waits
for all children to exit.  The exit value is 0 if all children exited
with 0, otherwise it is the first non-zero child's status.
"""


def main(args = sys.argv[1:]):
    opts = parse_args(args)
    if opts.unit_test:
        unittest_main()


def parse_args(args):
    p = argparse.ArgumentParser(
        description=DESCRIPTION,
        formatter_class=argparse.RawTextHelpFormatter)

    p.add_argument('--unit-test',
                   action='store_true',
                   help='Run internal unit tests, then exit.')

    return p.parse_args(args)


def unittest_main():
    covdir = tempfile.mkdtemp(prefix='coverage.', suffix='.iomux')
    covdata = os.path.join(covdir, 'coverage.data')
    print 'Saving unittest coverage data in: %r' % (covdata,)
    c = coverage(branch=True, data_file=covdata)
    c.start()
    try:
        unittest.main(argv=sys.argv[:1], verbosity=2)
    except SystemExit, e:
        c.stop()
        print 'Generating html coverage report in: %r' % (covdir,)
        c.html_report(directory=covdir)
        raise e



# Unit tests:
class CommandlineArgumentTests (unittest.TestCase):
    def test_parse_unit_test(self):
        opts = parse_args(['--unit-test'])
        self.assertIs(True, opts.unit_test)



if __name__ == '__main__':
    main()
