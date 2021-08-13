"""
============
gprof_nn.bin
============

This sub-module implements the top-level 'gprof_nn' command line applitcation.
The top-level implementation delegates the processing to the sub-commands
that are defined in the sub-module of the 'gprof_nn.bin' module.
"""
import argparse
import sys

def gprof_nn():
    """
    This function implements the top-level command line interface for the
    'gprof_nn' package. It serves as the global entry point to execute
    any of the available sub-commands.
    """
    from gprof_nn.bin import extract_data

    description = ("Neural-network based implementation of the Goddard "
                   "PROFiling algorithm (GPROF)")

    parser = argparse.ArgumentParser(
            prog='gprof_nn',
            description=description
            )

    subparsers = parser.add_subparsers(help='Sub-commands')

    parser_ret = subparsers.add_parser(
            'retrieve',
            help='Apply GPROF-NN to observations.')

    extract_data.add_parser(subparsers)

    if len(sys.argv)==1:
        parser.print_help(sys.stderr)
        return 1

    args = parser.parse_args()
    args.func(args)


