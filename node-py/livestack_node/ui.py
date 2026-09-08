"""The page a person opens — one HTML file, served by whichever broker is asked.

Harmony's state was readable only as JSON: `/fleet` is complete and nobody can
hold five hosts, nine nodes, six cards and their resident sets in their head
from it. The map that answers "what is on which GPU right now" had to be drawn
by hand every time somebody wanted it.

Two rules, both from the fleet view it renders:

* **An absence is a row, never a gap.** A node that cannot be read is drawn with
  its state, its age and its error, not omitted; a machine that reports no host
  memory says so rather than showing an empty bar.
* **It says how old it is.** The page keeps the last view when the broker stops
  answering and ages it in place, because a blank page cannot be told apart from
  an empty fleet — and this page is opened precisely when something is wrong.

No build step, no CDN, no framework: one self-contained file. Half this fleet is
behind the GFW, where a page that fetches a library from a CDN is a page that
renders blank, and a dashboard that needs a toolchain to change is one that
rots.
"""
from __future__ import annotations

from pathlib import Path

_PAGE = Path(__file__).with_name("ui.html")
_CACHE: dict = {}


def page() -> str:
    """The dashboard, read once and held. Re-read when the file changes on disk
    so editing it during development needs no restart."""
    try:
        stamp = _PAGE.stat().st_mtime_ns
    except OSError:
        return _CACHE.get("html") or "<h1>Harmony</h1><p>ui.html is missing.</p>"
    if _CACHE.get("stamp") != stamp:
        _CACHE.update(stamp=stamp, html=_PAGE.read_text(encoding="utf-8"))
    return _CACHE["html"]
