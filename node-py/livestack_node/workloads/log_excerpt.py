"""The decisive line of a failed handler's command.log, bounded and scrubbed.

A handler that dies on a bug in the code it was asked to run (a SyntaxError in a landed
script, a failed import) leaves the one line that says so somewhere in command.log, an artifact
nobody reads first. The authority's terminal cause for such a job used to carry only the exit
code, so every reader saw "failed, no reason" (openspec typed-outcome-causes-and-blockers,
"Record 2026-10-14"). The worker now ships `excerpt(...)` in the completion and the authority
re-applies `clean` (it never trusts a worker's bounds), so the cause names what broke.

Ranking (see `_decisive`): the first line of the last failure-marker block, else the last
exit-code line, else the last quiet line; warnings and daemon/pull noise are never preferred.

One line, at most EXCERPT_BYTES UTF-8 bytes, never a line that matches a secret pattern, URL
credentials removed. Pure functions: no raising on unreadable input (absence is `None`, and the
caller records that the log was unreadable rather than inventing text).
"""
from __future__ import annotations

import re
from pathlib import Path

EXCERPT_BYTES = 300
WINDOW_BYTES = 512 * 1024  # the END of the log is ranked first: the last failure block names what ended the run
HEAD_BYTES = 512 * 1024    # ... the start is consulted only when the end holds no failure marker

SECRET = re.compile(r"keystore|storepass|keypass|key_password|key\.properties|token|secret|passw(or)?d|"
                    r"authorization|bearer|api[_-]?key|credential", re.I)
URL_USERINFO = re.compile(r"(\w+://)[^/\s@]+@")
LONG_TOKEN = re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Tier 1, a harness's own failure marker. Case-sensitive on purpose: the old case-insensitive
# `\bFAILED\b` picked containerd's `level=warning msg="failed check for fsverity support"` over the
# real failure (authority job a12d9930, 2026-10-14).
DECISIVE = re.compile(
    r"\b[A-Z]\w*(?:Error|Exception|Failed|Failure)\b\s*(?:\[\w+\])?:|\bError:|"
    r"(?i:^\s*(?:error|fatal)(?:\[\w+\])?:)|(?i:^\s*error\s+[\u2022-])|(?i:\b(?:build|compilation|compile) failed\b)|"
    r"\bpanicked at\b|^\s*panic\b|\bFAILED\b|^\s*FAIL\b|\bELIFECYCLE\b|\bnpm ERR!|Cannot find (?:module|package)|"
    r"\bSome tests failed\b|\[E\]\s*$|\b[1-9]\d* (?:tests? )?failed\b|\bTests?:.*\bfailed\b|\b[1-9]\d* issues? found|^E {2,}\S")
# Tier 2, an exit-code line.
EXIT_LINE = re.compile(r"(?i:\bexit(?:ed)?(?: with)?(?: code| status)?[ :=]+[1-9]\d*\b|non-?zero exit|returned non-zero)")
# Down-ranked whatever else they match: warnings, info-level daemon chatter, deprecations,
# registry pull noise and progress bars.
NOISE = re.compile(
    r"(?i:\blevel=(?:warn(?:ing)?|info|debug|trace)\b|^\W*warn(?:ing)?\b|fsverity|deprecat|skip loading plugin|"
    r"pulling fs layer|pull complete|download complete|verifying checksum|status: (?:downloaded|image is up)|"
    r"digest: sha256:|^\W*waiting$|\beta\b)|\bWARN(?:ING)?\b|#{5,}|\[=+>\s*\]|(?:^|\s)\d{1,3}(?:\.\d+)?%(?:\s|$|\|)")
BLOCK_GAP = 2   # quiet lines tolerated inside one failure block (a source frame, a `-->` pointer)
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


def _lines(raw):
    return [ANSI.sub("", line) for line in raw.decode("utf-8", "replace").splitlines()]


def _place(path):
    """The tree-relative tail of a path: never the worker's home or workspace prefix."""
    for marker in ("/source/", "/input/"):
        if marker in path:
            return path.split(marker, 1)[1]
    return path.rsplit("/", 1)[-1]


def _locate(lines, index):
    """` (at tree/path:LINE)` when node printed `file:LINE` just above a SyntaxError-style line."""
    for earlier in reversed(lines[max(0, index - 6):index]):
        match = LOCATION.match(earlier.strip())
        if match:
            return " (at %s:%s)" % (_place(match.group(1)), match.group(2))
    return ""


def _decisive(lines):
    """Rank candidates: (1) the FIRST line of the LAST failure-marker block (the first error of a
    run often cascades, so the final block names what ended it), (2) the last exit-code line,
    (3) None, leaving the caller to take the last quiet line. Warnings and noise never qualify."""
    usable = [i for i, line in enumerate(lines) if line.strip() and not NOISE.search(line) and not SECRET.search(line)]
    hits = [i for i in usable if DECISIVE.search(lines[i])]
    if hits:
        start = hits[-1]
        for earlier in reversed(hits[:-1]):
            quiet = [i for i in usable if earlier < i < start and not DECISIVE.search(lines[i])]
            if len(quiet) > BLOCK_GAP:
                break
            start = earlier
        return lines[start].strip() + _locate(lines, start)
    exits = [i for i in usable if EXIT_LINE.search(lines[i])]
    return lines[exits[-1]].strip() if exits else None


def excerpt(path, exit_code):
    """`exit N: <decisive line>` for a failed run, from command.log at `path`; None when unreadable."""
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - WINDOW_BYTES))
            tail = handle.read(WINDOW_BYTES)
            head = b""
            if size > WINDOW_BYTES:
                handle.seek(0)
                head = handle.read(HEAD_BYTES)
    except OSError:
        return None
    lines = _lines(tail)
    if head:
        lines = lines[1:]   # the window starts mid-line
    chosen = _decisive(lines)
    if chosen is None and head:
        chosen = _decisive(_lines(head))   # a long log whose only failure marker scrolled out of the window
    if chosen is None:
        quiet = [line for line in lines if line.strip()]
        chosen = next((line for line in reversed(quiet) if not NOISE.search(line)), quiet[-1] if quiet else "").strip() or None
    prefix = "exit %s" % (exit_code,) if exit_code is not None else "failed"
    body = clean(chosen) if chosen else None
    line = "%s: %s" % (prefix, body) if body else "%s; command.log had no output" % prefix
    return line.encode("utf-8")[:EXCERPT_BYTES].decode("utf-8", "ignore")
