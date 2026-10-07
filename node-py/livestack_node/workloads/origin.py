"""Advisory `labels.origin` / `labels.describe` for a submission.

Display text for people reading the queue, NOT authentication or authorization: the authority
trusts only the caller's principal. Sourced from what the submitting process already knows (the
pane environment a coding agent runs in), never from a token, e-mail or secret."""
import getpass
import os
import socket
import sys

_RUNNER_ENV = (("CLAUDECODE", "claude"), ("CODEX_SANDBOX", "codex"), ("CODEX_THREAD_ID", "codex"),
               ("OPENCODE", "opencode"), ("AIDER_MODEL", "aider"))


def _safe(text, limit):
    return "".join(c for c in str(text) if c.isalnum() or c in "_.@:-")[:limit]


def job_origin(environ=None, *, isatty=None):
    env = os.environ if environ is None else environ
    host = _safe(socket.gethostname(), 40) or "unknown-host"
    pane = env.get("HERDR_PANE_ID") or env.get("TMUX_PANE")
    if pane:
        runner = next((name for key, name in _RUNNER_ENV if env.get(key)), "agent")
        mux = "herdr:" + _safe(env.get("HERDR_WORKSPACE_ID", ""), 16) + ":" if env.get("HERDR_PANE_ID") else ""
        return f"agent:{runner}@{host}:{mux}{_safe(pane, 24)}"[:100 + 6]
    try:
        user = _safe(getpass.getuser(), 32) or "unknown"
    except Exception:
        user = "unknown"
    interactive = (sys.stdin.isatty() if isatty is None else isatty)
    return f"{'human' if interactive else 'system'}:{user}@{host}"


def stamp(request, environ=None):
    """Copy of a submission with origin/describe filled in where the caller left them out."""
    if not isinstance(request, dict):
        return request
    labels = dict(request.get("labels") or {})
    labels.setdefault("origin", job_origin(environ))
    labels.setdefault("describe", f"{request.get('handler', '?')}: {request.get('key', '')}"[:200])
    return dict(request, labels=labels)
