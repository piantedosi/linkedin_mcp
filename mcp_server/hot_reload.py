"""Reload browser.py and library.py inside the running MCP process.

Tool calls read those modules through the process globals. When a file
changes, the next tool call loads it and points the open browser session at
the new class. The stdio server and the Chromium window stay up.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

log = logging.getLogger("linkedin_mcp")

_WATCHED = ("browser", "library")
_LIBRARY_NAMES = (
    "assess",
    "extract_posting",
    "known_job_ids",
    "move_job",
    "show_job",
)
_seen: dict[str, float] = {}


def note_loaded() -> None:
    """Remember the worker files as they were imported at process start."""
    _seen.clear()
    _seen.update(_snapshot())


def refresh() -> str | None:
    """Reload worker modules when their files change. Returns an error, or None."""
    try:
        current = _snapshot()
    except OSError as exc:
        return str(exc)
    if current == _seen:
        return None
    try:
        _reload_modules()
    except Exception as exc:
        log.exception("hot reload failed")
        return f"{type(exc).__name__}: {exc}"
    try:
        _seen.clear()
        _seen.update(_snapshot())
    except OSError as exc:
        return str(exc)
    log.info("reloaded LinkedIn worker modules")
    return None


def arm() -> None:
    """Reload now and hook the already-running server's tool entry points."""
    _reload_modules()
    note_loaded()
    main = sys.modules["__main__"]
    _hook_session_from(main)
    _hook_library_calls(main)


def _snapshot() -> dict[str, float]:
    stamps: dict[str, float] = {}
    for name in _WATCHED:
        module = sys.modules[name]
        stamps[name] = Path(module.__file__ or "").stat().st_mtime
    return stamps


def _reload_modules() -> None:
    browser = sys.modules["browser"]
    library = sys.modules["library"]
    importlib.reload(browser)
    importlib.reload(library)
    _rebind()


def _rebind() -> None:
    main = sys.modules["__main__"]
    browser = sys.modules["browser"]
    library = sys.modules["library"]
    main.LinkedInSession = browser.LinkedInSession
    main.profile_dir = browser.profile_dir
    session = getattr(main, "_session", None)
    if session is not None:
        session.__class__ = browser.LinkedInSession
    for name in _LIBRARY_NAMES:
        current = getattr(main, name, None)
        if getattr(current, "_hot_library", False):
            continue
        setattr(main, name, getattr(library, name))


def _hook_session_from(main) -> None:
    current = main._session_from
    if getattr(current, "_hot_reload", False):
        return

    def _session_from(ctx):
        failed = refresh()
        if failed:
            raise main.ToolError(f"Hot reload failed: {failed}")
        return current(ctx)

    _session_from._hot_reload = True
    main._session_from = _session_from


def _hook_library_calls(main) -> None:
    """Tools that never open the browser still look these names up at call time."""

    def wrap(export: str):
        def wrapped(*args, **kwargs):
            failed = refresh()
            if failed:
                raise main.ToolError(f"Hot reload failed: {failed}")
            return getattr(sys.modules["library"], export)(*args, **kwargs)

        wrapped._hot_library = True
        wrapped.__name__ = export
        return wrapped

    for name in ("show_job", "move_job"):
        current = getattr(main, name, None)
        if getattr(current, "_hot_library", False):
            continue
        setattr(main, name, wrap(name))
