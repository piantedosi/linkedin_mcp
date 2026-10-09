"""Signed-in LinkedIn Jobs browser used by the MCP server.

The visible Chromium window stays open for the life of the server process.
Login credentials are read by the caller from the environment and typed into
the page. They are never written into tool results or saved postings.

Navigation stays on LinkedIn job search results and job posts. A company
page, profile, feed, or any other site is sent back to the last jobs URL.
"""

from __future__ import annotations

import asyncio
import html
import importlib.util
import os
import random
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

REPO_ROOT = Path(__file__).resolve().parents[1]
HTML_LIMIT = 80_000
DESCRIPTION_LIMIT = 50_000
PAUSE_MIN_SECONDS = 2.0
PAUSE_MAX_SECONDS = 6.0

_JOB_ID = re.compile(r"(?:/jobs/view/(?:[^/?#]*-)?|currentJobId=)(\d+)")
_JOBS_SURFACE = re.compile(
    r"^https://(?:www|[a-z]{2})\.linkedin\.com/jobs/"
    r"(?:"
    r"search(?:-results)?/?(?:\?[^#]*)?(?:#.*)?"
    r"|"
    r"view/(?:[^/?#]*-)?\d+/?(?:\?[^#]*)?(?:#.*)?"
    r")$",
    re.IGNORECASE,
)
_SEE_MORE = re.compile(r"see more|show more|mostra altro|visualizza altro", re.IGNORECASE)

_SEARCH_SELECTORS = (
    'input[placeholder*="Descrivi il lavoro" i]',
    'input[placeholder*="Describe the job" i]',
    "input.search-global-typeahead__input",
    "#global-nav-typeahead input",
    'header input[aria-label="Search"]',
    'header input[placeholder="Search"]',
    'input[aria-label="Search by title, skill, or company"]',
    'input[aria-label*="Search by title" i]',
    'input[aria-label*="Cerca per qualifica" i]',
    'input[id*="jobs-search-box-keyword"]',
    "input.jobs-search-box__text-input",
)
_LOCATION_SELECTORS = (
    'input[id*="jobs-search-box-location"]',
    'input[aria-label*="Città" i]',
    'input[aria-label*="City, state" i]',
    'input[aria-label*="Località" i]',
    'input[aria-label*="Location" i]',
    'input[placeholder*="Località" i]',
    'input[placeholder*="Location" i]',
)
_TITLE_SELECTORS = (
    ".job-details-jobs-unified-top-card__job-title",
    ".jobs-unified-top-card__job-title",
    "h1.t-24",
    "main h1",
)
_COMPANY_SELECTORS = (
    ".job-details-jobs-unified-top-card__company-name",
    ".jobs-unified-top-card__company-name",
    "a.topcard__org-name-link",
)
_DESCRIPTION_SELECTORS = (
    "#job-details",
    ".jobs-description__content",
    ".jobs-box__html-content",
    "article",
)
_EMAIL_SELECTORS = (
    "#username",
    'input[name="session_key"]',
    'input[autocomplete="username"]',
    'input[type="email"]',
)
_PASSWORD_SELECTORS = (
    "#password",
    'input[name="session_password"]',
    'input[autocomplete="current-password"]',
    'input[type="password"]',
)
_SIGN_IN_BUTTON = re.compile(r"^(sign in|accedi)$", re.IGNORECASE)

_COLLECT_CARDS_JS = r'''
() => {
  const idRe = /(?:\/jobs\/view\/(?:[^/?#]*-)?|currentJobId=|job-card-component-ref-)(\d{5,})/;
  const seen = new Set();
  const cards = [];
  const nodes = [];
  const walk = (root) => {
    if (!root || !root.querySelectorAll) return;
    for (const el of root.querySelectorAll("*")) {
      nodes.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  const push = (id, el) => {
    if (!id || seen.has(id)) return;
    const card = el.closest("[componentkey^='job-card-component-ref-']") || el;
    const text = (card.innerText || el.innerText || "").trim();
    if (text.length < 8) return;
    seen.add(id);
    const lines = text.split(/\n+/).map(line => line.trim()).filter(Boolean);
    cards.push({
      job_id: id,
      title: lines[0] || "",
      company: lines[1] || "",
      location: lines[2] || "",
      snippet: lines.slice(0, 8).join(" | ").slice(0, 500),
    });
  };
  for (const el of nodes) {
    const key = el.getAttribute("componentkey") || "";
    const href = el.getAttribute("href") || "";
    const attrId = el.getAttribute("data-job-id") || el.getAttribute("data-occludable-job-id") || "";
    const match = idRe.exec(key) || idRe.exec(href) || (/^\d{5,}$/.test(attrId) ? [attrId, attrId] : null);
    if (!match) continue;
    push(match[1], el);
  }
  return cards.slice(0, 30);
}
'''

