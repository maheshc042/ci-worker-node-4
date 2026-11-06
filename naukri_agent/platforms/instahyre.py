"""
Instahyre Platform Implementation (Pagination & Modal-Swiper Engine).
"""
from __future__ import annotations

import asyncio
import json
import re
import time as _time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    first_visible,
    human_pause,
    human_type,
    safe_text,
)
from ..config import AgentConfig, JobProfile, NaukriAccount, get_settings
from ..core.models import ApplicationStatus, ApplyOutcome, FilterDecision, Job, SkipReason
from ..core.run_policy import RunPolicy
from ..logging_setup import get_logger
from ..naukri.parser import extract_jd_experience
from .base import BaseJobPlatform

log = get_logger(__name__)

_APPLIED_CARD_MARKERS = [
    "button:text-is('Applied')",
    "span.applied-badge:text-is('Applied')",
    ".application-status-box .status-label:text-is('Applied')",
]
_SKILL_INPUTS = [
    "#skills-drop-select-job-search-selectized", "#skills-selectized",
    "input[placeholder*='skills' i]",
]
_SKILL_TAGS = (
    ".selectize-control.skills-drop-select-job-search .selectize-input .item[data-value], "
    "#job-search-section .selectize-control .selectize-input .item[data-value]"
)
_EXPERIENCE_SLIDER = ".experience-filter [ng-model='search.sliderExperience']"


def _ai_tenure_shortfall(description: str, config: AgentConfig | None) -> FilterDecision | None:
    """Compare an explicit AI-domain tenure requirement with approved AI years."""
    if config is None:
        return None
    required_text = re.split(r"preferred qualifications|nice to have|good to have", description, flags=re.I)[0]
    requirement = re.search(
        r"(\d+(?:\.\d+)?)(?:\s*[-–]\s*\d+(?:\.\d+)?|\s*\+)?\s*years?\s+"
        r"(?:of\s+)?(?:relevant\s+|hands-on\s+)?experience\s+in\s+"
        r"(generative ai|large language models?|llms?)\b", required_text, re.I,
    )
    if not requirement:
        return None
    skill = "generative ai" if requirement[2].lower() == "generative ai" else "llm"
    verified = config.experience.skills.get(skill)
    minimum = float(requirement[1])
    if verified is not None and float(verified) < minimum:
        return FilterDecision(False, SkipReason.FILTER_EXPERIENCE,
                              f"Requires {minimum:g} years in {skill}; configured experience is {verified:g} years")
    return None


