"""The ``duologue`` command line."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
from collections.abc import Iterator, Sequence

from duologue import __version__, config, gate, linking
from duologue.codex.rpc import daemon_running
from duologue.engine import TRANSIENT, Engine, _printable
from duologue.journal import Blocked, Wait
from duologue.linking import LinkError
from duologue.pairs import ActivityLog, Busy, Store, exclusive
from duologue.service import Service

DESCRIPTION = "One conversation, two coding agents. Switch sides whenever you like."

COMMANDS = {
    "link": "pair a Claude Code session with a Codex thread (or create the missing one)",
    "unlink": "stop syncing a pair; both conversations stay as they are",
    "status": "show every pair and where its sync stands",
    "sync": "run one sync pass now",
    "doctor": "check the apps, their versions and duologue's own state",
    "hold": "pause syncing a pair",
    "release": "resume a held pair, or one that stopped and needed a person",
    "resolve": "after you wrote in both apps, keep one side's new turns",
    "log": "show recent sync activity",
    "service": "run sync in the background every 30 seconds (launchd or systemd)",
}
FAILED = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="duologue", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def command(name: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=COMMANDS[name], description=COMMANDS[name])

    p = command("link")
    p.add_argument("--claude", metavar="SESSION", help="a Claude Code session id, or 'last'")
    p.add_argument("--codex", metavar="THREAD", help="a Codex thread id, or 'last'")
    p.add_argument("--name", help="what to call the pair (default: the conversation's title)")
    p = command("unlink")
    p.add_argument("pair", help="the pair's name, the start of it, or either side's id")
    p.add_argument("--force", action="store_true", help="even with a write not confirmed yet")
    p = command("status")
    p.add_argument("--json", action="store_true", help="every pair's state as JSON")
    p = command("sync")
    p.add_argument("pairs", nargs="*", metavar="pair", help="only these pairs (default: all)")
    p.add_argument("--force", action="store_true", help="check sides that look unchanged too")
    p.add_argument("--quiet", action="store_true", help="print only what changed")
    command("doctor")
    p = command("hold")
    p.add_argument("pair")
    p.add_argument("--reason", default="held by you", help="shown in duologue status")
    p = command("release")
    p.add_argument("pair")
    p = command("resolve")
    p.add_argument("pair")
    p.add_argument("--keep", required=True, choices=("claude", "codex"))
    p = command("log")
    p.add_argument("pair", nargs="?")
    p.add_argument("-n", type=int, default=20, help="how many entries (default 20)")
    p = command("service")
    p.add_argument("action", choices=("install", "uninstall", "status"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return HANDLERS[args.command](args)
    except Wait as e:
        print(f"duologue {args.command}: not now: {e}", file=sys.stderr)
        return FAILED
    except (LinkError, config.ConfigError, Busy, Blocked, ValueError, RuntimeError) as e:
        print(f"duologue {args.command}: {e}", file=sys.stderr)
        return FAILED
    except TRANSIENT as e:
        print(f"duologue {args.command}: {type(e).__name__}: {e}", file=sys.stderr)
        return FAILED


# ---------------------------------------------------------------------------------- handlers
@contextlib.contextmanager
def engine(lock: bool = True) -> Iterator[Engine]:
    cfg = config.load()
    guard = exclusive(cfg.lock_file) if lock else contextlib.nullcontext()
    with guard:
        store = Store(cfg.state_file)
        yield Engine(cfg, store.load(), store, ActivityLog(cfg.log_file))


def cmd_link(args) -> int:
    with engine() as e:
        pair, done = linking.link(
            e, claude=args.claude, codex=args.codex, name=args.name, cwd=os.getcwd()
        )
    print(f'"{pair.name}": {done}')
    print(f"  Claude session {pair.claude}\n  Codex thread   {pair.codex}")
    return 0


def cmd_unlink(args) -> int:
    with engine() as e:
        pair = linking.find_pair(e, args.pair)
        print(f'"{pair.name}": {linking.unlink(e, pair, args.force)}')
    return 0


def cmd_status(args) -> int:
    with engine(lock=False) as e:
        pairs = list(e.pairs.values())
    if args.json:
        print(json.dumps([p.to_dict() for p in pairs], indent=1, ensure_ascii=False))
        return 0
    if not pairs:
        print("no pairs yet; link one with: duologue link --claude last --codex last")
        return 0
    for p in pairs:
        print(f'"{p.name}"  {p.state}')
        print(f"  Claude {p.claude}   Codex {p.codex}")
        print(f"  last sync {p.last_sync or 'never'}; {p.status or 'not synced yet'}")
        if p.pending:
            print(f"  a write to {p.pending.side} is not confirmed yet")
    return 0


def cmd_sync(args) -> int:
    failed = False
    with engine() as e:
        chosen = [linking.find_pair(e, ref) for ref in args.pairs] or list(e.pairs.values())
        for pair in chosen:
            outcome = e.sync(pair, force=args.force or bool(args.pairs))
            failed |= outcome.state in ("blocked", "conflict", "error")
            if not args.quiet or outcome.state not in ("idle", "waiting", "held"):
                print(f'"{pair.name}": {outcome.state}: {outcome.text}')
    return FAILED if failed else 0


def cmd_hold(args) -> int:
    with engine() as e:
        pair = linking.find_pair(e, args.pair)
        pair.hold = args.reason
        e.save()
        e.log.write(pair.name, "hold", args.reason)
    print(f'"{pair.name}": on hold; duologue release "{pair.name}" resumes it')
    return 0


def cmd_release(args) -> int:
    with engine() as e:
        pair = linking.find_pair(e, args.pair)
        print(f'"{pair.name}": {linking.release(e, pair)}')
    return 0


def cmd_resolve(args) -> int:
    with engine() as e:
        pair = linking.find_pair(e, args.pair)
        print(f'"{pair.name}": {linking.resolve(e, pair, args.keep)}')
    return 0


def cmd_log(args) -> int:
    name = args.pair
    if name is not None:
        with engine(lock=False) as e:  # a start of the name or an id, as elsewhere
            try:
                name = linking.find_pair(e, name).name
            except LinkError:
                pass  # an unlinked pair's entries are still in the log
        cfg = e.config
    else:
        cfg = config.load()
    for entry in ActivityLog(cfg.log_file).tail(args.n, name):
        who = f'"{_printable(entry["pair"])}" ' if entry.get("pair") else ""
        print(f"{entry['time']}  {who}{entry['event']}: {_printable(entry['text'])}")
    return 0


def cmd_doctor(args) -> int:
    cfg = config.load()
    problems = 0

    def report(ok: bool, text: str) -> None:
        nonlocal problems
        problems += not ok
        print(f"{'ok ' if ok else 'NO '} {text}")

    print(f"duologue {__version__}; settings in {cfg.config_file}")
    claude_bin = shutil.which(cfg.claude_bin)
    version = linking.claude_version(cfg.claude_bin) if claude_bin else ""
    found = gate.version_tuple(version)
    report(
        bool(found) and found >= gate.CLAUDE_FLOOR,
        f"Claude Code {version or 'not found'} ({claude_bin or cfg.claude_bin}); "
        f"needs {'.'.join(map(str, gate.CLAUDE_FLOOR))} or later",
    )
    report(cfg.claude_home.is_dir(), f"Claude Code's folder {cfg.claude_home}")
    with engine(lock=False) as e:
        fmt = gate.codex_format(e.codex, cfg.allow_untested_codex)
        report(
            fmt.problem is None,
            f"Codex {fmt.version or 'not found'} ({shutil.which(cfg.codex_bin) or cfg.codex_bin}); "
            + (
                fmt.problem
                or f"item format known ({gate.KNOWN_FORMATS.get(fmt.fingerprint, 'allowed')})"
            ),
        )
        report(cfg.codex_home.is_dir(), f"Codex's folder {cfg.codex_home}")
        shared = daemon_running(cfg.codex_home)
        print(
            "    Codex app-server: "
            + (
                "a shared daemon (duologue closes threads in it before writing)"
                if shared
                else "none shared; duologue starts its own, and waits while a thread is open"
            )
        )
        for p in e.pairs.values():
            report(
                p.state in ("linked", "held"),
                f'pair "{p.name}": {p.state}; {p.status or "not synced yet"}',
            )
            if p.pending:
                report(False, f'pair "{p.name}": a write to {p.pending.side} is not confirmed yet')
    try:
        print(f"    background service: {Service(cfg.home).status()}")
    except RuntimeError as err:
        print(f"    background service: {err}")
    return FAILED if problems else 0


def cmd_service(args) -> int:
    cfg = config.load()
    service = Service(cfg.home)
    if args.action == "install":
        for path in service.install():
            print(f"wrote {path}")
        print(f"duologue now syncs every 30 seconds; its output goes to {service.log_file}")
    elif args.action == "uninstall":
        removed = service.uninstall()
        print("removed " + ", ".join(removed) if removed else "the service was not installed")
    else:
        print(service.status())
    return 0


HANDLERS = {
    "link": cmd_link,
    "unlink": cmd_unlink,
    "status": cmd_status,
    "sync": cmd_sync,
    "doctor": cmd_doctor,
    "hold": cmd_hold,
    "release": cmd_release,
    "resolve": cmd_resolve,
    "log": cmd_log,
    "service": cmd_service,
}