_SCROLL_LIST_JS = """
() => {
  const nodes = [...document.querySelectorAll("div")];
  const list = nodes.find(node =>
    node.scrollHeight > node.clientHeight + 80
    && node.clientHeight > 300
    && node.clientHeight < 900
  );
  if (list) list.scrollTop = list.scrollHeight;
}
"""


def _ensure_headed_display() -> None:
    """Give headed Chromium a display when the parent process stripped it.

    The MCP Inspector stdio client only forwards HOME, PATH, USER, and a few
    other variables, so DISPLAY is absent even when the terminal has one.
    """
    if os.environ.get("DISPLAY", "").strip():
        return
    if Path("/tmp/.X11-unix/X0").exists():
        os.environ["DISPLAY"] = ":0"
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not runtime:
        candidate = Path(f"/run/user/{os.getuid()}")
        if candidate.is_dir():
            runtime = str(candidate)
            os.environ["XDG_RUNTIME_DIR"] = runtime
    if runtime and not os.environ.get("WAYLAND_DISPLAY", "").strip():
        if (Path(runtime) / "wayland-0").exists():
            os.environ["WAYLAND_DISPLAY"] = "wayland-0"


def profile_dir() -> Path:
    raw = os.environ.get("LINKEDIN_PROFILE_DIR", "").strip()
    if raw:
        return Path(raw)
    return REPO_ROOT / "linkedin_mcp" / ".browser_profile"


