"""
Stealth LinkedIn Post & Recruiter Scraper for Cold Outreach.

Design Decisions:
- Persistent Context: Uses a persistent browser profile (`artifacts/linkedin_chrome_profile`) to store tracking cookies & tokens.
- JS Stealth Injection: Masks `navigator.webdriver`, languages, and plugins to evade anti-bot scripts.
- Human Pacing: Scrolls with variable delays and expands "...see more" buttons to reveal post text.
- Manual Login Fallback: Waits up to 60s for manual login in non-headless mode if redirected to login/authwall.
"""
import asyncio
import os
import random
import re
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright

from ..logging_setup import get_logger
from .analyzer import (
    IIT_ONLY_REJECT_REGEX,
    ZERO_TECH_REJECT_REGEX,
    classify_role,
    extract_company,
    extract_poster,
    extract_recruiter_emails,
    has_profile_skill_overlap,
    is_abroad_onsite,
    is_experience_match,
    is_likely_hiring_poster,
    is_rate_limit_page,
    is_spam_or_unpaid,
    parse_post_age_hours,
    post_hash,
    post_content,
    resolve_recipient_first_name,
)


class RateLimitedError(Exception):
    """LinkedIn is throttling the session/IP (Cloudflare 1200 class).
    Continuing to scrape burns the account for zero reads — stop the run."""

log = get_logger(__name__)


class CookieExpiredError(Exception):
    """Raised when the LinkedIn li_at authentication cookie has expired."""


_STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
    window.chrome = { runtime: {}, loadTimes: function() {} };
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en-US', 'en'] });
"""


class LinkedInHunter:
    def __init__(self, li_at_cookie: str = "", headless: bool = True, storage_state: dict | None = None) -> None:
        self.li_at_cookie = (li_at_cookie or "").strip()
        self.headless = headless
        self.storage_state = storage_state

    async def _setup_stealth_context(
        self, p: Any, headless: bool | None = None, inject_cookies: bool = True
    ) -> BrowserContext:
        """Creates a persistent browser context that stores session state and evades anti-bot detection."""
        profile_dir = Path("artifacts/linkedin_chrome_profile").resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)

        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.headless if headless is None else headless,
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            # Software rendering + lean shared memory: this browser only
            # reads DOM text (screenshots/video never used), and headed GPU
            # renderers on LinkedIn's feed are what die with
            # "Input.dispatchMouseEvent: Internal error", taking the search
            # down. SwiftShader fallback keeps headed mode fully working.
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        # Memory armor: LinkedIn feed pages are renderer hogs (infinite
        # images/video/fonts) and a dead renderer kills the whole search with
        # "Input.dispatchMouseEvent: Internal error". Text + structure is all
        # the hunter reads — drop the heavy bytes at the network layer.
        try:
            await context.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,mp4,webm}",
                lambda route: route.abort(),
            )
        except Exception:
            pass

        shared_cookies = [c for c in (self.storage_state or {}).get("cookies", [])
                          if c.get("domain", "").lstrip(".") in {"linkedin.com", "www.linkedin.com"}]
        if inject_cookies and any(c.get("name") == "li_at" for c in shared_cookies):
            # Share the primary login that Easy Apply already uses, instead
            # of preferring an unrelated stale campaign profile cookie.
            await context.add_cookies(shared_cookies)
            log.info("linkedin.shared_primary_session_loaded")
            return context
        if inject_cookies and self.li_at_cookie:
            # A session saved by login_interactive() is always fresher than the
            # env cookie — injecting a stale li_at would overwrite it and break
            # the login. Only inject when the profile has no session yet.
            existing = await context.cookies()
            if any(c.get("name") == "li_at" for c in existing):
                log.info(
                    "linkedin.profile_session_found",
                    detail="Using saved profile session; skipping li_at cookie injection.",
                )
                return context

            clean_cookie = self.li_at_cookie.strip().strip('"').strip("'")
            jsession_id = os.getenv("LINKEDIN_JSESSIONID", "").strip().strip('"').strip("'")

            cookies_to_add = [
                {
                    "name": "li_at",
                    "value": clean_cookie,
                    "domain": ".linkedin.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            ]
            if jsession_id:
                cookies_to_add.append(
                    {
                        "name": "JSESSIONID",
                        "value": jsession_id,
                        "domain": ".linkedin.com",
                        "path": "/",
                        "secure": True,
                    }
                )

            await context.add_cookies(cookies_to_add)
        return context

    # Container families the harvester reads. The stall counter watches the
    # SAME set: counting a subset while harvesting a superset manufactured
    # false stalls (posts loaded invisibly to the counter).
    COUNT_SELECTORS = (
        "li.reusable-search__result-container, div.feed-shared-update-v2, "
        "div.occludable-update, article, "
        "div[componentkey*='FeedType_FLAGSHIP_SEARCH']"
    )

    # End-of-feed markers (fail-open: a miss falls back to stall logic,
    # never to an error — LinkedIn rewrites this copy regularly).
    END_OF_FEED_MARKERS = (
        "no more results",
        "you've seen all",
        "you have seen all",
        "end of results",
        "no further results",
    )

    async def _human_scroll(
        self,
        page: Page,
        max_scrolls: int = 30,
        stall_rounds: int = 3,
        count_selector: str = "",
        identity_fn=None,
        wall_check=None,
        end_markers: tuple = (),
    ) -> dict:
        """Scroll-until-exhausted: read rich searches deep, skip thin ones fast.

        Progress = NEW post identities when `identity_fn` is given (immune
        to list virtualization, where node counts stay flat while fresh
        posts stream past), else node-count growth (legacy callers/tests).
        A dead page (closed browser/renderer) ends the read instead of
        raising. Returns {rounds, new_ids, exit} where exit is one of
        stall|cap|end_marker|wall|dead.
        """
        cls = type(self) if self is not None else LinkedInHunter
        count_selector = count_selector or cls.COUNT_SELECTORS
        end_markers = end_markers if end_markers else cls.END_OF_FEED_MARKERS
        try:
            last_count = await page.locator(count_selector).count()
        except Exception:
            last_count = 0
        seen_ids: set[str] = set()
        use_identity = callable(identity_fn)
        if use_identity:
            try:
                seen_ids = set(await identity_fn() or ())
            except Exception:
                seen_ids = set()
        new_total = 0
        stagnant = 0
        done = 0
        exit_reason = "cap"
        for _ in range(max_scrolls):
            try:
                await page.mouse.wheel(0, random.randint(600, 1400))
            except Exception:
                exit_reason = "dead"
                break
            await asyncio.sleep(random.uniform(1.8, 3.5))
            done += 1
            # Mid-scroll wall abort: walls often clear the list — stop now,
            # not in 20 more rounds.
            if wall_check is not None:
                try:
                    if await wall_check():
                        exit_reason = "wall"
                        break
                except Exception:
                    pass
            if end_markers:
                try:
                    body = ((await page.locator("body").first.inner_text(
                        timeout=2000)) or "").lower()
                except Exception:
                    body = ""
                if body and any(m in body for m in end_markers):
                    exit_reason = "end_marker"
                    break
            try:
                count_now = await page.locator(count_selector).count()
            except Exception:
                exit_reason = "dead"
                break
            # Collapse guard: list cleared under us (wall without markers).
            if count_now < last_count - max(5, last_count // 2):
                exit_reason = "wall"
                break
            progressed = False
            if use_identity:
                try:
                    current = set(await identity_fn() or ())
                except Exception:
                    current = set()
                fresh = current - seen_ids
                if fresh:
                    progressed = True
                    new_total += len(fresh)
                    seen_ids |= current
                last_count = max(last_count, count_now)
            else:
                if count_now > last_count:
                    progressed = True
                    last_count = count_now
            if progressed:
                stagnant = 0
            else:
                stagnant += 1
                if stagnant >= stall_rounds:
                    exit_reason = "stall"
                    break
        return {"rounds": done, "new_ids": new_total, "exit": exit_reason}

    _AUTHOR_SELECTORS = [
        ".feed-shared-actor__name",
        "span.update-components-actor__name",
        ".update-components-actor__title span[aria-hidden='true']",
    ]
    _HEADLINE_SELECTORS = [
        ".feed-shared-actor__description",
        "span.update-components-actor__description",
        ".update-components-actor__subtitle span[aria-hidden='true']",
    ]

    @staticmethod
    def _clean_author(raw: str) -> str:
        """Strip connection-degree suffixes ('• 1st') and metadata."""
        if not raw:
            return ""
        first_line = raw.strip().splitlines()[0]
        cleaned = re.sub(r"\s*[•·|]\s*.*$", "", first_line).strip()
        return cleaned[:80]

    async def _extract_author(self, container) -> tuple[str, str]:
        """Poster name + headline, best-effort across LinkedIn layouts."""
        author, headline = "", ""
        for sel in self._AUTHOR_SELECTORS:
            try:
                loc = container.locator(sel).first
                if await loc.count():
                    text = await loc.inner_text()
                    if text and text.strip():
                        author = self._clean_author(text)
                        break
            except Exception:
                continue
        for sel in self._HEADLINE_SELECTORS:
            try:
                loc = container.locator(sel).first
                if await loc.count():
                    text = await loc.inner_text()
                    if text and text.strip():
                        headline = text.strip().splitlines()[0][:160]
                        break
            except Exception:
                continue
        return author, headline

    async def _expand_long_posts(self, page: Page) -> None:
        """Finds and clicks all '...see more' buttons to reveal hidden post text and email addresses."""
        selectors = [
            "button.feed-shared-inline-show-more-text__button",
            "button.see-more",
            "button[aria-label*='see more']",
            "button[aria-label*='See more']",
        ]
        for sel in selectors:
            try:
                buttons = await page.locator(sel).all()
                for btn in buttons[:15]:
                    if await btn.is_visible():
                        await btn.click()
                        await asyncio.sleep(0.3)
            except Exception:
                continue

    async def login_interactive(self, timeout_s: int = 180) -> bool:
        """
        Opens the persistent profile browser (headed) for a manual LinkedIn login.

        The saved session cookies live in `artifacts/linkedin_chrome_profile`,
        which `hunt_for_jobs` reuses — so one manual login here keeps the
        headless campaign authenticated. Returns True once the `li_at` session
        cookie appears, False on timeout.
        """
        log.info("linkedin.login_interactive", detail="Opening browser for manual login...")
        async with async_playwright() as p:
            # Skip li_at injection: a stale cookie would break the fresh login.
            context = await self._setup_stealth_context(p, headless=False, inject_cookies=False)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.add_init_script(_STEALTH_JS)

            ok = False
            try:
                await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=45_000)

                deadline = asyncio.get_event_loop().time() + timeout_s
                while asyncio.get_event_loop().time() < deadline:
                    cookies = await context.cookies()
                    if any(c.get("name") == "li_at" for c in cookies):
                        ok = True
                        break
                    await asyncio.sleep(2.0)

                if ok:
                    log.info("linkedin.login_interactive_success", detail="li_at cookie detected; session saved to profile.")
                else:
                    log.warning(
                        "linkedin.login_interactive_timeout",
                        detail=f"No li_at cookie after {timeout_s}s. Login not completed.",
                    )
            except Exception as exc:
                log.error("linkedin.login_interactive_crashed", error=str(exc)[:200])
            finally:
                await context.close()
        return ok

    @staticmethod
    async def _goto_search(page: Page, search_url: str) -> bool:
        """Navigate with one retry and a commit fallback.

        LinkedIn search pages intermittently hang past 45s (heavy SPA,
        throttled automation traffic). A single retry with
        wait_until="commit" recovers transient stalls; a second failure
        means the page is genuinely unreachable — caller moves on.
        """
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)
            return True
        except Exception as first:
            log.warning("linkedin.goto_retry", error=str(first)[:150], url=search_url[:70])
        try:
            await page.goto(search_url, wait_until="commit", timeout=45_000)
            await asyncio.sleep(3.0)
            return True
        except Exception as second:
            log.warning("linkedin.goto_failed", error=str(second)[:150], url=search_url[:70])
            return False

    async def _copy_post_permalink(self, page: Page, container) -> str:
        """Use the post's native Copy link control without reading the OS clipboard."""
        menu = container.locator("button[aria-label^='Open control menu for post']").first
        if not await menu.count():
            return ""
        installed = False
        try:
            installed = await page.evaluate("""() => {
                const clipboard = navigator.clipboard;
                if (!clipboard) return false;
                const own = Object.getOwnPropertyDescriptor(clipboard, 'writeText');
                const command = document.execCommand;
                window.__jobAgentCopiedLink = '';
                Object.defineProperty(clipboard, 'writeText', {configurable:true, value: async value => {window.__jobAgentCopiedLink = String(value);}});
                // Some LinkedIn layouts use a temporary selected textarea and
                // execCommand rather than the asynchronous clipboard API.
                document.execCommand = function(name, ...args) {
                    if (String(name).toLowerCase() === 'copy') {
                        const active = document.activeElement;
                        const selected = active && typeof active.value === 'string'
                            ? active.value.slice(active.selectionStart || 0, active.selectionEnd ?? active.value.length)
                            : String(window.getSelection() || '');
                        window.__jobAgentCopiedLink = selected;
                        return true;
                    }
                    return command.call(this, name, ...args);
                };
                window.__jobAgentRestoreClipboard = () => {
                    document.execCommand = command;
                    if (own) Object.defineProperty(clipboard, 'writeText', own);
                    else delete clipboard.writeText;
                };
                return true;
            }""")
            if not installed:
                return ""
            await menu.click(timeout=3000)
            copy = page.get_by_text("Copy link to post", exact=True)
            await copy.wait_for(state="visible", timeout=3000)
            if await copy.count() != 1:
                log.debug("linkedin.post_permalink_ambiguous", count=await copy.count())
                return ""
            await copy.click(timeout=3000)
            await page.wait_for_function("() => !!window.__jobAgentCopiedLink", timeout=2000)
            value = await page.evaluate("() => window.__jobAgentCopiedLink")
            from urllib.parse import urlsplit
            source = urlsplit(value)
            if source.scheme == "https" and source.hostname == "lnkd.in" and source.path.startswith("/p/"):
                # The current native copy menu emits a LinkedIn short link.
                # Resolve its real redirect; never invent an activity URN.
                response = await page.context.request.get(value, timeout=15000)
                value = response.url
                source = urlsplit(value)
            if source.hostname in {"www.linkedin.com", "linkedin.com"} and source.path.startswith(("/posts/", "/feed/update/")):
                return source._replace(query="", fragment="").geturl()
        except Exception as exc:
            log.debug("linkedin.post_permalink_unavailable", error=str(exc)[:100])
        finally:
            try:
                if installed:
                    await page.evaluate("() => {window.__jobAgentRestoreClipboard?.(); delete window.__jobAgentCopiedLink; delete window.__jobAgentRestoreClipboard;}")
                await page.keyboard.press("Escape")
            except Exception:
                pass
        return ""

    async def hunt_for_jobs(
        self, search_url: str, required_skills: set[str] | frozenset[str] | None = None,
        search_label: str = "", seen: dict[str, list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Navigates to the search URL, waits for posts, extracts text, classifies roles, and extracts emails.
        required_skills: profile-map skills (years > 0). Posts naming none
        of them are rejected — quality (match-to-profile) beats volume.
        search_label + seen: cross-search overlap measurement. Every read
        post's hash is recorded under its keyword so the campaign can report
        duplication rates and prune redundant keywords with data.
        """
        results: list[dict[str, Any]] = []
        rejected: dict[str, int] = {}
        # Posts that reached the author gate (i.e. had emails + passed all
        # text gates) vs ones where name/headline extraction worked. A 0%
        # hit rate means LinkedIn changed its DOM and the selectors below
        # are stale — the seeker_poster gate silently stops working then.
        author_hits = 0
        author_gated = 0
        log.info("linkedin.hunt_started", url=search_url[:70])

        def _drop(reason: str, preview: str = "") -> None:
            rejected[reason] = rejected.get(reason, 0) + 1
            log.debug(f"linkedin.lead.rejected_{reason}", preview=preview[:70].replace("\n", " "))

        async with async_playwright() as p:
            context = await self._setup_stealth_context(p)
            page = context.pages[0] if context.pages else await context.new_page()

            await page.add_init_script(_STEALTH_JS)

            try:
                # Go directly to search URL
                if not await self._goto_search(page, search_url):
                    return results

                # Rate wall check BEFORE anything else: a throttled session
                # renders the interstitial on every navigation, and each
                # further search only deepens the flag on the account.
                try:
                    _title = (await page.title()) or ""
                except Exception:
                    _title = ""
                if is_rate_limit_page(_title, page.url):
                    log.error("linkedin.rate_limited", url=search_url[:70])
                    raise RateLimitedError("LinkedIn rate wall (1200 class) — stopping campaign")

                # Check if redirected to login
                page_title = await page.title()
                is_auth_page = (
                    "login" in page.url.lower() or
                    "authwall" in page.url.lower() or
                    "signup" in page.url.lower() or
                    "checkpoint" in page.url.lower() or
                    "login" in page_title.lower() or
                    "sign in" in page_title.lower() or
                    await page.locator("form.login__form").count() > 0 or
                    await page.locator("#username").count() > 0
                )
                if is_auth_page:
                    if self.headless and not any(x in page.url.lower() for x in ("checkpoint", "challenge", "security-check")):
                        try:
                            await page.wait_for_url("**/search/results/**", timeout=20_000)
                            is_auth_page = any(x in page.url.lower() for x in ("login", "authwall", "signup", "checkpoint"))
                        except Exception:
                            pass
                if is_auth_page:
                    if not self.headless:
                        log.info("linkedin.manual_login", detail="Waiting 60 seconds for you to log in MANUALLY...")
                        await page.wait_for_url("**/search/results/**", timeout=60_000)
                        log.info("linkedin.manual_login_success", detail="Login detected! Session saved.")
                    else:
                        raise CookieExpiredError("Cookie rejected. Run in headed mode to log in manually.")

                # Wait for search results to render
                try:
                    await page.wait_for_selector(
                        "li.reusable-search__result-container, div.feed-shared-update-v2, div[componentkey*='FeedType_FLAGSHIP_SEARCH'], span:text-is('Feed post')",
                        timeout=15_000
                    )
                    await asyncio.sleep(2)
                except Exception:
                    # Empty page after a full wait is how a soft wall often
                    # presents (no markers, just nothing). Re-probe before
                    # calling it an empty search.
                    try:
                        _t = (await page.title()) or ""
                    except Exception:
                        _t = ""
                    if is_rate_limit_page(_t, page.url):
                        log.error("linkedin.rate_limited", url=search_url[:70])
                        raise RateLimitedError("LinkedIn rate wall (1200 class) — stopping campaign")
                    log.warning("linkedin.no_posts", detail="No posts appeared within 15 seconds. Page might be empty.")

                async def _current_post_ids():
                    """Identities of currently loaded posts (bounded read)."""
                    try:
                        locs = await page.locator(
                            "li.reusable-search__result-container, "
                            "div.feed-shared-update-v2, div.occludable-update, "
                            "article, div[componentkey*='FeedType_FLAGSHIP_SEARCH']"
                        ).all()
                    except Exception:
                        return frozenset()
                    ids: set[str] = set()
                    for loc in locs[:40]:
                        try:
                            t = await loc.inner_text(timeout=1000)
                        except Exception:
                            continue
                        if t and t.strip():
                            ids.add(post_hash(t))
                    return frozenset(ids)

                async def _wall_present() -> bool:
                    try:
                        _t = (await page.title()) or ""
                    except Exception:
                        _t = ""
                    try:
                        _u = page.url or ""
                    except Exception:
                        _u = ""
                    return is_rate_limit_page(_t, _u)

                scroll_stats = await self._human_scroll(
                    page, identity_fn=_current_post_ids, wall_check=_wall_present)
                # Selector health vs empty feed: zero containers under ANY
                # known family means LinkedIn changed its DOM (alert-worthy),
                # while matches-but-empty means a genuinely thin query.
                family_hits: dict[str, int] = {}
                for _fam, _sel in (
                    ("reusable", "li.reusable-search__result-container"),
                    ("feed-shared", "div.feed-shared-update-v2"),
                    ("occludable", "div.occludable-update"),
                    ("article", "article"),
                    ("componentkey", "div[componentkey*='FeedType_FLAGSHIP_SEARCH']"),
                ):
                    try:
                        family_hits[_fam] = await page.locator(_sel).count()
                    except Exception:
                        family_hits[_fam] = -1
                if all(v <= 0 for v in family_hits.values()):
                    log.warning("linkedin.layout_drift_suspected", url=search_url[:70])
                log.info("linkedin.scroll_telemetry", search_label=search_label,
                         **scroll_stats,
                         families={k: v for k, v in family_hits.items() if v > 0})
                await self._expand_long_posts(page)

                containers = await page.locator(
                    "li.reusable-search__result-container, div.feed-shared-update-v2, div.occludable-update, article, div[componentkey*='FeedType_FLAGSHIP_SEARCH']"
                ).all()

                if not containers:
                    # Fallback for even newer layout structures
                    spans = await page.locator("span:text-is('Feed post')").all()
                    containers = []
                    for span in spans:
                        # Grab the container which is typically a few levels up
                        containers.append(span.locator("xpath=../../.."))

                seen_texts: set[str] = set()

                for container in containers:
                    text = await container.inner_text()
                    if not text or text in seen_texts:
                        continue
                    seen_texts.add(text)
                    if seen is not None:
                        labels = seen.setdefault(post_hash(text), [])
                        label = search_label or "?"
                        if not labels or labels[-1] != label:
                            labels.append(label)

                    emails = extract_recruiter_emails(text)
                    if not emails:
                        _drop("no_email", text)
                        continue

                    author_gated += 1
                    author, headline = await self._extract_author(container)
                    if not author and not headline:
                        # DOM missed (stale selectors, truncated render):
                        # the snippet itself carries "Name • degree • headline".
                        sn_author, sn_headline = extract_poster(text)
                        if sn_author:
                            author, headline = sn_author, sn_headline or headline
                    if author or headline:
                        author_hits += 1
                    if not is_likely_hiring_poster(headline):
                        rejected["seeker_poster"] = rejected.get("seeker_poster", 0) + 1
                        log.debug("linkedin.lead.rejected_seeker_poster", headline=headline[:60])
                        continue

                    hiring_text = post_content(text, headline)
                    emails = extract_recruiter_emails(hiring_text)
                    if not emails:
                        _drop("no_email_in_post_body", text)
                        continue
                    if is_spam_or_unpaid(hiring_text):
                        _drop("spam_or_unpaid", text)
                        continue
                    if is_abroad_onsite(hiring_text):
                        _drop("abroad_onsite", text)
                        continue

                    if IIT_ONLY_REJECT_REGEX.search(hiring_text):
                        _drop("iit_only", text)
                        continue

                    if ZERO_TECH_REJECT_REGEX.search(hiring_text):
                        _drop("zero_tech", text)
                        continue

                    if not is_experience_match(hiring_text):
                        _drop("experience", text)
                        continue

                    role = classify_role(hiring_text)
                    if not role:
                        _drop("role_or_tech", text)
                        continue

                    if not has_profile_skill_overlap(hiring_text, required_skills):
                        _drop("no_skill_overlap", text)
                        continue

                    post_url = ""
                    try:
                        link_loc = container.locator(
                            "a[href*='/feed/update/urn:li:'], a[href*='urn:li:activity'], a[href*='activity-'], a[href*='/posts/']"
                        ).first
                        if await link_loc.count():
                            href = await link_loc.get_attribute("href") or ""
                            if href:
                                post_url = href if href.startswith("http") else f"https://www.linkedin.com{href}"
                    except Exception:
                        post_url = ""

                    if not post_url:
                        post_url = await self._copy_post_permalink(page, container)

                    fn = resolve_recipient_first_name(author, emails[0] if emails else "")

                    results.append({
                        "text": hiring_text, "source_text": text, "role": role, "emails": emails,
                        "post_url": post_url, "author": author,
                        "headline": headline,
                        "company": extract_company(hiring_text, ""),
                        "first_name": fn,
                        "post_age_hours": parse_post_age_hours(text),
                    })

                log.info("linkedin.hunt_completed", total_posts_read=len(containers), qualified_leads=len(results), rejected=rejected, author_hits=author_hits, author_gated=author_gated)

            except CookieExpiredError:
                raise
            except Exception as exc:
                err_msg = str(exc)
                if "ERR_TOO_MANY_REDIRECTS" in err_msg or "ERR_HTTP_RESPONSE_CODE_FAILURE" in err_msg:
                    log.error("linkedin.blocked", detail="LinkedIn blocked the request. Try increasing delay between searches.")
                else:
                    log.error("linkedin.scrape_crashed", error=err_msg[:200])
            finally:
                # The driver itself may be dead (OOM/closed browser) — a
                # throwing close would mask the real error and kill the run.
                try:
                    await context.close()
                except Exception:
                    pass

        return results
