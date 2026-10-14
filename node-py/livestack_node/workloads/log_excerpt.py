"""The decisive line of a failed handler's command.log, bounded and scrubbed.

A handler that dies on a bug in the code it was asked to run (a SyntaxError in a landed
script, a failed import) leaves the one line that says so somewhere in command.log, an artifact
nobody reads first. The authority's terminal cause for such a job used to carry only the exit
code, so every reader saw "failed, no reason" (openspec typed-outcome-causes-and-blockers,
"Record 2026-10-14"). The worker now ships `excerpt(...)` in the completion and the authority
re-applies `clean` (it never trusts a worker's bounds), so the cause names what broke.

One line, at most EXCERPT_BYTES UTF-8 bytes, never a line that matches a secret pattern, URL
credentials removed. Pure functions: no raising on unreadable input (absence is `None`, and the
caller records that the log was unreadable rather than inventing text).
"""
from __future__ import annotations

import re
from pathlib import Path

EXCERPT_BYTES = 300
HEAD_BYTES = 512 * 1024   # the cause is usually the FIRST error, long before post-failure noise
TAIL_BYTES = 32 * 1024    # ... and the last lines are the fallback when no error line matched

SECRET = re.compile(r"keystore|storepass|keypass|key_password|key\.properties|token|secret|passw(or)?d|"
                    r"authorization|bearer|api[_-]?key|credential", re.I)
URL_USERINFO = re.compile(r"(\w+://)[^/\s@]+@")
LONG_TOKEN = re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# A decisive line: a named language error, a compiler/tool `error:` line, or a test failure.
DECISIVE = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b\s*(?:\[\w+\])?:|^\s*error(?:\[\w+\])?:|\bpanicked at\b|"
                      r"\bFAILED\b|^\s*fatal:|Cannot find (?:module|package)", re.I)
# node prints `file:///path/x.mjs:LINE` before a SyntaxError's source excerpt
LOCATION = re.compile(r"^(?:file://)?(\S+\.[cm]?[jt]sx?):(\d+)\s*$")


def clean(text, limit=EXCERPT_BYTES):
    """One scrubbed line of at most `limit` bytes, or None when nothing printable remains."""
    if not isinstance(text, str):
        return None
    text = ANSI.sub("", text)
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    if not text:
        return None
    if SECRET.search(text):
        return "[line withheld: matches a secret pattern]"
    text = LONG_TOKEN.sub("[redacted]", URL_USERINFO.sub(r"\1", text))
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _place(path):
    """The tree-relative tail of a path: never the worker's home or workspace prefix."""
    for marker in ("/source/", "/input/"):
        if marker in path:
            return path.split(marker, 1)[1]
    return path.rsplit("/", 1)[-1]


def _decisive(lines):
    for index, line in enumerate(lines):
        if not DECISIVE.search(line) or SECRET.search(line):
            continue
        where = ""
        for earlier in reversed(lines[max(0, index - 6):index]):
            match = LOCATION.match(earlier.strip())
            if match:
                where = " (at %s:%s)" % (_place(match.group(1)), match.group(2))
                break
        return line.strip() + where
    return None


def excerpt(path, exit_code):
    """`exit N: <decisive line>` for a failed run, from command.log at `path`; None when unreadable."""
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(0)
            head = handle.read(HEAD_BYTES)
            tail = b""
            if size > HEAD_BYTES:
                handle.seek(max(HEAD_BYTES, size - TAIL_BYTES))
                tail = handle.read(TAIL_BYTES)
    except OSError:
        return None
    lines = head.decode("utf-8", "replace").splitlines()
    chosen = _decisive(lines)
    if chosen is None:
        rest = (tail.decode("utf-8", "replace").splitlines() if tail else lines)
        quiet = [line for line in rest if line.strip()]
        chosen = quiet[-1].strip() if quiet else None
    prefix = "exit %s" % (exit_code,) if exit_code is not None else "failed"
    body = clean(chosen) if chosen else None
    line = "%s: %s" % (prefix, body) if body else "%s; command.log had no output" % prefix
    return line.encode("utf-8")[:EXCERPT_BYTES].decode("utf-8", "ignore")