def load_normalize_slug():
    path = REPO_ROOT / "cv_test" / "python_utils" / "cv_slug.py"
    spec = importlib.util.spec_from_file_location("cv_slug_for_linkedin", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load slug helper at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.normalize_slug


normalize_slug = load_normalize_slug()


def posting_stem(company: str, title: str) -> str:
    return f"{normalize_slug(company)}_{normalize_slug(title)}"


def unique_stem(stem: str, used: set[str]) -> str:
    if stem not in used:
        used.add(stem)
        return stem
    number = 2
    while f"{stem}_{number}" in used:
        number += 1
    chosen = f"{stem}_{number}"
    used.add(chosen)
    return chosen


def render_posting(link: str, description: str) -> str:
    return f"{link}\n\n{description.strip()}\n"


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def job_id_from_url(url: str) -> str | None:
    match = _JOB_ID.search(url)
    if match is None:
        return None
    return match.group(1)


def canonical_job_url(url: str) -> str | None:
    job_id = job_id_from_url(url)
    if job_id is None:
        return None
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def jobs_surface_url(url: str) -> bool:
    return _JOBS_SURFACE.match(url.strip()) is not None


def _next_start_url(url: str) -> str:
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    start = 0
    replaced = False
    updated: list[tuple[str, str]] = []
    for key, value in pairs:
        if key == "start":
            try:
                start = int(value)
            except ValueError:
                start = 0
            updated.append((key, str(start + 25)))
            replaced = True
        else:
            updated.append((key, value))
    if not replaced:
        updated.append(("start", "25"))
    return urlunparse(parsed._replace(query=urlencode(updated)))


def blocked_page(url: str) -> str | None:
    lowered = url.lower()
    if any(part in lowered for part in ("/checkpoint", "/challenge", "authwall")):
        return "still on the login/checkpoint page"
    if "/login" in lowered or "/uas/login" in lowered:
        return "Not signed in. Call linkedin_login."
    return None


def signed_in_url(url: str) -> bool:
    lowered = url.lower()
    return "linkedin.com" in lowered and blocked_page(url) is None


def build_jobs_html(
    cards: list[dict[str, str]],
    posting: dict[str, str] | None,
    known_ids: set[str],
) -> tuple[str, list[str]]:
    """Return the jobs-region HTML and the result ids included in it."""
    posting_html = ""
    if posting:
        description = posting["description"]
        if len(description) > DESCRIPTION_LIMIT:
            description = description[:DESCRIPTION_LIMIT]
        posting_html = (
            '<section data-surface="job_post">'
            f"<h1>{_esc(posting['title'])}</h1>"
            f'<p data-company>{_esc(posting["company"])}</p>'
            f'<p data-link>{_esc(posting["link"])}</p>'
            f'<div data-description>{_esc(description)}</div>'
            "</section>"
        )
    articles: list[str] = []
    included: list[str] = []
    for card in cards:
        job_id = card["job_id"]
        known = ' data-known="true"' if job_id in known_ids else ""
        article = (
            f'<article data-job-id="{_esc(job_id)}"{known}>'
            f"<h2>{_esc(card['title'])}</h2>"
            f'<p data-company>{_esc(card["company"])}</p>'
            f'<p data-location>{_esc(card["location"])}</p>'
            f'<p data-snippet>{_esc(card["snippet"])}</p>'
            "</article>"
        )
        projected = len(posting_html) + sum(len(item) for item in articles) + len(article) + 80
        if projected > HTML_LIMIT:
            break
        articles.append(article)
        included.append(job_id)
    parts: list[str] = []
    if articles:
        parts.append('<section data-surface="search_results">' + "".join(articles) + "</section>")
    if posting_html:
        parts.append(posting_html)
    page_html = "\n".join(parts)
    if len(page_html) > HTML_LIMIT:
        page_html = page_html[:HTML_LIMIT] + "\n<!-- truncated -->"
    return page_html, included


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


class LinkedInSession:
    def __init__(self, user_data_dir: Path) -> None:
        self.profile_dir = user_data_dir
        self._playwright = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()
        self._search_url: str | None = None
        self._result_job_ids: set[str] = set()
        self._last_jobs_url: str | None = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        _ensure_headed_display()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page.set_default_timeout(20_000)

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def status(self) -> dict[str, bool]:
        async with self._lock:
            signed_in = False
            reachable = self._context is not None and self._page is not None
            if self._context is not None:
                cookies = await self._context.cookies("https://www.linkedin.com")
                signed_in = any(cookie.get("name") == "li_at" and cookie.get("value") for cookie in cookies)
            return {
                "profile_dir_exists": self.profile_dir.is_dir(),
                "browser_reachable": reachable,
                "signed_in": signed_in,
            }

    async def login(self, email: str, password: str) -> str:
        async with self._lock:
            page = self._require_page()
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            if signed_in_url(page.url):
                return "signed in"
            email_box = await self._locator(page, _EMAIL_SELECTORS)
            password_box = await self._locator(page, _PASSWORD_SELECTORS)
            if email_box is None or password_box is None:
                message = blocked_page(page.url) or "still on the login/checkpoint page"
                raise RuntimeError(message)
            try:
                await email_box.fill(email)
                await password_box.fill(password)
                await self._click_sign_in(page)
            except Exception as exc:
                raise RuntimeError("LinkedIn login form could not be submitted.") from exc
            outcome = await self._wait_for_login_outcome(page)
            if outcome != "signed in":
                raise RuntimeError(outcome)
            return outcome

    async def open_job_search(self, query: str, location: str = "") -> str:
        query = query.strip()
        if not query:
            raise RuntimeError("query is empty")
        async with self._lock:
            page = self._require_page()
            if query.startswith("https://www.linkedin.com/jobs/search"):
                if not jobs_surface_url(query):
                    raise RuntimeError("That URL is outside LinkedIn Jobs search.")
                target = query
            else:
                text = query
                if location.strip():
                    text = f"{text} {location.strip()}"
                params = {"keywords": text}
                target = "https://www.linkedin.com/jobs/search-results/?" + urlencode(params)
            await page.goto(
                target,
                wait_until="domcontentloaded",
            )
            await self._guard_jobs(page)
            await self._pause()
            await self._guard_jobs(page)
            cards = await self._wait_for_cards(page, timeout_seconds=20)
            await self._guard_jobs(page)
            self._search_url = page.url
            return f"Job search open. {len(cards)} cards. URL: {page.url}"

    async def jobs_page_html(self, known_ids: set[str]) -> str:
        async with self._lock:
            page = self._require_page()
            await self._guard_jobs(page)
            cards = await self._collect_cards(page)
            posting = None
            if await self._posting_visible(page):
                try:
                    title, company, description = await self._read_posting(page)
                except RuntimeError as exc:
                    if not str(exc).startswith("LinkedIn did not show"):
                        raise
                    title = ""
                    company = ""
                    description = ""
                link = canonical_job_url(page.url) or page.url
                if len(description) > 80:
                    posting = {
                        "title": title,
                        "company": company,
                        "link": link,
                        "description": description,
                    }
            if not cards and posting is None:
                raise RuntimeError("LinkedIn did not show job results or a job post.")
            page_html, included = build_jobs_html(cards, posting, known_ids)
            if included:
                self._result_job_ids = set(included)
            return page_html

    async def next_results_page(self) -> str:
        async with self._lock:
            page = self._require_page()
            await self._guard_jobs(page)
            if "/jobs/search" not in page.url.lower():
                if not self._search_url:
                    raise RuntimeError("No job search is open. Call open_job_search.")
                await page.goto(self._search_url, wait_until="domcontentloaded")
                await self._guard_jobs(page)
            before = {card["job_id"] for card in await self._collect_cards(page)}
            for _ in range(3):
                await page.evaluate(_SCROLL_LIST_JS)
                await asyncio.sleep(0.8)
            await self._guard_jobs(page)
            after = await self._collect_cards(page)
            new_count = sum(1 for card in after if card["job_id"] not in before)
            if new_count == 0 and "/jobs/search" in page.url.lower():
                target = _next_start_url(page.url)
                await page.goto(target, wait_until="domcontentloaded")
                await self._guard_jobs(page)
                await self._pause()
                after = await self._wait_for_cards(page, timeout_seconds=15)
                new_count = sum(1 for card in after if card["job_id"] not in before)
                self._search_url = page.url
            return (
                f"Results list scrolled. {len(after)} cards visible, {new_count} new. "
                "Call jobs_page_html to read them."
            )

    async def open_job(self, job_id: str) -> str:
        job_id = job_id.strip()
        if not job_id.isdigit():
            raise RuntimeError("job_id must be the numeric id from the results page.")
        async with self._lock:
            if job_id not in self._result_job_ids:
                raise RuntimeError(
                    "That job id was not on the last results page. "
                    "Call jobs_page_html and choose an id from that page."
                )
            page = self._require_page()
            await page.goto(
                f"https://www.linkedin.com/jobs/view/{job_id}/",
                wait_until="domcontentloaded",
            )
            await self._pause()
            await self._guard_jobs(page)
            return f"Opened job {job_id}. Call jobs_page_html to read it."

    async def back_to_results(self) -> str:
        async with self._lock:
            page = self._require_page()
            target = self._search_url or "https://www.linkedin.com/jobs/search/"
            if not jobs_surface_url(target) or "/jobs/search" not in target.lower():
                target = "https://www.linkedin.com/jobs/search/"
            await page.goto(target, wait_until="domcontentloaded")
            await self._guard_jobs(page)
            return f"Back on results. URL: {page.url}"

    async def read_open_posting(self) -> dict[str, str]:
        async with self._lock:
            page = self._require_page()
            await self._guard_jobs(page)
            if not await self._posting_visible(page):
                raise RuntimeError("No job post is open. Call open_job first.")
            title, company, description = await self._read_posting(page)
            link = canonical_job_url(page.url) or page.url
            return {
                "title": title,
                "company": company,
                "description": description,
                "link": link,
            }

    def _require_page(self):
        if self._page is None:
            raise RuntimeError("LinkedIn browser is not running.")
        return self._page

    async def _guard_jobs(self, page) -> None:
        url = page.url
        if jobs_surface_url(url):
            self._last_jobs_url = url
            if "/jobs/search" in url.lower():
                self._search_url = url
            return
        detail = blocked_page(url) or "That page is outside LinkedIn Jobs."
        if self._last_jobs_url and jobs_surface_url(self._last_jobs_url):
            await page.goto(self._last_jobs_url, wait_until="domcontentloaded")
            detail += " Returned to the last jobs page."
        else:
            detail += " Call open_job_search."
        raise RuntimeError(detail)

    async def _pause(self) -> None:
        await asyncio.sleep(random.uniform(PAUSE_MIN_SECONDS, PAUSE_MAX_SECONDS))

    async def _wait_for_login_outcome(self, page) -> str:
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            if signed_in_url(page.url):
                return "signed in"
            if blocked_page(page.url) == "still on the login/checkpoint page":
                return "still on the login/checkpoint page"
            await asyncio.sleep(0.5)
        if signed_in_url(page.url):
            return "signed in"
        return "still on the login/checkpoint page"

    async def _click_sign_in(self, page) -> None:
        button = page.get_by_role("button", name=_SIGN_IN_BUTTON)
        count = await button.count()
        for index in range(count):
            candidate = button.nth(index)
            if await candidate.is_visible():
                await candidate.click()
                return
        await page.locator('button[type="submit"]').first.click()

    async def _locator(self, page, selectors: tuple[str, ...]):
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if await candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        return None

    async def _wait_for_locator(self, page, selectors: tuple[str, ...], timeout_seconds: float):
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            found = await self._locator(page, selectors)
            if found is not None:
                return found
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.5)

    async def _collect_cards(self, page) -> list[dict[str, str]]:
        raw = await page.evaluate(_COLLECT_CARDS_JS)
        cards: list[dict[str, str]] = []
        if not isinstance(raw, list):
            return cards
        for item in raw:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("job_id") or "").strip()
            if not job_id.isdigit():
                continue
            cards.append(
                {
                    "job_id": job_id,
                    "title": str(item.get("title") or ""),
                    "company": str(item.get("company") or ""),
                    "location": str(item.get("location") or ""),
                    "snippet": str(item.get("snippet") or ""),
                }
            )
        return cards

    async def _wait_for_cards(self, page, timeout_seconds: float) -> list[dict[str, str]]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        cards = await self._collect_cards(page)
        while not cards and asyncio.get_running_loop().time() < deadline:
            await page.evaluate(_SCROLL_LIST_JS)
            await asyncio.sleep(0.8)
            cards = await self._collect_cards(page)
        return cards

    async def _posting_visible(self, page) -> bool:
        lowered = page.url.lower()
        if "/jobs/view/" in lowered or "currentjobid=" in lowered:
            return True
        try:
            text = await page.locator("#job-details, .jobs-description__content").first.inner_text(
                timeout=1_000
            )
        except Exception:
            return False
        return len(text.strip()) > 80

    async def _read_posting(self, page) -> tuple[str, str, str]:
        try:
            await page.wait_for_selector("main", timeout=20_000)
        except Exception as exc:
            raise RuntimeError("LinkedIn did not show the job posting.") from exc
        more = page.get_by_role("button", name=_SEE_MORE)
        try:
            if await more.count() and await more.first.is_visible():
                await more.first.click(timeout=2_000)
        except Exception:
            pass
        await self._guard_jobs(page)
        payload: dict[str, str] = {}
        deadline = asyncio.get_running_loop().time() + 20
        while True:
            payload = await page.evaluate(
                """() => {
                  const docTitle = document.title.replace(/\\s*\\|\\s*LinkedIn\\s*$/i, "");
                  const parts = docTitle.split("|").map(part => part.trim()).filter(Boolean);
                  const headings = [...document.querySelectorAll("h2")];
                  const about = headings.find(heading =>
                    /about the job|job description|informazioni sull.offerta|descrizione del lavoro/i
                      .test(heading.innerText || "")
                  );
                  let description = "";
                  const main = document.querySelector("main");
                  if (main && about) {
                    const mainText = main.innerText;
                    const heading = (about.innerText || "").trim();
                    const start = mainText.indexOf(heading);
                    if (start >= 0) {
                      let body = mainText.slice(start + heading.length).trim();
                      const stop = body.search(/\\n(?:Set alert|About the company|Imposta un avviso|Informazioni sull.azienda|More jobs|Altre offerte)\\b/i);
                      if (stop > 0) body = body.slice(0, stop).trim();
                      description = body;
                    }
                  }
                  if (!description) {
                    const legacy = document.querySelector("#job-details, .jobs-description__content, .jobs-box__html-content");
                    description = legacy ? legacy.innerText : "";
                  }
                  return {title: parts[0] || "", company: parts[1] || "", description: description || ""};
                }"""
            )
            description = (payload.get("description") or "").strip()
            if len(description) > 80:
                break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.5)
        title = (payload.get("title") or "").strip() or await self._text(page, _TITLE_SELECTORS) or "Untitled role"
        company = (payload.get("company") or "").strip() or await self._text(page, _COMPANY_SELECTORS) or "Unknown company"
        description = (payload.get("description") or "").strip() or await self._text(page, _DESCRIPTION_SELECTORS)
        if not description:
            raise RuntimeError("LinkedIn did not show the job description.")
        return _first_line(title), _first_line(company), description

    async def _text(self, page, selectors: tuple[str, ...]) -> str:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() == 0:
                    continue
                text = (await locator.inner_text()).strip()
            except Exception:
                continue
            if text:
                return text
        return ""


def _first_line(value: str) -> str:
    for line in value.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return value.strip()
