"""The version and format gate: duologue writes into undocumented files, so it checks first that
the apps are versions it knows.

For Codex the check that matters is the item format, not the version number: duologue writes
user and agent messages, commands, file changes and MCP calls as Codex's own items, and their
shape is in the app-server's JSON schema (``codex app-server generate-json-schema``). The gate
fingerprints those five item kinds, with every definition they reference, and compares the
fingerprint with the ones duologue was tested against. A Codex with another fingerprint gets no
writes until duologue knows it (or the user sets ``allow_untested_codex``).
"""

from __future__ import annotations

import glob
import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from duologue.codex.threads import Codex

ITEM_KINDS = ("userMessage", "agentMessage", "commandExecution", "fileChange", "mcpToolCall")
# Fingerprints of those item kinds in Codex releases duologue was tested against.
KNOWN_FORMATS = {
    "4a28793ccce040ab7e7ec9d03864f3d03da21843be41ef0616f0170cfb332ebf": "0.166.0",
}
CODEX_FLOOR = (0, 163, 0)  # paginated history and the append path's behaviour
CLAUDE_FLOOR = (2, 1, 303)  # the oldest Claude Code the transcript format was checked on


@dataclass(frozen=True)
class CodexFormat:
    version: str | None
    fingerprint: str | None
    known: bool
    problem: str | None  # why writes are refused, or None


def version_tuple(version: str | None) -> tuple[int, ...] | None:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version or "")
    return tuple(int(x) for x in match.groups()) if match else None


def fingerprint(schema_dir: Path) -> str | None:
    """The fingerprint of the item kinds duologue writes, from a generated schema folder."""
    for path in sorted(glob.glob(str(Path(schema_dir) / "**" / "*.json"), recursive=True)):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        defs = (doc.get("definitions") or doc.get("$defs") or {}) if isinstance(doc, dict) else {}
        if "ThreadItem" in defs:
            break
    else:
        return None
    variants = {}
    for v in defs["ThreadItem"].get("oneOf", []):
        kind = (v.get("properties", {}).get("type", {}).get("enum") or [None])[0]
        variants[kind] = v
    if any(k not in variants for k in ITEM_KINDS):
        return None
    chosen = {k: variants[k] for k in ITEM_KINDS}
    referenced: dict[str, object] = {}
    todo = list(chosen.values())
    while todo:
        for name in _refs(todo.pop()):
            if name not in referenced and name in defs:
                referenced[name] = defs[name]
                todo.append(defs[name])
    doc = {"variants": chosen, "definitions": referenced}
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _refs(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value.rsplit("/", 1)[-1]
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def cached_format(codex: Codex, allow_untested: bool, cache: Path) -> tuple[CodexFormat, bool]:
    """The format check, run once per Codex version: the background sync starts a process
    every 30 seconds, and reading the schema starts Codex twice. Also whether this is the
    first time this result was seen (so its alert goes out once)."""
    version = codex.version()
    try:
        saved = json.loads(Path(cache).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    key = {"bin": codex.bin, "version": version, "allow": allow_untested}
    if version and saved.get("key") == key:
        r = saved.get("result") or {}
        return CodexFormat(
            version, r.get("fingerprint"), bool(r.get("known")), r.get("problem")
        ), False
    found = codex_format(codex, allow_untested, version)
    if found.version:
        result = {"fingerprint": found.fingerprint, "known": found.known, "problem": found.problem}
        try:
            Path(cache).parent.mkdir(parents=True, exist_ok=True)
            Path(cache).write_text(json.dumps({"key": key, "result": result}), encoding="utf-8")
        except OSError:
            pass
    return found, True


def codex_format(
    codex: Codex, allow_untested: bool = False, version: str | None = None
) -> CodexFormat:
    version = version or codex.version()
    if version is None:
        return CodexFormat(None, None, False, f"Codex does not run ({codex.bin} --version failed)")
    found = version_tuple(version)
    if found is not None and found < CODEX_FLOOR:
        floor = ".".join(map(str, CODEX_FLOOR))
        return CodexFormat(version, None, False, f"Codex {version} is older than {floor}")
    with tempfile.TemporaryDirectory(prefix="duologue-schema-") as out:
        try:
            r = codex.run("app-server", "generate-json-schema", "--out", out, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            return CodexFormat(version, None, False, f"Codex's schema could not be read: {e}")
        fp = fingerprint(Path(out)) if r.returncode == 0 else None
    known = fp in KNOWN_FORMATS
    problem = None
    if not known and not allow_untested:
        problem = (
            f"Codex {version} writes its items in a format duologue has not been tested with; "
            "update duologue, or set allow_untested_codex = true in its config to write anyway"
        )
    return CodexFormat(version, fp, known, problem)