class InstahyrePlatform(BaseJobPlatform):
    def __init__(
        self,
        page: Page,
        account: NaukriAccount,
        artifacts: ArtifactStore,
        policy: RunPolicy,
        config: AgentConfig | None = None,
        metrics: Any | None = None,
    ):
        super().__init__(page, account.key, policy)
        self.account = account
        self.artifacts = artifacts
        self.policy = policy
        self.config = config
        self._metrics = metrics
        self._current_view: str = "recommended"
        self._current_profile: JobProfile | None = None
        # Pages actually rendered during fetch. The apply phase may ONLY
        # search these: run 366 proved the pagination control can advertise
        # phantom pages (11-20 of a 10-page feed), burning ~40s per job on
        # cards that were never scraped. Unknown pages => STALE, fast.
        self._visited_pages: set[int] = set()
        self.logged_out_markers = [
            "input[type='email']",
            "input[name='email']",
            "input[type='password']",
            "form[action*='login']",
        ]

    @property
    def platform_name(self) -> str:
        return "instahyre"

    async def ensure_logged_in(self) -> bool:
        self._current_view = "recommended"
        await self.page.goto(
            "https://www.instahyre.com/candidate/opportunities/?matching=true", wait_until="domcontentloaded"
        )
        await human_pause(1000, 2000)

        email_input = await first_visible(
            self.page, ["input[type='email']", "input[name='email']"], timeout_ms=3000
        )
        pass_input = await first_visible(
            self.page, ["input[type='password']", "input[name='password']"], timeout_ms=3000
        )

        authenticated = await first_visible(
            self.page,
            [
                "a[href*='/candidate/profile']",
                "a[href*='/candidate/opportunities']",
                "div.employer-row",
                "button:has-text('Logout')",
            ],
            timeout_ms=3000,
        )
        if authenticated:
            log.info("instahyre.auth.session_reused")
            return True
        if not email_input or not pass_input:
            # Check if we can wait for manual login in headed mode
            log.warning("instahyre.auth.waiting_for_manual_login", msg="Please log in to Instahyre in the open browser window if prompted...")
            try:
                authenticated = await first_visible(
                    self.page,
                    [
                        "a[href*='/candidate/profile']",
                        "a[href*='/candidate/opportunities']",
                        "div.employer-row",
                        "button:has-text('Logout')",
                        "#opportunities",
                    ],
                    timeout_ms=30000,
                )
                if authenticated:
                    log.info("instahyre.auth.manual_login_success")
                    return True
            except Exception:
                pass
            log.error("instahyre.auth.uncertain_state", url=self.page.url)
            return False

        log.info("instahyre.auth.login_start")

        settings = get_settings()
        email = (settings.instahyre_email or self.account.email).strip()
        password = (settings.instahyre_password or self.account.password).strip()

        await human_type(email_input, email)
        await human_type(pass_input, password)
        await human_pause(300, 600)

        submit = await first_visible(self.page, ["button:has-text('Login')", "button[type='submit']"])
        if submit:
            await submit.click()
            await human_pause(2000, 4000)

            authenticated = await first_visible(
                self.page,
                [
                    "a[href*='/candidate/profile']",
                    "a[href*='/candidate/opportunities']",
                    "div.employer-row",
                    "button:has-text('Logout')",
                ],
                timeout_ms=5000,
            )
            if authenticated:
                log.info("instahyre.auth.login_success")
                return True

        log.error("instahyre.auth.login_failed")
        return False

    async def _clear_skill_tags(self) -> int:
        """Remove every active skill tag; return how many remain.

        Loops until clean (or 3 passes): single-pass clearing leaves fossils
        like the ever-present SDET tag, which then skew every filtered search.
        """
        # Selectize collapses tags after the first four. Its native select-all
        # gesture clears hidden tags too; verify the remaining real items.
        skills_input = await first_visible(self.page, _SKILL_INPUTS, timeout_ms=1000)
        if skills_input is not None:
            try:
                await skills_input.fill("")
                await skills_input.focus()
                await skills_input.press("Control+A")
                await skills_input.press("Backspace")
                await human_pause(100, 250)
            except Exception:
                pass
        remaining = 0
        for _ in range(3):
            try:
                remove_buttons = await self.page.locator(
                    ", ".join(f"{selector.strip()} a.remove" for selector in _SKILL_TAGS.split(","))
                ).all()
                for rem in remove_buttons:
                    try:
                        await rem.click()
                        await human_pause(100, 250)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                tags = await self.page.locator(_SKILL_TAGS).all_inner_texts()
                remaining = len([t for t in tags if t.strip()])
            except Exception:
                remaining = 0
            if remaining == 0:
                break
        return remaining

    async def _select_skill(self, _tags_text, skill: str) -> None:
        """Select the visible exact suggestion from the current skill input."""
        try:
            await self.page.keyboard.press("Escape")
            await human_pause(100, 200)
        except Exception:
            pass
        skills_input = await first_visible(
            self.page,
            _SKILL_INPUTS,
            timeout_ms=2500,
        )
        if skills_input is None:
            raise RuntimeError("skill input gone")
        # The input collapses to a few px once tags exist and is routinely
        # covered by the tag/flyout layer (runs 396-403: every click() dies
        # in actionability). focus() skips coverage checks — type after it.
        try:
            await skills_input.click(timeout=2000)
        except Exception:
            await skills_input.focus()
        await human_pause(150, 300)
        await skills_input.fill("")
        await skills_input.press_sequentially(skill, delay=45)

        # Selectize keeps hidden suggestions attached, including results from
        # the previous skill. Wait for the exact current suggestion to appear.
        options = self.page.locator(
            ".selectize-dropdown:visible .selectize-option, .selectize-dropdown:visible .option"
        )
        exact_option = options.filter(
            has_text=re.compile(rf"^\s*{re.escape(skill)}\s*$", re.IGNORECASE)
        ).first
        try:
            await exact_option.click(timeout=3000)
        except PlaywrightTimeoutError:
            # The current UI supports free-text skill/title keywords through
            # an explicit Add option. Select that control, never inject tags.
            create = self.page.locator(".selectize-dropdown:visible .create").filter(
                has_text=re.compile(rf"^\s*Add\s+{re.escape(skill)}\s*(?:…|\.\.\.)?\s*$", re.IGNORECASE)
            )
            if await create.count() != 1:
                log.warning("instahyre.filters.skill_missing", skill=skill)
                return
            await create.click(timeout=2000)
            log.info("instahyre.filters.keyword_selected", keyword=skill)
        await human_pause(250, 450)
        if skill.lower() not in await _tags_text():
            log.warning("instahyre.filters.skill_not_applied", skill=skill)

    async def _apply_ui_filters(self, profile: JobProfile) -> None:
        """Interactively opens and sets the filter UI on Instahyre opportunities page."""
        log.info("instahyre.filters.applying_ui")
        await self._dismiss_modals()


        # 1. Expand "Search other jobs" section ONLY if currently hidden
        filters_panel = self.page.locator("div.show-filters:not(.ng-hide)")
        if not await filters_panel.is_visible():
            search_heading = await first_visible(
                self.page,
                [
                    ".job-search-heading",
                    "h6:has-text('Search other jobs')",
                    "#job-search-section h6",
                ],
                timeout_ms=2000,
            )
            if search_heading:
                try:
                    await search_heading.click()
                    await human_pause(400, 800)
                except Exception:
                    pass


        # 2. Fill Skills Input (#skills-selectized) with Node.js (with dot) and Python
        skills_input = await first_visible(
            self.page,
            _SKILL_INPUTS,
            timeout_ms=2500,
        )

        # Target skills: Configured skill set (Python, Node.js, React.js, TypeScript, FastAPI, Next.js, Generative AI, LLMs, AWS)
        inst_cfg = getattr(self.config, "instahyre", None) if self.config else None
        target_skills = (
            inst_cfg.skills
            if inst_cfg and inst_cfg.skills
            else [
                "Python",
                "Node.js",
                "React.js",
                "TypeScript",
                "FastAPI",
                "Next.js",
                "Generative AI",
                "LLMs",
                "AWS",
            ]
        )

        if skills_input:
            # Clear any pre-existing stale skill tags (verified loop — a
            # single pass leaves fossils like the ever-present SDET tag).
            cleared_left = await self._clear_skill_tags()
            if cleared_left:
                log.warning("instahyre.filters.clear_incomplete", remaining=cleared_left)

            # Instahyre enforces MAX 15 skill tags: anything past #15 is
            # silently ignored (run 366). Truncate loudly, never silently.
            if len(target_skills) > 15:
                log.warning(
                    "instahyre.filters.skills_truncated",
                    configured=len(target_skills),
                    dropped=target_skills[15:],
                )
                target_skills = target_skills[:15]

            async def _tags_text() -> str:
                try:
                    tags = await self.page.locator(_SKILL_TAGS).all_inner_texts()
                    return " ".join(tags).lower()
                except Exception:
                    return ""

            # Skill loop: real UI interaction only. (A scope-injection path
            # was tried in runs 412-417: tags displayed but the backend
            # resolved nothing — display tags without matching option
            # identities filter zero jobs. Removed; only UI-created tags
            # count as coverage.)
            for skill in target_skills:
                if skill.lower() in await _tags_text():
                    continue
                # Hard time-box per skill: on the new UI the selectize input
                # intermittently stops accepting fills (run 396: 14 skills ×
                # ~20s of hung fills = 5 silent minutes, 1/15 tags landed).
                # One stuck skill must never eat the run budget.
                try:
                    await asyncio.wait_for(
                        self._select_skill(_tags_text, skill),
                        timeout=15,
                    )
                except asyncio.TimeoutError:
                    log.warning("instahyre.filters.skill_timeboxed", skill=skill)
                except Exception as exc:
                    log.debug("instahyre.filters.skill_select_failed", skill=skill, error=str(exc))

            # Last resort: unfiltered feed. When the skill widget is dead
            # (runs 396+: 0 options render, nothing lands), a skill-filtered
            # search returns near-zero jobs while an unfiltered one returns
            # the full feed — and the planner/ranker already enforce precision
            # downstream (run 368: 111 scraped -> 17 applied with no skill UI
            # dependence). Never trade a dead filter for zero coverage.
            try:
                landed_text = await _tags_text()
                landed_n = sum(1 for s in target_skills if s.lower() in landed_text)
            except Exception:
                landed_n = 0
            if landed_n < max(1, len(target_skills) // 2):
                left = await self._clear_skill_tags()
                log.warning(
                    "instahyre.filters.unfiltered_fallback",
                    landed=landed_n,
                    total=len(target_skills),
                    tags_cleared=left == 0,
                )

        # 3. Fill Experience Input (#years)
        exp_input = await first_visible(
            self.page,
            [
                "input#years",
                "input[name='years']",
                "input[placeholder*='e.g. 4' i]",
            ],
            timeout_ms=2000,
        )
        if inst_cfg and inst_cfg.experience_years is not None:
            exp_val = str(int(inst_cfg.experience_years))
        elif profile.experience_years > 0:
            exp_val = str(int(profile.experience_years))
        elif self.config and getattr(self.config, "experience", None) and self.config.experience.total_years:
            exp_val = str(int(self.config.experience.total_years))
        elif profile.filters and profile.filters.experience and profile.filters.experience.max_years < 50:
            exp_val = str(int(profile.filters.experience.max_years))
        else:
            # No configured tenure: leave the filter untouched rather than
            # searching with an invented number of years.
            exp_val = ""

        legacy_experience_set = False
        if exp_input and exp_val:
            try:
                await exp_input.click()
                await human_pause(200, 400)
                await exp_input.fill("")
                await human_type(exp_input, exp_val)
                await human_pause(400, 800)
                legacy_experience_set = await exp_input.input_value(timeout=1500) == exp_val
            except Exception as exc:
                log.debug("instahyre.filters.exp_select_failed", error=str(exc))

        # 4. Click the search submit button. NOTE Sep 2026: Instahyre
        # renamed it from button#show-results ("Show results") to
        # button.skills-search-btn ("Search") — the old selectors silently
        # match nothing and the search never fires (runs 396+: 0 jobs with
        # zero errors). New markup first, legacy as fallback.
        submit_filter = await first_visible(
            self.page,
            [
                "button.skills-search-btn",
                "#job-search-section button:has-text('Search')",
                "button#show-results",
                "button.show-results",
                "button:has-text('Show results')",
            ],
            timeout_ms=2500,
        )
        if submit_filter:
            try:
                await submit_filter.click()
                await human_pause(1500, 2500)
                try:
                    await self.page.wait_for_selector(
                        "div.employer-row div.company-name, div.employer-job-name",
                        timeout=12000,
                    )
                except Exception:
                    pass
                await human_pause(1000, 2000)
            except Exception:
                await self.page.keyboard.press("Enter")
                await human_pause(3000, 5000)



        if exp_val:
            slider = await first_visible(self.page, [_EXPERIENCE_SLIDER], timeout_ms=2500)
            if slider is not None:
                if not await self._set_experience_slider(slider, int(exp_val)):
                    log.warning("instahyre.filters.experience_slider_unverified", requested_years=exp_val)
            elif not legacy_experience_set:
                log.warning("instahyre.filters.experience_control_missing", requested_years=exp_val,
                            detail="Search tenure not set; card and detail eligibility checks remain required")

        # Dump search results HTML for exact inspection
        try:
            dump_path = self.artifacts.dir / "instahyre-search-results.html"
            content = await self.page.content()
            dump_path.write_text(content, encoding="utf-8")
            log.info("instahyre.search_results_html_saved", path=str(dump_path))
        except Exception as exc:
            log.debug("instahyre.search_results_dump_failed", error=str(exc))

        # Self-diagnostic: if NO skill tags are active after the filter pass,
        # the feed is unfiltered and the plan stage will mass-reject on title
        # (run 360: 117 scraped, 0 eligible). Loud warning, not silent drift.
        try:
            active_tags = await self.page.locator(_SKILL_TAGS).all_inner_texts()
            active_tags = [t.strip() for t in active_tags if t.strip()]
        except Exception:
            active_tags = []
        if not active_tags:
            log.warning(
                "instahyre.filters.skills_missing",
                detail="No skill tags active after UI filter pass — feed is UNFILTERED, expect mass title rejects downstream",
            )
        else:
            log.info("instahyre.filters.skills_active", count=len(active_tags), tags=active_tags[:18])
            # Fracture check: multi-word skills sometimes split into fragments
            # ("Generative AI" -> "Gener" + "ative AI" in run 366) that match
            # nothing on their side. Loud when expected skills are absent.
            try:
                wanted = list(getattr(getattr(self.config, "instahyre", None), "skills", []) or [])
                joined = " ".join(active_tags).lower()
                missing = [s for s in wanted if s and s.lower() not in joined]
                if missing:
                    log.warning("instahyre.filters.skills_fractured", missing=missing)
            except (AttributeError, TypeError, ValueError) as exc:
                log.debug("instahyre.filters.fracture_check_failed", error=str(exc))


    async def _set_experience_slider(self, slider: Locator, years: int) -> bool:
        """Move the native handle and verify the resulting search and value."""
        try:
            bounds = await slider.evaluate(
                """element => {
                    const scope = window.angular?.element(element).isolateScope();
                    return scope ? {min: scope.min, max: scope.max, value: scope.ngModel} : null;
                }""",
                timeout=2500,
            )
            if not bounds or not bounds["min"] <= years < bounds["max"]:
                return False
            label = slider.locator(".slider-value")
            expected = {str(years)}
            if years == bounds["max"] - 1:
                expected.add(f"{years}+")
            if bounds["value"] == years:
                return await safe_text(label) in expected

            handle = slider.locator(".slider-handle")
            await handle.scroll_into_view_if_needed(timeout=2500)
            thumb = await handle.bounding_box(timeout=2500)
            track = await slider.locator(".slider-track").bounding_box(timeout=2500)
            if not thumb or not track or track["width"] <= thumb["width"]:
                return False
            fraction = (years - bounds["min"]) / (bounds["max"] - bounds["min"])
            target_x = track["x"] + thumb["width"] / 2 + (track["width"] - thumb["width"]) * fraction
            y = thumb["y"] + thumb["height"] / 2

            def is_filtered_search(response) -> bool:
                url = urlsplit(response.url)
                return (
                    url.hostname == "www.instahyre.com"
                    and url.path.rstrip("/") == "/api/v1/job_search"
                    and parse_qs(url.query).get("years") == [str(years)]
                )

            async with self.page.expect_response(
                is_filtered_search, timeout=8000,
            ) as search:
                await self.page.mouse.move(thumb["x"] + thumb["width"] / 2, y)
                await self.page.mouse.down()
                try:
                    await self.page.mouse.move(target_x, y, steps=12)
                finally:
                    await self.page.mouse.up()
            response = await search.value
            if not response.ok:
                return False
            await response.body()
            if await safe_text(label) in expected:
                log.info("instahyre.filters.experience_slider_verified", years=years)
                return True
        except (PlaywrightError, TypeError, KeyError) as exc:
            log.debug("instahyre.filters.experience_slider_failed", error_type=type(exc).__name__)
        return False

    async def _dismiss_modals(self) -> None:
        """Dismiss popup modals (e.g. 'Are you looking for a job actively?')."""
        try:
            # 1. "Are you looking for a job actively?" Modal
            # (scoped to the modal — a bare button:has-text('Save') could hit
            # unrelated Save controls anywhere on the page)
            active_modal_btn = await first_visible(
                self.page,
                [
                    "div.candidate-active-check-modal button.btn-success",
                    "div.candidate-active-check-modal button:has-text('Save')",
                    "div.candidate-active-check-modal button:has-text('Cancel')",
                ],
                timeout_ms=1500,
            )
            if active_modal_btn:
                log.info("instahyre.modal.dismissing_active_check")
                await active_modal_btn.click()
                await human_pause(1000, 1500)

            # 2. Post-apply bulk-apply modal ("Want to apply to other similar
            #    jobs?") — its backdrop intercepts clicks on the next card if
            #    left open (verified class names in feed DOM).
            bulk_cancel = await first_visible(
                self.page,
                [
                    "div.candidate-apply-all-modal button.back-button-modal-close",
                    "div.candidate-apply-all-modal button:has-text('Cancel')",
                ],
                timeout_ms=800,
            )
            if bulk_cancel:
                log.info("instahyre.modal.dismissing_bulk_apply")
                await bulk_cancel.click()
                await human_pause(600, 1000)

            # 3. Premium / Share / WhatsApp Modals
            premium_close = await first_visible(
                self.page,
                [
                    "a:has-text('No thanks')",
                    "div.application-modal-close",
                    ".modal-close",
                ],
                timeout_ms=1000,
            )
            if premium_close:
                log.info("instahyre.modal.dismissing_premium_modal")
                await premium_close.click()
                await human_pause(800, 1200)

            # 4. Error / Toast Message Popups (#messages)
            error_close = await first_visible(
                self.page,
                [
                    "#messages button",
                    "#messages button.close",
                    "#messages div.alert button",
                    "div.toast-close-button",
                ],
                timeout_ms=800,
            )
            if error_close:
                log.info("instahyre.modal.dismissing_error_toast")
                await error_close.click()
                await human_pause(400, 800)

        except Exception as exc:
            log.debug("instahyre.dismiss_modals_error", error=str(exc))

    async def _scrape_page_cards(
        self,
        tab_name: str,
        page_num: int,
        exclude_job_ids: set[str],
        seen_ids: set[str],
        jobs: list[Job],
        max_jobs: int,
    ) -> int:
        card_selector = (
            "div.employer-row, div.opportunity-box, div.opportunity-card, "
            "div.job-card, div[class*='employer-row']"
        )
        # Page 1 gets a long settle budget: a 16-skill backend search can
        # take 20s+ (run 412 saw 0 cards at 8s while FETCHING was still in
        # flight, then declared last page). Later pages keep the short budget.
        budget_s = 30 if page_num == 1 else 8
        deadline = _time.monotonic() + budget_s
        while True:
            try:
                await self.page.wait_for_selector(card_selector, timeout=3000)
                break
            except Exception:
                pass
            if _time.monotonic() >= deadline:
                break
            await human_pause(1500, 2500)

        cards = await self.page.locator(card_selector).all()
        if not cards:
            return 0

        added_this_page = 0
        for index, card in enumerate(cards, start=len(jobs) + 1):
            if len(jobs) >= max_jobs:
                break
            try:
                raw_name = ""
                name_loc = card.locator("div.employer-job-name .company-name").first
                try:
                    if await name_loc.count() > 0:
                        raw_name = (await name_loc.inner_text(timeout=2000)).strip()
                except Exception:
                    pass

                if not raw_name:
                    title_attr_loc = card.locator("div.employer-job-name").first
                    try:
                        raw_name = (await title_attr_loc.get_attribute("title") or "").strip()
                    except Exception:
                        pass

                if not raw_name:
                    continue

                if " - " in raw_name:
                    parts = raw_name.split(" - ", 1)
                    company = parts[0].strip()
                    title = parts[1].strip()
                else:
                    company = raw_name
                    title = raw_name

                if not title:
                    continue

                tags: list[str] = []
                seen_tags: set[str] = set()
                for t in await card.locator("ul.tags li").all():
                    txt = (await safe_text(t)).strip()
                    if txt and not txt.startswith("+") and txt.lower() not in seen_tags:
                        seen_tags.add(txt.lower())
                        tags.append(txt)

                location = ""
                loc_node = card.locator(".employer-chips-location, div.employer-locations span").first
                if await loc_node.count() > 0:
                    location = (await safe_text(loc_node)).strip()

                note_text = ""
                note_loc = card.locator("div.employer-notes").first
                if await note_loc.count() > 0:
                    note_text = (await safe_text(note_loc)).strip()

                url = "https://www.instahyre.com/candidate/opportunities/"
                # Real opportunity id from the card's AngularJS scope (same
                # `opp` object the Apply button consumes). Falls back to the
                # title+company hash only when Angular is not bootstrapped.
                opp_id = ""
                try:
                    opp_id = (await card.evaluate("""(el) => {
                        try {
                            if (!window.angular) return '';
                            let s = null;
                            try { s = window.angular.element(el).scope(); } catch (e) { return ''; }
                            for (let depth = 0; depth < 6 && s; depth++) {
                                const o = s.opp || s.selectedOpp || s.opportunity || null;
                                const v = o && (o.job?.id || o.id || o.opportunity_id || o.job_id || o._id || o.opp_id || o.opportunityId);
                                if (v) return String(v);
                                s = s.$parent;
                            }
                            return '';
                        } catch (e) { return ''; }
                    }""") or "").strip()
                except Exception as exc:
                    log.debug("instahyre.fetch.opp_id_failed", error=str(exc))
                if not opp_id:
                    native = card.locator("ul[id^='job-skills-']").first
                    native_id = (await native.get_attribute("id") or "") if await native.count() else ""
                    match_id = re.fullmatch(r"job-skills-(\d+)", native_id)
                    if match_id:
                        opp_id = match_id[1]
                if opp_id:
                    self._opp_ids_resolved = getattr(self, "_opp_ids_resolved", 0) + 1
                legacy_id = f"instahyre-{Job.stable_id(url, title, company)}"
                job_id = f"instahyre-{opp_id}" if opp_id else legacy_id

                # Preserve dedupe against runs that stored the old title hash.
                if job_id in exclude_job_ids or legacy_id in exclude_job_ids or job_id in seen_ids:
                    continue

                seen_ids.add(job_id)
                jobs.append(
                    Job(
                        job_id=job_id,
                        title=title,
                        company=company,
                        location=location,
                        url=url,
                        description=note_text,
                        tags=tags,
                        recommendation_tab=f"{tab_name}_{page_num}",
                        recommendation_position=index,
                        platform="instahyre",
                    )
                )
                added_this_page += 1
            except Exception as exc:
                log.debug("instahyre.fetch.parse_error", error=str(exc))
                continue

        return added_this_page

    async def _paginate_and_collect(
        self,
        tab_name: str,
        max_pages: int,
        exclude_job_ids: set[str],
        seen_ids: set[str],
        jobs: list[Job],
        max_jobs: int,
    ) -> None:
        page_num = 1
        while page_num <= max_pages and len(jobs) < max_jobs:
            log.info("instahyre.fetch.scraping_page", tab=tab_name, page_num=page_num, total_jobs=len(jobs))
            self._visited_pages.add(page_num)
            added = await self._scrape_page_cards(tab_name, page_num, exclude_job_ids, seen_ids, jobs, max_jobs)
            if added == 0 and page_num > 1:
                break

            if len(jobs) >= max_jobs or page_num >= max_pages:
                break

            has_next = False
            next_li = self.page.locator("div.pagination li:has-text('Next'):not(.hidden)").first
            try:
                has_next = await next_li.count() > 0 and await next_li.is_visible()
            except Exception:
                pass

            if not has_next:
                next_num = self.page.locator(f"div.pagination li:text-is('{page_num + 1}')").first
                try:
                    has_next = await next_num.count() > 0 and await next_num.is_visible()
                    if has_next:
                        next_li = next_num
                except Exception:
                    pass

            if has_next:
                await next_li.click()
                await human_pause(1500, 2500)
                page_num += 1
            else:
                log.info("instahyre.fetch.last_page_reached", tab=tab_name, pages=page_num)
                break

        # Return to page 1
        try:
            pagination_div = self.page.locator("div.pagination").first
            if await pagination_div.count() > 0:
                await pagination_div.scroll_into_view_if_needed()
                first_page = pagination_div.locator("li").filter(has_text=re.compile(r"^\s*1\s*$")).first
                if await first_page.count() > 0:
                    await first_page.click(force=True)
                    await human_pause(1200, 2000)
                    await self.page.evaluate("window.scrollTo(0, 0)")
        except Exception as exc:
            log.debug("instahyre.fetch.return_to_p1_failed", error=str(exc))

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        log.info("instahyre.fetch.start", profile=profile.name)
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        feed_url = "https://www.instahyre.com/candidate/opportunities/"
        target_applies = profile.platform_limits.get(self.platform_name, 150)
        # Scrape a generous pool (2x apply limit) directly from UI search filters
        max_scrape = max(250, int(target_applies * 2))
        self._current_profile = profile
        self._current_view = "search"

        try:
            await self.page.goto(feed_url, wait_until="domcontentloaded")
            await human_pause(2000, 3000)
            await self._dismiss_modals()

            # Directly apply targeted UI Search Filters (Node.js/Python + Exp)
            log.info(
                "instahyre.fetch.direct_search_filters_start",
                target_limit=max_scrape,
                profile=profile.name,
            )
            # Whole filter pass is time-boxed: a degraded filter UI must yield
            # a partial filter set, never a silent multi-minute stall that
            # eats the run budget (runs 395/396).
            try:
                await asyncio.wait_for(self._apply_ui_filters(profile), timeout=180)
            except asyncio.TimeoutError:
                log.warning("instahyre.filters.pass_timeboxed")
            self._current_view = "search"

            # Paginate through filtered search results directly
            inst_cfg = getattr(self.config, "instahyre", None) if self.config else None
            fetch_max_pages = (
                int(inst_cfg.max_pages)
                if inst_cfg and getattr(inst_cfg, "max_pages", None)
                else 3
            )
            await self._paginate_and_collect(
                tab_name="search_page",
                max_pages=fetch_max_pages,
                exclude_job_ids=exclude_job_ids,
                seen_ids=seen_ids,
                jobs=jobs,
                max_jobs=max_scrape,
            )
            log.info("instahyre.fetch.direct_search_filters_done", total_gathered=len(jobs))

        except Exception as exc:
            log.warning("instahyre.fetch.error", url=feed_url, error=str(exc))

        log.info(
            "instahyre.fetch.done",
            count=len(jobs),
            current_view=self._current_view,
            real_opp_ids=getattr(self, "_opp_ids_resolved", 0),
        )
        return jobs

    @staticmethod
    async def _click_best_effort(loc) -> bool:
        """Click-or-JS-click without ever raising: UI affordances are best
        effort by nature; callers decide what a missed click means."""
        try:
            await loc.click(timeout=3000)
            return True
        except Exception as exc:
            log.debug("instahyre.pagination.click_retry_js", error=str(exc)[:80])
        try:
            await loc.evaluate("el => el.click()")
            return True
        except Exception:
            log.debug("instahyre.pagination.click_failed")
            return False

    async def _switch_to_page(self, target_page: int) -> bool:
        """Safely switch to target page in Instahyre search/reco feed using direct pagination element interaction."""
        try:
            pagination_div = self.page.locator("div.pagination").first
            if await pagination_div.count() == 0:
                return False

            # Check if current page is already target_page
            active_btn = pagination_div.locator("li.active").first
            if await active_btn.count() > 0:
                cur_text = (await safe_text(active_btn)).strip()
                if cur_text == str(target_page):
                    return True

            # Scroll pagination into view
            await pagination_div.scroll_into_view_if_needed()
            await human_pause(200, 400)

            # Click + verify, up to 2 attempts: a single click can land while
            # Angular is still re-rendering (run 366: 10->3 silently missed,
            # then 40s burned hunting phantom pages 11-20).
            for attempt in (1, 2):
                # Locate the exact page number li
                page_btn = pagination_div.locator("li").filter(has_text=re.compile(rf"^\s*{target_page}\s*$")).first
                clicked = await self._click_best_effort(page_btn) if await page_btn.count() > 0 else False

                if not clicked:
                    # If target page number is beyond current visible numbers, click Next
                    next_btn = pagination_div.locator("li:has-text('Next'):not(.hidden)").first
                    if await next_btn.count() > 0:
                        clicked = await self._click_best_effort(next_btn)

                if not clicked:
                    return False

                # Wait for active page indicator to update
                active_target = pagination_div.locator("li.active").filter(has_text=re.compile(rf"^\s*{target_page}\s*$"))
                try:
                    await active_target.wait_for(state="visible", timeout=5000)
                except Exception:
                    await human_pause(1200, 2000)

                # Wait for cards to be rendered on the feed
                try:
                    await self.page.locator("div.employer-row").first.wait_for(state="visible", timeout=4000)
                except Exception:
                    pass

                await self.page.evaluate("window.scrollTo(0, 0)")
                await human_pause(400, 800)

                # Verify that the active page is indeed target_page
                now_active = pagination_div.locator("li.active").first
                if await now_active.count() > 0:
                    now_text = (await safe_text(now_active)).strip()
                    if now_text == str(target_page):
                        self._visited_pages.add(target_page)
                        return True
                log.debug("instahyre.pagination.switch_retry", target_page=target_page, attempt=attempt)
                await human_pause(800, 1200)
        except Exception as exc:
            log.debug("instahyre.pagination.switch_failed", target_page=target_page, error=str(exc))
        return False

    async def _ensure_modal_closed(self) -> None:
        """Forces the Instahyre carousel modal to close cleanly and removes any backdrops without destroying DOM templates."""
        # 1. Native AngularJS Scope Close Trigger on employerProfileModalCtrl
        try:
            await self.page.evaluate("""() => {
                const modalCtrl = document.querySelector('[ng-controller="employerProfileModalCtrl"]');
                if (modalCtrl && window.angular) {
                    const scope = window.angular.element(modalCtrl).scope();
                    if (scope) {
                        if (typeof scope.closeApplyModal === 'function') {
                            scope.closeApplyModal();
                        }
                        if (scope.bulkParams) {
                            scope.bulkParams.showApplyModal = false;
                        }
                        scope.$evalAsync();
                    }
                }
            }""")
        except Exception:
            pass

        modal = await first_visible(
            self.page,
            ["div.modal-content", "div#employer-profile-modal", "div[class*='modal']"],
            timeout_ms=1000,
        )
        if modal is not None:
            close_btn = await first_visible(
                self.page,
                ["button.close", "span.close", "button[aria-label='Close']", ".modal-header button", ".application-modal-backdrop"],
                timeout_ms=1000,
            )
            if close_btn:
                try:
                    await close_btn.click()
                except Exception:
                    pass
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            await human_pause(300, 600)

        # Force remove ONLY promotional overlays and stray backdrops (NEVER remove .application-modal!)
        try:
            await self.page.evaluate("""() => {
                document.querySelectorAll('.modal-backdrop, #go-premium-modal, #refer, #follow-premium-modal').forEach(el => el.remove());
                document.body.classList.remove('modal-open');
            }""")
        except Exception:
            pass

    async def _find_target_card(self, job: Job, page: Page | None = None):
        page = page if page is not None else self.page
        expected = " ".join(f"{job.company} - {job.title}".split())
        name = page.locator("div.employer-job-name .company-name").filter(
            has_text=re.compile(rf"^\s*{re.escape(expected)}\s*$", re.IGNORECASE)
        )
        titled = page.locator(f"div.employer-job-name[title={json.dumps(expected, ensure_ascii=False)}]")
        rows = page.locator("div.employer-row").filter(has=name.or_(titled))
        numeric_id = job.job_id.removeprefix("instahyre-")
        if numeric_id.isdecimal():
            rows = rows.filter(has=page.locator(f"ul[id='job-skills-{numeric_id}']"))
        return rows if await rows.count() == 1 else None

    async def _verify_target_applied(self, job: Job) -> bool:
        """Read a fresh, company-filtered Applied feed without changing search.

        Search cards can disappear after submitting. Neither disappearance,
        the global applied count nor an employer popup establishes success.
        """
        card = await self._find_target_card(job)
        if card is not None and await first_visible(card, _APPLIED_CARD_MARKERS, timeout_ms=1200):
            return True
        verification = await self.page.context.new_page()
        try:
            await verification.goto("https://www.instahyre.com/candidate/opportunities/?matching=true",
                                    wait_until="domcontentloaded", timeout=25000)
            tab = verification.locator(".interest_filters .tab").filter(
                has_text=re.compile(r"^\s*Applied jobs\s*\d*\s*$")
            )
            await tab.wait_for(state="visible", timeout=8000)
            await tab.click(timeout=4000)
            company_input = verification.locator("#companies-selectized")
            await company_input.fill("", timeout=4000)
            await company_input.press_sequentially(job.company, delay=30)
            option = verification.locator(".selectize-dropdown:visible [data-selectable]").filter(
                has_text=re.compile(rf"^\s*{re.escape(job.company)}\s*(?:\(\d+\))?\s*$", re.I)
            )
            await option.wait_for(state="visible", timeout=5000)
            if await option.count() != 1:
                return False
            await option.click(timeout=4000)
            expected = " ".join(f"{job.company} - {job.title}".split())
            await verification.locator(
                f"div.employer-job-name[title={json.dumps(expected, ensure_ascii=False)}]"
            ).first.wait_for(state="visible", timeout=8000)
            selected = verification.locator(".interest_filters .tab.selected").filter(
                has_text=re.compile(r"^\s*Applied jobs\b")
            )
            if await selected.count() != 1:
                return False
            card = await self._find_target_card(job, verification)
            confirmed = card is not None and await first_visible(card, _APPLIED_CARD_MARKERS, timeout_ms=4000) is not None
            await self.artifacts.dump_html(verification, "instahyre-applied-feed-check", "verification", job.job_id)
            return confirmed
        except Exception as exc:
            log.warning("instahyre.verify.target_lookup_failed", job_id=job.job_id, error=str(exc)[:160])
            await self.artifacts.capture_failure(verification, "instahyre-receipt-lookup-failed", "verification", job.job_id)
            return False
        finally:
            await verification.close()

    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        log.info("instahyre.apply.start", job_id=job.job_id)
        t0 = _time.perf_counter()
        jt = None
        if self._metrics is not None:
            try:
                from ..core.runtime_metrics import JobTiming

                jt = JobTiming(job_id=job.job_id, title=job.title)
            except Exception:
                jt = None

        def _record_timing() -> None:
            if jt is not None and self._metrics is not None:
                jt.total_s = _time.perf_counter() - t0
                try:
                    self._metrics.job_timings.append(jt)
                except Exception:
                    pass

        # Reopen from the target card so the apply and confirmation share an identity.
        await self._ensure_modal_closed()
        modal = None
        if not modal:
            cur_view = getattr(self, "_current_view", "search")
            if cur_view != "search":
                log.info("instahyre.apply.ensuring_search_view", job_id=job.job_id)
                if self._current_profile:
                    await self._apply_ui_filters(self._current_profile)
                self._current_view = "search"

            tab = job.recommendation_tab or ""

            # 1. Determine target pagination page
            target_page = 1
            if tab and "_" in tab:
                try:
                    target_page = int(tab.split("_")[-1])
                except Exception:
                    target_page = 1

            pagination_div = self.page.locator("div.pagination").first
            if await pagination_div.count() > 0:
                active_page_loc = pagination_div.locator("li.active").first
                current_page_num = 1
                if await active_page_loc.count() > 0:
                    try:
                        current_page_num = int((await safe_text(active_page_loc)).strip())
                    except Exception:
                        current_page_num = 1

                if current_page_num != target_page:
                    log.info("instahyre.apply.switching_page", from_page=current_page_num, to_page=target_page)
                    switched = await self._switch_to_page(target_page)
                    if not switched:
                        log.warning("instahyre.apply.page_switch_unconfirmed", from_page=current_page_num, to_page=target_page)

            async def _find_card_on_current_view():
                return await self._find_target_card(job)

            card = await _find_card_on_current_view()

            # Dynamic pagination scan, bounded to pages ACTUALLY rendered
            # during fetch. Reading page numbers off the control is
            # unreliable (run 366: phantom pages 11-20 of a 10-page feed
            # burned ~40s per job on cards that were never scraped).
            if not card and await pagination_div.count() > 0:
                visited = sorted(p for p in self._visited_pages if p != target_page)
                if visited:
                    search_targets = visited[:6]
                else:
                    available_pages: list[int] = []
                    page_items = await pagination_div.locator("li").all()
                    for p_item in page_items:
                        txt = (await safe_text(p_item)).strip()
                        if txt.isdigit() and int(txt) <= 10:
                            available_pages.append(int(txt))
                    search_targets = [p for p in sorted(set(available_pages)) if p != target_page][:6]
                for search_p in search_targets:
                    log.info("instahyre.apply.searching_across_pages", page=search_p, job=f"{job.company} - {job.title}")
                    switched = await self._switch_to_page(search_p)
                    if switched:
                        card = await _find_card_on_current_view()
                        if card:
                            log.info("instahyre.apply.card_found_on_page", page=search_p, job=f"{job.company} - {job.title}")
                            break

            if not card:
                # Feed churn (filled/expired between collect and apply), not an
                # agent failure: skip without tripping the failure breaker.
                _record_timing()
                return ApplyOutcome(
                    status=ApplicationStatus.SKIPPED,
                    reason=SkipReason.STALE_JOB,
                    detail=f"Job card for {job.company} - {job.title} no longer on feed",
                )

            applied_markers = _APPLIED_CARD_MARKERS
            if await first_visible(card, applied_markers, timeout_ms=600) is not None:
                return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)

            opener = await first_visible(
                card, ["a#employer-profile-opportunity[ng-click='openApplyModal(opp)']"],
                timeout_ms=2500,
            )
            if opener is None:
                return ApplyOutcome(ApplicationStatus.FAILED, detail="Instahyre opportunity opener could not be verified")
            try:
                await opener.click(timeout=4000)
            except Exception as exc:
                return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Instahyre opportunity did not open: {exc!s}")

            modal = await first_visible(
                self.page,
                [".candidate-apply-modal > .application-modal-block", "#employer-profile-modal"],
                timeout_ms=8000,
            )
            if modal is None:
                _record_timing()
                return ApplyOutcome(ApplicationStatus.FAILED, detail="Modal did not open after clicking card")

        # Dump opened modal HTML for exact inspection
        try:
            dump_path = self.artifacts.dir / f"instahyre-modal-{job.job_id}.html"
            content = await self.page.content()
            dump_path.write_text(content, encoding="utf-8")
            log.info("instahyre.modal_html_saved", path=str(dump_path))
        except Exception as exc:
            log.debug("instahyre.modal_dump_failed", error=str(exc))

        description = await first_visible(
            modal, ["#job-description .job-description", "div.opportunity-description", "div.job-description"],
            timeout_ms=4500,
        )
        full_desc = await safe_text(description)
        if not full_desc or pre_submit_check is None:
            await self._ensure_modal_closed()
            return ApplyOutcome(ApplicationStatus.FAILED, detail="Instahyre detail eligibility could not be verified")

        job.description = full_desc
        minimum, maximum = extract_jd_experience(full_desc)
        if minimum is not None:
            job.min_experience, job.max_experience = minimum, maximum
            job.experience_imputed = False
        ai_gap = _ai_tenure_shortfall(full_desc, self.config)
        if ai_gap is not None:
            await self._ensure_modal_closed()
            _record_timing()
            return ApplyOutcome(ApplicationStatus.SKIPPED, reason=ai_gap.reason, detail=ai_gap.detail)
        decision = pre_submit_check(job)
        if not decision.passed:
            await self._ensure_modal_closed()
            _record_timing()
            return ApplyOutcome(ApplicationStatus.SKIPPED, reason=decision.reason, detail=decision.detail)

        external = await first_visible(
            modal, ["a:text-is('Apply on company site')", "button:text-is('Apply on company site')"],
            timeout_ms=1000,
        )
        if external is not None:
            href = (await external.get_attribute("href") or "").strip()
            target = urlsplit(href)
            await self._ensure_modal_closed()
            if target.scheme in {"http", "https"} and target.hostname and not (
                target.hostname == "instahyre.com" or target.hostname.endswith(".instahyre.com")
            ):
                return ApplyOutcome(ApplicationStatus.EXTERNAL, reason=SkipReason.EXTERNAL_APPLY, external_url=href)
            return ApplyOutcome(ApplicationStatus.NEEDS_REVIEW, reason=SkipReason.STUCK_FLOW,
                                detail="Instahyre company-site destination could not be verified")

        apply_btn = await first_visible(
            modal,
            ["div.apply[ng-click='submitChoice(opp, true)'] button:text-is('Apply')", "div.bar-actions button:text-is('Apply')"],
            timeout_ms=4000,
        )

        if not apply_btn:
            # Debug: Log all visible buttons on page
            all_buttons = await self.page.locator("button, a.btn, input[type='button'], input[type='submit']").all()
            button_texts = []
            for btn in all_buttons:
                txt = (await safe_text(btn)).strip()
                if txt:
                    button_texts.append(txt[:30])
            log.info("instahyre.debug_buttons", count=len(all_buttons), buttons=button_texts[:20])

            # Dump page HTML for exact inspection
            try:
                dump_path = self.artifacts.dir / f"instahyre-dump-{job.job_id}.html"
                content = await self.page.content()
                dump_path.write_text(content, encoding="utf-8")
                log.info("instahyre.html_dump_saved", path=str(dump_path))
            except Exception as exc:
                log.debug("instahyre.html_dump_failed", error=str(exc))

            if await first_visible(card, _APPLIED_CARD_MARKERS):
                _record_timing()
                return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)
            _record_timing()
            return ApplyOutcome(ApplicationStatus.FAILED, detail="Apply button not found in modal")



        if not self.policy.may_mutate:
            await self._ensure_modal_closed()
            return ApplyOutcome(ApplicationStatus.SKIPPED, reason=SkipReason.DRY_RUN,
                                detail="Instahyre native Apply verified; no submission attempted")
        self.require_mutation("application.final_submit")

        # Ensure the modal action bar is scrolled into view.
        bar = modal.locator("div.bar-actions, div.row.bar-actions").first
        if await bar.count() > 0:
            try:
                await bar.scroll_into_view_if_needed()
                await human_pause(800, 1500)
            except Exception:
                pass

        try:
            await apply_btn.scroll_into_view_if_needed()
            await apply_btn.click(timeout=4000)
        except Exception as exc:
            log.warning("instahyre.apply.click_uncertain", job_id=job.job_id, error=str(exc)[:120])

        await self._ensure_modal_closed()
        await self._dismiss_modals()
        try:
            confirmed = await self._verify_target_applied(job)
        except Exception as exc:
            log.warning("instahyre.apply.verification_failed", job_id=job.job_id, error=str(exc)[:160])
            confirmed = False
        _record_timing()
        if confirmed:
            return ApplyOutcome(
                ApplicationStatus.APPLIED,
                confirmation_type="dom_marker",
                confirmation_evidence=f"Instahyre exact card shows Applied: {job.company} - {job.title}",
            )
        shot = await self.artifacts.capture_failure(self.page, "instahyre-submit-unconfirmed", profile_name, job.job_id)
        return ApplyOutcome(
            ApplicationStatus.FAILED,
            reason=SkipReason.SUBMISSION_UNCONFIRMED,
            detail="Submission clicked but no Instahyre success confirmation was observed",
            screenshot_path=shot,
        )
