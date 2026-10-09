"""LinkedIn Jobs MCP server.

Cursor starts this process and speaks JSON-RPC on stdin/stdout. Logs stay on
stderr. Email and password come from the process environment. If they are
unset, they are filled from linkedin_mcp/.env.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from browser import LinkedInSession, profile_dir
from hot_reload import note_loaded, refresh as refresh_code
from library import assess, extract_posting, known_job_ids, move_job, refresh_catalog, show_job

note_loaded()

log = logging.getLogger("linkedin_mcp")


def _load_env_file() -> None:
    path = Path(__file__).with_name(".env")
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()

_RULES = (
    "Stay on LinkedIn job search results and job posts. "
    "Company pages, profiles, the feed, and the rest of the web are out of reach. "
    "The tools reject those moves.\n\n"
    "`open_job_search` starts or replaces a search. "
    "`jobs_page_html` shows the current results list or the open post. "
    "Cards marked `data-known` are already in the library. "
    "`next_results_page` reveals more cards from this search. "
    "`open_job` opens one role whose id is on the page you just saw. "
    "`back_to_results` returns to the list.\n\n"
    "`check_duplicate` says whether the open post is already in `jobs_minhash.jsonl`. "
    "`extract_job` saves the open post into `jobs_extracted`, interesting or not, so it is remembered. "
    "A duplicate is not written again. "
    "`show_extracted_job` reads a saved post back. "
    "`move_to_interesting` moves a saved post into `jobs_interesting`, and only when it matches what the user wants.\n\n"
    "You choose the query, which roles to open, which posts to extract, which of those to move, and when to stop. "
    "Do not write the files yourself."
)

_PASSWORD = "Never ask the user for their LinkedIn password. Call linkedin_login if the session is signed out."

_session: LinkedInSession | None = None


def _task(interest: str | None) -> str:
    if interest is None:
        lead = (
            "Find LinkedIn job posts that are new and interesting. "
            "Interesting means what the user just asked for in this chat. "
            "If they did not say, ask what they want before you move any post."
        )
    else:
        lead = (
            "Find LinkedIn job posts that are new and interesting. "
            f"Interesting means: {interest.strip()}. "
            "If that is empty, ask what the user wants before you move any post."
        )
    return f"{lead}\n\n{_RULES}"


@asynccontextmanager
async def app_lifespan(_server: FastMCP) -> AsyncIterator[LinkedInSession]:
    global _session
    refresh_catalog()
    log.info("job library ready")
    session = LinkedInSession(profile_dir())
    await session.start()
    _session = session
    try:
        yield session
    finally:
        _session = None
        await session.close()


mcp = FastMCP(
    "linkedin-jobs",
    instructions=f"{_task(None)}\n\n{_PASSWORD}",
    lifespan=app_lifespan,
)


def _session_from(ctx: Context) -> LinkedInSession:
    failed = refresh_code()
    if failed:
        raise ToolError(f"Hot reload failed: {failed}")
    lifespan_context = ctx.request_context.lifespan_context
    if not isinstance(lifespan_context, LinkedInSession):
        raise ToolError("LinkedIn browser is not running.")
    return lifespan_context


def _credentials() -> tuple[str, str]:
    email = os.environ.get("LINKEDIN_EMAIL", "").strip()
    password = os.environ.get("LINKEDIN_PASSWORD", "")
    missing = []
    if not email:
        missing.append("LINKEDIN_EMAIL")
    if password == "":
        missing.append("LINKEDIN_PASSWORD")
    if missing:
        names = ", ".join(missing)
        raise ToolError(f"Missing {names}. Set them in linkedin_mcp/.env and reload the MCP server.")
    return email, password


def _tool_error(exc: RuntimeError) -> ToolError:
    return ToolError(str(exc))


@mcp.tool(structured_output=False)
async def linkedin_login(ctx: Context) -> str:
    """Open the visible LinkedIn window and sign in.

    Reads LINKEDIN_EMAIL and LINKEDIN_PASSWORD from the server environment.
    This tool has no email or password arguments. Returns "signed in", or an
    error if the window stays on the login page or a checkpoint. Finish a
    verification code in the open window, then call this tool again.
    """
    email, password = _credentials()
    session = _session_from(ctx)
    try:
        return await session.login(email, password)
    except RuntimeError as exc:
        raise _tool_error(exc) from exc


@mcp.tool(structured_output=False)
async def linkedin_session_status(ctx: Context) -> str:
    """Report whether the local browser profile has a LinkedIn session.

    Checks the profile directory, the open browser, and the session cookie.
    Does not navigate to a new page.
    """
    session = _session_from(ctx)
    return json.dumps(await session.status())


@mcp.tool(structured_output=False)
async def open_job_search(query: str, ctx: Context, location: str = "") -> str:
    """Open LinkedIn Jobs search and submit a query.

    Starts a search or replaces the current one. location is optional.
    Does not save any postings. Call jobs_page_html to read the results.
    """
    session = _session_from(ctx)
    try:
        return await session.open_job_search(query, location)
    except RuntimeError as exc:
        raise _tool_error(exc) from exc


@mcp.tool(structured_output=False)
async def jobs_page_html(ctx: Context) -> str:
    """Return the HTML of the current LinkedIn Jobs surface.

    A results list is a search_results section. Each card has data-job-id.
    data-known="true" means that id is already in the MinHash catalog.
    An open posting is a job_post section. On the split view, both sections
    are returned. This is the way to see the page.
    """
    session = _session_from(ctx)
    try:
        return await session.jobs_page_html(known_job_ids())
    except RuntimeError as exc:
        raise _tool_error(exc) from exc


@mcp.tool(structured_output=False)
async def next_results_page(ctx: Context) -> str:
    """Scroll the current LinkedIn Jobs results list to reveal more cards.

    Stays on the same search. Call jobs_page_html to read the new cards.
    """
    session = _session_from(ctx)
    try:
        return await session.next_results_page()
    except RuntimeError as exc:
        raise _tool_error(exc) from exc


@mcp.tool(structured_output=False)
async def open_job(job_id: str, ctx: Context) -> str:
    """Open one role from the last results page.

    job_id must be a data-job-id from the last jobs_page_html result.
    Call jobs_page_html again to read the posting.
    """
    session = _session_from(ctx)
    try:
        return await session.open_job(job_id)
    except RuntimeError as exc:
        raise _tool_error(exc) from exc


@mcp.tool(structured_output=False)
async def back_to_results(ctx: Context) -> str:
    """Return to the current LinkedIn Jobs search results."""
    session = _session_from(ctx)
    try:
        return await session.back_to_results()
    except RuntimeError as exc:
        raise _tool_error(exc) from exc


@mcp.tool(structured_output=False)
async def check_duplicate(ctx: Context) -> str:
    """Hash the open job post and search jobs_minhash.jsonl.

    Returns is_new, the best score, the threshold, and the matching path.
    An exact job id is reported before the MinHash score. A score at or
    above 0.5 is a duplicate. This is the only duplicate search.
    """
    session = _session_from(ctx)
    try:
        posting = await session.read_open_posting()
        report = assess(posting["link"], posting["description"])
    except RuntimeError as exc:
        raise _tool_error(exc) from exc
    return json.dumps(report, ensure_ascii=False)


@mcp.tool(structured_output=False)
async def extract_job(ctx: Context) -> str:
    """Save the open job post into jobs_extracted and add its MinHash line.

    Use this for a post worth remembering, interesting or not. If the catalog
    already has that job id, or a MinHash score at or above 0.5, nothing is
    written and the existing path is returned.
    """
    session = _session_from(ctx)
    try:
        posting = await session.read_open_posting()
        report = extract_posting(
            posting["title"],
            posting["company"],
            posting["link"],
            posting["description"],
        )
    except RuntimeError as exc:
        raise _tool_error(exc) from exc
    return json.dumps(report, ensure_ascii=False)


@mcp.tool(structured_output=False)
async def show_extracted_job(job: str) -> str:
    """Return the saved text of one post in jobs_extracted.

    job is the filename stem or the numeric job id. If that post has already
    been moved, the text is returned from jobs_interesting and the reply says so.
    """
    failed = refresh_code()
    if failed:
        raise ToolError(f"Hot reload failed: {failed}")
    try:
        return show_job(job)
    except RuntimeError as exc:
        raise _tool_error(exc) from exc


@mcp.tool(structured_output=False)
async def move_to_interesting(job: str) -> str:
    """Move one post from jobs_extracted to jobs_interesting.

    job is the filename stem or the numeric job id. The MinHash line stays,
    with its path updated. Refuses when the file is not in jobs_extracted.
    """
    failed = refresh_code()
    if failed:
        raise ToolError(f"Hot reload failed: {failed}")
    try:
        return move_job(job)
    except RuntimeError as exc:
        raise _tool_error(exc) from exc


@mcp.resource("linkedin://session", mime_type="application/json")
async def linkedin_session() -> str:
    """Read-only session status: profile directory, browser, and signed-in flag."""
    if _session is None:
        payload = {
            "profile_dir_exists": profile_dir().is_dir(),
            "browser_reachable": False,
            "signed_in": False,
        }
    else:
        payload = await _session.status()
    return json.dumps(payload)


@mcp.prompt()
def find_jobs(interest: str = "") -> str:
    """Find LinkedIn job posts that are new and interesting."""
    return _task(interest)


if __name__ == "__main__":
    mcp.run()
