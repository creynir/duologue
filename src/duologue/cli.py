"""The ``duologue`` command line."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from duologue import __version__

DESCRIPTION = "One conversation, two coding agents. Switch sides whenever you like."

# The planned commands. Each one gets its implementation as the port lands.
COMMANDS = {
    "link": "pair a Claude Code session with a Codex thread",
    "unlink": "stop syncing a pair",
    "status": "show every pair and where its sync stands",
    "sync": "run one sync pass now",
    "rebuild": "rebuild one side's history from every source",
    "doctor": "check versions, format gates and quarantined entries",
    "hold": "pause syncing a pair",
    "release": "resume syncing a held pair",
    "log": "show recent sync activity",
}

NOT_IMPLEMENTED = 3  # distinct from argparse's usage error (2) and a general failure (1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="duologue", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    for name, summary in COMMANDS.items():
        commands.add_parser(name, help=summary, description=summary)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    print(f"duologue {args.command}: not implemented yet", file=sys.stderr)
    return NOT_IMPLEMENTED
