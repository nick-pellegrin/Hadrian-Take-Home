"""Command-line interface for the warm-up program generator.

The subcommands (``generate``, ``list``, ``show-plan``, ``validate``, ``ui``)
arrive with the modules they drive; until then the CLI reports its version
and usage.
"""

import argparse
from collections.abc import Sequence

from cnc_warmup import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="cnc-warmup",
        description=(
            "Generate CNC machine warm-up programs for Heidenhain TNC 640 (Klartext) "
            "and Fanuc 31i controls."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
