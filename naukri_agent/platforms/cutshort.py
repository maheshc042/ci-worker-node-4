"""
Cutshort Platform Implementation (Direct-Action & Inbox Message Handler).
"""
from __future__ import annotations

import asyncio
import re
from time import monotonic
from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.async_api import Locator, Page

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    first_visible,
    human_pause,
    human_type,
    safe_text,
    scroll_page,
)
from ..config import AgentConfig, JobProfile, NaukriAccount
from ..core.answers import AnswerEngine
from ..core.models import (
    ApplicationStatus,
    ApplyOutcome,
    FilterDecision,
    Job,
    ScreeningQuestion,
    SkipReason,
)
from ..core.run_policy import RunPolicy
from ..logging_setup import get_logger
from ..naukri.parser import extract_jd_experience
from .base import BaseJobPlatform

log = get_logger(__name__)

CUTSHORT_AI_SKILLS: list[str | tuple[str, str]] = [
    "Python",
    "Generative AI",
    "Agentic AI",
    "Large Language Models (LLM)",
    ("RAG", "Retrieval Augmented Generation (RAG)"),
    ("Artificial Intelligence", "Artificial Intelligence (AI)"),
    "MLOps",
    ("tuning", "Large Language Models (LLM) tuning"),
    ("Bedrock", "AWS Bedrock"),
    ("FastAPI", "FastAPI"),
    "LangGraph",
    ("Prompt", "Prompt engineering"),
    ("Node", "NodeJS (Node.js)"),
    "TypeScript",
    "Javascript",
]

CUTSHORT_FULLSTACK_SKILLS: list[str | tuple[str, str]] = [
    ("React", "React.js"),
    ("Next", "NextJs (Next.js)"),
    "Javascript",
    ("Node", "NodeJS (Node.js)"),
    "TypeScript",
    "Python",
    "FastAPI",
]

# Unified automatically merges AI and Full Stack with zero duplication
CUTSHORT_UNIFIED_SKILLS: list[str | tuple[str, str]] = list(
    dict.fromkeys(CUTSHORT_AI_SKILLS + CUTSHORT_FULLSTACK_SKILLS)
)

async def _safe_click(loc) -> bool:
    """Click-or-JS-click without raising: questionnaire controls are best
    effort; callers decide what a missed click means."""
    try:
        await loc.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        await loc.click(force=True, timeout=1000)
        return True
    except Exception:
        pass
    try:
        await loc.evaluate("el => el.click()")
        return True
    except Exception as exc:
        log.debug("cutshort.click_failed", error=str(exc)[:80])
        return False


def _primary_topic(q_label: str, keywords: list[str], window: int = 120) -> bool:
    """True when a money/identity keyword leads the label (pure).

    Labels lead with their topic; container dumps bury it. A compensation
    keyword at position 400 of a 600-char dump is attribution noise, not a
    pay question (run 539: CTC boilerplate pasted into a demo-testing box).
    """
    head = (q_label or "")[:window].lower()
    return any(k in head for k in keywords)


# Run 642: notice-period LWD rendered as input[type=date] or a plain text
# input with a dd-mm-yyyy placeholder. The generic "notice" branch would
# otherwise fill it with "0 days" and hard-block the form.
_PLACEHOLDER_DATE_RE = re.compile(r"\b(?:dd|mm)\s*[-/.]\s*(?:dd|mm)\s*[-/.]\s*yy(?:yy)?", re.IGNORECASE)
_DATE_VALUE_RE = re.compile(r"^\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2,4})\s*$")
# Run 644 fail-closed gate: the canonical notice-period probes below are
# applied only to labels that themselves talk about notice/LWD/relieving —
# never stuffed into unrelated date inputs (e.g. "Preferred conference city?").
_NOTICE_DATE_HINT_RE = re.compile(r"notice|relievi?ng|last working|lwd", re.I)

# Employment status rendered outside fieldset (run 642). Fallback marker
# matcher fires ONLY when config could not resolve the exact option set;
# it asserts the candidate's real post-LWD status and must never absorb
# unrelated buckets (career break / fresher / student are named negatives).
_UNEMPLOYED_MARKERS = (
    "unemploy", "not employ", "not employed", "currently not working",
    "not working", "out of work", "without job", "no job",
)
_EMPLOYMENT_NEGATIVES = ("career break", "fresher", "student", "studying", "notice")


def _is_date_input_hint(inp_type: str, placeholder: str) -> bool:
    """True when an input must be treated as a date (pure).

    Native `type=date` inputs and text inputs whose placeholder spells a
    d-m-y format both qualify; ordinary placeholders ("About you...") do not.
    """
    if (inp_type or "").strip().lower() == "date":
        return True
    ph = (placeholder or "").strip()
    return bool(ph and _PLACEHOLDER_DATE_RE.search(ph))


def _format_date_for_input(raw: str, *, iso: bool, placeholder: str) -> str | None:
    """Shape a config date ("30/04/2026") into what the input accepts (pure).

    Native Playwright `fill()` on input[type=date] requires ISO; text inputs
    follow the placeholder's own order and separator. Returns None when raw
    is not a parseable d-m-y value — callers fail closed.
    """
    m = _DATE_VALUE_RE.match((raw or "").strip())
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        parsed = date(y, mo, d)
    except ValueError:
        return None
    if iso:
        return parsed.isoformat()
    ph = (placeholder or "").lower()
    mm_first = "mm" in ph and "dd" in ph and ph.index("mm") < ph.index("dd")
    sep = next((c for c in ("-", "/", ".") if c in ph), "-")
    if mm_first:
        return "{}{}{}{}{}".format(f"{mo:02d}", sep, f"{d:02d}", sep, f"{y:04d}")
    return "{}{}{}{}{}".format(f"{d:02d}", sep, f"{mo:02d}", sep, f"{y:04d}")


def _employment_status_pick(opt_texts: list[str]) -> str | None:
    """Fallback marker matcher for employment-status options (pure).

    Only for labels the config resolver could not match exactly. Picks the
    un-employed bucket ("Unemployed", "Not employed", "Not currently
    working"), skipping career-break / fresher / student buckets. Returns
    the matched original option text, else None.
    """
    for text in opt_texts or []:
        t = (text or "").strip().lower()
        if not t or any(neg in t for neg in _EMPLOYMENT_NEGATIVES):
            continue
        if any(marker in t for marker in _UNEMPLOYED_MARKERS):
            return (text or "").strip()
    return None


def _track_resume_for(job: "Job", extra_text: str = "") -> tuple[Path | None, str]:
    """Resolve the profile resume matching this job's engineering track.

    Track mapping is config-driven (config.yaml per-profile resume_file);
    no path is ever hardcoded here. Returns (absolute Path | None, track).
    """
    from ..config import PROJECT_ROOT
    try:
        cfg = AgentConfig.load()
        text_for_resume = f"{job.title} {extra_text} {job.description}".lower()
        is_ai_job = any(
            k in text_for_resume
            for k in ["genai", "machine learning", "chatbot", "fastapi"]
        )
        is_ai_job |= bool(re.search(r"\b(?:ai|ml|llms?|gpt|rag|agents?|nlp)\b", text_for_resume))
        is_ai_job |= bool(re.search(r"\bpython\b|\b(?:fastapi|django|flask)\b", job.title, re.I))
        profiles = [
            p for p in (getattr(cfg, "profiles", []) or [])
            if p.enabled and getattr(p, "resume_file", None)
        ]

        def _track_resume(want_ai: bool) -> str:
            for p in profiles:
                is_ai = any(k in (p.name or "").lower() for k in ["ai", "python", "ml", "llm"])
                if is_ai == want_ai:
                    return p.resume_file
            return profiles[0].resume_file if profiles else ""

        if is_ai_job:
            rel = _track_resume(True)
            return ((PROJECT_ROOT / rel) if rel else None, "AI / Python Engineer")
        rel = _track_resume(False)
        return ((PROJECT_ROOT / rel) if rel else None, "Full Stack Engineer")
    except Exception as exc:
        log.debug("cutshort.apply.dynamic_resume_failed", error=str(exc)[:80])
        return None, ""


def _resume_label_pattern(path: Path) -> re.Pattern[str]:
    stem = path.stem.rstrip("_")
    # The rendered filename can omit the local trailing underscore or use
    # it as the separator before Cutshort's upload suffix (4-12 chars).
    suffix = r"(?:_{1,2}[A-Za-z0-9-]{4,12}|_)?" if stem != path.stem else r"(?:_[A-Za-z0-9-]{4,12})?"
    return re.compile(rf"^{re.escape(stem)}{suffix}(?:\s*\(\d+\))?(?:[.]pdf)?$", re.IGNORECASE)


class CutshortChatbot:
    """Handles recruiter screening questions and quick-reply options in Cutshort messages."""

    def __init__(
        self,
        page: Page | Locator,
        answers: AnswerEngine,
        max_questions: int = 10,
        resume_path: Path | None = None,
    ):
        self.page = page
        self.answers = answers
        self.max_questions = max_questions
        # Optional absolute resume path pre-selected for this job's track by
        # the caller (run 642: form-stall was a file input the chatbot never
        # touched). Inbox threads pass None — file inputs there queue for
        # review (fail closed), since a thread context identifies no job.
        self.resume_path = resume_path
        # Questions this run could not answer (populated during run(),
        # drained by the caller into the review queue). Bounded: same
        # question re-queues only bump times_seen via upsert.
        self.unanswered: list[dict] = []

    def _queue_unanswered(self, text: str, kind: str, options: list) -> None:
        """Record a distinct unanswerable question for human review."""
        text = (text or "").strip()[:500]
        if not text:
            return
        if any(u.get("text") == text for u in self.unanswered):
            return
        self.unanswered.append({"text": text, "kind": kind, "options": list(options or [])})

    def _match_chip(self, chip_text: str, resolved_val: str) -> bool:
        """Match the resolved option or an explicit boolean answer."""
        chip = " ".join(chip_text.lower().split())
        answer = " ".join(resolved_val.lower().split())
        if not chip or not answer:
            return False
        if chip == answer:
            return True
        if answer in {"yes", "true", "no", "false"}:
            return AnswerEngine._polarity(chip) is (answer in {"yes", "true"})
        return False

    _JS_CHIP_FINGERPRINT = (
        "el => {"
        " var scope = el.closest('fieldset') || el.closest(\"div[role='radiogroup']\") || el.parentElement;"
        " var marks = scope ? scope.querySelectorAll(\"[aria-checked='true'], input:checked\").length : -1;"
        " if (el.matches(\"[aria-checked='true'], input:checked\")"
        " || el.querySelector(\"input:checked, [class*='sc-dde0ab83-2'] > [class*='sc-dde0ab83-3']\"))"
        " return 'selected:' + el.outerHTML;"
        " return el.outerHTML + '@' + marks;"
        "}"
    )

    async def _chip_fingerprint(self, opt) -> str:
        """DOM fingerprint of a chip plus its selection scope.

        OuterHTML plus the count of checked elements inside the closest
        fieldset/radiogroup scope. run-652 (Avive Voila questionnaire):
        options are plain div[tabindex='0'] chips (no role=radio, no
        aria-checked), and the pointer click never flipped the React state
        — the logs claimed a selection while the submitted form carried
        none. Comparing before/after this fingerprint is the only reliable
        way to know a click registered.
        """
        try:
            return str(await opt.evaluate(self._JS_CHIP_FINGERPRINT)).strip()
        except Exception:
            return ""

    async def _click_option_verified(self, opt) -> bool:
        """Click a chip and return True only when the DOM flipped.

        Escalating strategies (pointer click → synthetic mouse-event
        dispatch → keyboard focus + Space/Enter) with a fingerprint check
        after each. Returns False when no strategy changed the DOM, so the
        caller can fail closed instead of counting a phantom answer.
        """
        before = await self._chip_fingerprint(opt)
        if before.startswith("selected:"):
            return True
        try:
            await opt.scroll_into_view_if_needed()
        except Exception:
            pass

        # Pointer click.
        try:
            await opt.click(force=True, timeout=1000)
        except Exception as exc:
            log.debug("cutshort.chatbot.chip_click_failed", strategy="pointer", error=str(exc)[:80])
            try:
                await opt.evaluate("el => el.click()")
            except Exception:
                pass
        await human_pause(150, 350)
        after = await self._chip_fingerprint(opt)
        if after and after != before:
            return True

        # Synthetic mouse-event dispatch (bypasses Playwright actionability
        # but still goes through React's document-level listeners when they
        # are attached).
        for strategy in ("js", "keyboard"):
            try:
                if strategy == "js":
                    await opt.evaluate(
                        "el => { ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach("
                        "function(t) { el.dispatchEvent(new MouseEvent(t, "
                        "{ bubbles: true, cancelable: true, view: window })); }); }"
                    )
                else:
                    # Keyboard can double-toggle on Enter after Space, so
                    # re-verify after each key and stop at the first hit.
                    await opt.evaluate("el => { el.focus && el.focus(); }")
                    for key in ("Space", "Enter"):
                        try:
                            await opt.press(key)
                        except Exception as exc:
                            log.debug(
                                "cutshort.chatbot.chip_click_failed",
                                strategy="keyboard",
                                key=key,
                                error=str(exc)[:80],
                            )
                        await human_pause(150, 350)
                        after = await self._chip_fingerprint(opt)
                        if after and after != before:
                            return True
                    continue
                await human_pause(150, 350)
                after = await self._chip_fingerprint(opt)
                if after and after != before:
                    return True
            except Exception as exc:
                log.debug("cutshort.chatbot.chip_click_failed", strategy=strategy, error=str(exc)[:80])
        return False

    def _resolve_date_answer(self, q_label: str) -> str:
        """Config-sourced raw date for a date input (never fabricated).

        Resolution order: the question's own label through the AnswerEngine
        (its Stage-0 LWD intent catches last-working-day / relieving-date /
        notice-end wordings — widened in run 644 for the conditional
        "When will your notice period end?" reveal), then — only when the
        label itself hints at notice/LWD (or is not a clean string; those
        kept the legacy blanket behaviour) — the neutral canonical readings.
        Returns "" when config holds no date.
        """
        try:
            hinted = isinstance(q_label, str) and bool(_NOTICE_DATE_HINT_RE.search(q_label))
        except Exception:
            hinted = False
        probes = [q_label]
        if hinted or not isinstance(q_label, str):
            probes.extend(
                [
                    "when will your notice period end",
                    "end date of notice",
                    "last working day",
                ]
            )
        for q_text in probes:
            if not q_text:
                continue
            res = self.answers.resolve(ScreeningQuestion(text=q_text[:200], kind="text"))
            if res and res.value and _DATE_VALUE_RE.match(str(res.value)):
                return str(res.value)
        return ""

    async def _count_empty_required(self) -> int:
        """Number of visible required text boxes still empty after a submit.

        Post-submit verification (run 644): a rejected submission keeps the
        form on screen with empty required controls, while a real success
        navigates away (count 0). Checkboxes and radios are excluded — a
        native radio always reports a non-empty input_value() even when
        unchecked, so it can never express "empty" this way.
        """
        count = 0
        try:
            inputs = await self.page.locator(
                "form input[required]:not([type='checkbox']):not([type='radio']), "
                "form textarea[required], form select[required]"
            ).all()
        except Exception:
            return 0
        for el in inputs:
            try:
                if not await el.is_visible():
                    continue
                if not str(await el.input_value()).strip():
                    count += 1
            except Exception:
                continue
        return count

    async def run(self) -> tuple[bool, int, str]:
        """Returns (success, questions_answered, error_message)."""
        answered = 0
        self.unanswered = []
        self.submitted = False
        self.already_submitted = False
        receipt_selectors = [
            ":text-is('Questionnaire submitted!')",
            ":text-is('Your response has been recorded')",
        ]

        for attempt in range(self.max_questions):
            await human_pause(1200, 2000)

            # The observed receipt replaces the questions inside this form.
            form_loc = self.page.locator(
                "form:has(fieldset), form:has(textarea), form:has(button:text-is('Submit')), "
                "form:has(button:text-is('Send response')), form:has(:text-is('Questionnaire submitted!')), "
                "form:has(:text-is('Your response has been recorded'))"
            ).first
            form_present = await form_loc.count() > 0 and await form_loc.is_visible()
            if form_present and await first_visible(form_loc, receipt_selectors, timeout_ms=600) is not None:
                self.already_submitted = True
                log.info("cutshort.chatbot.already_submitted")
                return True, answered, ""

            # 1A0. DATE inputs (run 642: notice-period end date rendered as
            # input[type=date] or a dd-mm-yyyy text input). Must run before
            # phase 1A: the generic "notice" branch fills these with
            # "0 days", which a date input rejects and the form never
            # submits → stall. Answer source is config only (answer engine
            # LWD intent), formatted per input (ISO for native fill()).
            if form_present:
                try:
                    date_inputs = await form_loc.locator("input[type='date'], input[placeholder]").all()
                except Exception:
                    date_inputs = []
                for dinp in date_inputs:
                    try:
                        if not await dinp.is_visible():
                            continue
                        # Skip anything already carrying a value: the date
                        # phase fills each input at most once per run.
                        if (await dinp.input_value()).strip():
                            continue
                        d_type = (await dinp.get_attribute("type")) or ""
                        d_ph = (await dinp.get_attribute("placeholder")) or ""
                        if not _is_date_input_hint(d_type, d_ph):
                            continue  # text input the 1A branch owns
                        dq_label = ""
                        try:
                            _lab = dinp.locator("xpath=preceding::label[1], xpath=ancestor::li[1]").first
                            if await _lab.count() > 0:
                                dq_label = (await safe_text(_lab)).strip()[:200]
                        except Exception:
                            pass
                        raw = self._resolve_date_answer(dq_label)
                        if not raw:
                            log.debug("cutshort.chatbot.no_date_config", question=(dq_label or "date")[:60])
                            self._queue_unanswered(dq_label or "Date input", "date", [])
                            continue
                        formatted = _format_date_for_input(
                            raw, iso=(d_type.strip().lower() == "date"), placeholder=d_ph,
                        )
                        if not formatted:
                            log.debug("cutshort.chatbot.date_unparseable", raw=raw[:20])
                            self._queue_unanswered(dq_label or "Date input", "date", [])
                            continue
                        await dinp.scroll_into_view_if_needed()
                        await human_pause(200, 400)
                        await dinp.fill(formatted)
                        await human_pause(200, 400)
                        await dinp.dispatch_event("input")
                        await dinp.dispatch_event("change")
                        log.info("cutshort.chatbot.answered_date", question=(dq_label or "date")[:60], value=formatted)
                        answered += 1
                    except Exception as exc:
                        log.debug("cutshort.chatbot.date_fill_failed", error=str(exc)[:80])

            # 1A. Textareas & Text Inputs inside form
            if form_present:
                textareas = await form_loc.locator("textarea:not([name='message']), input[type='text']:not([name='message'])").all()
                for ta in textareas:
                    if not await ta.is_visible():
                        continue
                    current_val = (await ta.input_value()).strip()
                    if current_val:
                        continue

                    # Date-format inputs belong to phase 1A0 above (run 642:
                    # filling a dd-mm-yyyy box with "0 days" blocks submit).
                    try:
                        if _is_date_input_hint(
                            (await ta.get_attribute("type")) or "",
                            (await ta.get_attribute("placeholder")) or "",
                        ):
                            continue
                    except Exception:
                        pass

                    # Extract question label
                    q_label = ""
                    try:
                        label_loc = ta.locator("xpath=ancestor::li[1]//label, xpath=preceding::label[1]").first
                        if await label_loc.count() > 0:
                            q_label = (await safe_text(label_loc)).strip()
                    except Exception:
                        pass
                    if not q_label:
                        try:
                            parent_li = ta.locator("xpath=ancestor::li[1]").first
                            if await parent_li.count() > 0:
                                q_label = (await safe_text(parent_li)).strip()
                        except Exception:
                            pass

                    # Label-attribution guard (run 539: a compensation label
                    # from a NEIGHBORING question leaked into the demo-testing
                    # box, pasting CTC boilerplate as its answer). A label
                    # taken from outside the textarea's own list item is
                    # discarded; the container text becomes the question so the
                    # item still trains via review instead of vanishing. Money
                    # branches stay off for unattributed text (a container dump
                    # may name other questions' pay bands).
                    _label_trusted = True
                    if q_label:
                        try:
                            _li = ta.locator("xpath=ancestor::li[1]").first
                            if await _li.count() > 0:
                                _li_text = (await safe_text(_li)).strip()
                                if q_label[:40] not in _li_text:
                                    log.info("cutshort.chatbot.label_misattributed",
                                             question=q_label[:80])
                                    q_label = _li_text[:500]
                                    _label_trusted = False
                        except Exception:
                            pass

                    q_label_low = q_label.lower()

                    if any(k in q_label_low for k in ["hardest", "challenging", "complex problem", "technical problem", "problems you have worked on"]):
                        # Fail closed: answer only from user config, never a
                        # hard-coded achievement narrative.
                        _hard = self.answers.resolve(ScreeningQuestion(text=q_label[:200], kind="text"))
                        ta_ans = str(_hard.value) if _hard and _hard.value else ""
                    elif any(k in q_label_low for k in ["technical skillset", "strong in", "strength", "skillsets do you consider"]):
                        # Fail closed: answer only from user config, never a
                        # hard-coded skill/tenure claim.
                        _stren = self.answers.resolve(ScreeningQuestion(text=q_label[:200], kind="text"))
                        ta_ans = str(_stren.value) if _stren and _stren.value else ""
                    elif _primary_topic(q_label, ["phone", "mobile", "contact number", "contact no", "whatsapp", "call you"]) and _label_trusted:
                        ta_ans = (AgentConfig.load().applicant_phone or "").strip()
                    elif _primary_topic(q_label, ["email", "mail id", "email address", "mail address"]) and _label_trusted:
                        ta_ans = (AgentConfig.load().applicant_email or "").strip()
                    elif _primary_topic(q_label, ["ctc", "salary", "fixed", "variable", "in hand", "in-hand", "annual ctc", "compensation"]) and _label_trusted:
                        ctc_parts = []
                        for q_text, label in (("current ctc", "Current CTC"), ("expected ctc", "Expected CTC"), ("notice period", "Notice Period")):
                            part = self.answers.resolve(ScreeningQuestion(text=q_text, kind="text"))
                            if part and part.value:
                                ctc_parts.append(f"{label}: {part.value}")
                        ta_ans = ". ".join(ctc_parts)
                    elif any(k in q_label_low for k in ["docker", "kubernetes", "container"]):
                        # Fail closed: answer only from user config, never a
                        # hard-coded tenure claim.
                        _dock = self.answers.resolve(ScreeningQuestion(text=q_label[:200], kind="text"))
                        ta_ans = str(_dock.value) if _dock and _dock.value else ""
                    elif _primary_topic(q_label, ["notice", "how soon", "when can you join", "joining date", "availability"]) and _label_trusted:
                        notice_res = self.answers.resolve(ScreeningQuestion(text="notice period", kind="text"))
                        ta_ans = notice_res.value if notice_res and notice_res.value else ""
                    elif _primary_topic(q_label, ["location", "relocate", "relocation", "bangalore", "bengaluru", "mumbai", "hyderabad"]) and _label_trusted:
                        loc_res = self.answers.resolve(ScreeningQuestion(text=q_label[:200], kind="text"))
                        ta_ans = loc_res.value if loc_res and loc_res.value else ""
                    else:
                        resolved = self.answers.resolve(ScreeningQuestion(text=q_label[:200], kind="text"))
                        if resolved and resolved.value:
                            ta_ans = str(resolved.value)
                        else:
                            ta_ans = ""

                    if not (ta_ans or "").strip():
                        # Config holds no answer for this question: fail closed
                        # instead of submitting a guessed or literal response,
                        # and record it for review so it trains once.
                        log.debug("cutshort.chatbot.no_config_answer", question=q_label[:60])
                        self._queue_unanswered(q_label, "text", [])
                        continue
                    try:
                        await ta.scroll_into_view_if_needed()
                        await human_pause(200, 400)
                        await ta.fill(ta_ans)
                        await human_pause(200, 400)
                        # Crucial: dispatch input & change so React synthetic state updates
                        await ta.dispatch_event("input")
                        await ta.dispatch_event("change")
                        log.info("cutshort.chatbot.answered_textarea", question=q_label[:60], chars=len(ta_ans))
                        answered += 1
                    except Exception as exc:
                        log.debug("cutshort.chatbot.textarea_fill_failed", error=str(exc))

            # 1B. Fieldset Radios inside form or page
            fieldsets = await self.page.locator("fieldset").all()
            if fieldsets:
                for fs in fieldsets:
                    if not await fs.is_visible():
                        continue
                    legend_el = fs.locator("legend").first
                    q_text = (await safe_text(legend_el)).strip() if await legend_el.count() > 0 else (await safe_text(fs)).strip()
                    if not q_text:
                        continue

                    options = await fs.locator("div[tabindex='0']").all()
                    if not options:
                        options = await fs.locator("div[role='radio'], label, button").all()
                    opt_texts = [(await safe_text(opt)).strip() for opt in options if (await safe_text(opt)).strip()]

                    # Display rows masquerading as questions (run 375: "Resume /
                    # Work experience / Current company / Current location" with
                    # a single echo option). Not choices — skip without failing,
                    # UNLESS an unchecked declaration checkbox needs ticking.
                    distinct_opts = {o.lower() for o in opt_texts}
                    if len(distinct_opts) <= 1:
                        boxes = await fs.locator("input[type='checkbox']").all()
                        ticked = False
                        for cb in boxes:
                            try:
                                if not await cb.is_visible() or await cb.is_checked():
                                    continue
                                cb_low = q_text.lower()
                                if any(k in cb_low for k in ("declar", "agree", "accept", "confirm", "certify", "consent", "acknowledge", "terms", "policy", "true and correct")):
                                    try:
                                        await cb.check(force=True)
                                    except Exception:
                                        await _safe_click(cb)
                                    await human_pause(200, 400)
                                    log.info("cutshort.chatbot.checked_declaration", question=q_text[:70])
                                    answered += 1
                                    ticked = True
                            except Exception:
                                continue
                        if not ticked:
                            log.debug("cutshort.chatbot.display_row_skipped", question=q_text[:70])
                        continue

                    # Specialization choice (run 375: Software development vs
                    # fullstack). Candidate is full-stack first: prefer the
                    # fullstack option explicitly instead of leaving it to the
                    # generic resolver, which has no specialization mapping.
                    if "specializ" in q_text.lower():
                        _pref = ["fullstack", "full stack", "software development", "backend", "frontend", "ai", "python"]
                        picked_spec = None
                        for want in _pref:
                            for opt in options:
                                if want in (await safe_text(opt)).strip().lower():
                                    picked_spec = opt
                                    break
                            if picked_spec is not None:
                                break
                        if picked_spec is not None:
                            if await self._click_option_verified(picked_spec):
                                log.info(
                                    "cutshort.chatbot.answered_specialization",
                                    selected=(await safe_text(picked_spec)).strip()[:50],
                                )
                                await human_pause(200, 400)
                                answered += 1
                            else:
                                # run-652: unverifiable pick — fail closed and
                                # queue for review instead of counting it.
                                log.warning(
                                    "cutshort.chatbot.chip_click_unverified",
                                    question=q_text[:70],
                                    selected=(await safe_text(picked_spec)).strip()[:50],
                                )
                                self._queue_unanswered(q_text, "radio", opt_texts)
                            continue

                    # Salary inputs disguised as degenerate radios (run 375:
                    # options ['INR','INR'] with current/expected text boxes).
                    # Fill from configured CTC answers — never fabricated.
                    if any(k in q_text.lower() for k in ["salary", "ctc", "lacs", "compensation", "drawn salary", "expected salary"]) and (
                        len(distinct_opts) <= 1 or all(o.lower() in ("inr", "rs", "₹", "lacs", "lpa") for o in opt_texts)
                    ):
                        sal_inputs = await fs.locator("input[type='text'], input[type='number'], input:not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='file']):not([type='submit'])").all()
                        sal_inputs = [i for i in sal_inputs if await i.is_visible()]
                        cur_res = self.answers.resolve(ScreeningQuestion(text="current ctc", kind="text", options=[]))
                        exp_res = self.answers.resolve(ScreeningQuestion(text="expected ctc", kind="text", options=[]))
                        cur_val = (cur_res.value if cur_res and cur_res.value else "").strip()
                        exp_val = (exp_res.value if exp_res and exp_res.value else "").strip()
                        filled = 0
                        for idx, inp in enumerate(sal_inputs[:2]):
                            want_val = cur_val if idx == 0 else exp_val
                            if not want_val:
                                continue
                            try:
                                cur = (await inp.input_value()).strip()
                                if cur:
                                    filled += 1
                                    continue
                                await inp.scroll_into_view_if_needed()
                                await inp.fill(want_val)
                                await human_pause(200, 400)
                                try:
                                    await inp.dispatch_event("input")
                                    await inp.dispatch_event("change")
                                except Exception:
                                    pass
                                log.info("cutshort.chatbot.answered_salary", question=q_text[:60], value=want_val)
                                filled += 1
                            except Exception as exc:
                                log.debug("cutshort.chatbot.salary_fill_failed", error=str(exc))
                        if filled:
                            answered += filled
                            continue

                    resolved = self.answers.resolve(ScreeningQuestion(text=q_text, kind="radio", options=opt_texts))
                    target_val = resolved.value if resolved else ""

                    matched_opt = False
                    for opt in options:
                        opt_text = (await safe_text(opt)).strip()
                        if target_val and (self._match_chip(opt_text, target_val) or opt_text.lower() == target_val.lower()):
                            matched_opt = True
                            if await self._click_option_verified(opt):
                                log.info(
                                    "cutshort.chatbot.answered_field",
                                    question=q_text[:70],
                                    selected=opt_text[:50],
                                )
                                await human_pause(200, 400)
                                answered += 1
                            else:
                                # run-652: the click never registered the
                                # React selected state. Fail closed — do not
                                # count it as answered; queue for review
                                # instead of submitting an empty form.
                                log.warning(
                                    "cutshort.chatbot.chip_click_unverified",
                                    question=q_text[:70],
                                    selected=opt_text[:50],
                                )
                                matched_opt = False
                            break
                    if not matched_opt:
                        # No option matched (or nothing resolved): record for
                        # review instead of silently skipping every run.
                        self._queue_unanswered(q_text, "radio", opt_texts)

            # 1B2. Radiogroups OUTSIDE fieldsets (run 642: "Employment status"
            # renders as div[role='radiogroup'] with no <legend>, which the
            # fieldset scanner above can never see).
            try:
                rgroups = await self.page.locator("div[role='radiogroup']").all()
            except Exception:
                rgroups = []
            for rg in rgroups:
                try:
                    if not await rg.is_visible():
                        continue
                    # Already answered: an aria-checked option or a checked
                    # native radio inside means this group is done.
                    already = False
                    try:
                        already = (await rg.locator("[aria-checked='true']").count()) > 0
                    except Exception:
                        pass
                    try:
                        already = already or (await rg.locator("input:checked").count()) > 0
                    except Exception:
                        pass
                    if already:
                        continue

                    opts = await rg.locator("div[role='radio']").all()
                    if not opts:
                        opts = await rg.locator("button, label").all()
                    r_opt_texts = [(await safe_text(o)).strip() for o in opts if (await safe_text(o)).strip()]
                    # Row-guard: a display row echoes itself as a single
                    # chunk of text — not choices, skip without queueing.
                    if len(r_opt_texts) <= 1:
                        continue

                    r_label = ""
                    try:
                        r_label = (await rg.get_attribute("aria-label")) or ""
                    except Exception:
                        pass
                    if not r_label:
                        try:
                            _anc = rg.locator("xpath=ancestor::li[1]").first
                            if await _anc.count() > 0:
                                r_label = (await safe_text(_anc)).strip()[:200]
                        except Exception:
                            pass

                    r_res = self.answers.resolve(ScreeningQuestion(text=r_label, kind="radio", options=r_opt_texts))
                    target_val = r_res.value if r_res and r_res.value else ""
                    # Employment fallback fires ONLY for status labels config
                    # could not resolve (run 642: "Employment status" options
                    # are not in config) — never for any other group.
                    if not target_val and any(
                        k in r_label.lower()
                        for k in ("employment", "currently working", "work status", "career status")
                    ):
                        target_val = _employment_status_pick(r_opt_texts) or ""

                    r_matched = False
                    # Two-pass selection: exact equality first. Substring
                    # fuzzy matching alone would let the "Employed" chip
                    # match a resolved "Not employed" (the "employed"
                    # substring) — silently claiming the wrong status.
                    # Only the FIRST hit option is clicked: never escalate
                    # to a fuzzy-different chip after an unverified exact
                    # hit.
                    hit_opt = None
                    hit_text = ""
                    for pass_exact in (True, False):
                        for o in opts:
                            o_text = (await safe_text(o)).strip()
                            if not target_val:
                                hit_opt = None
                                break
                            low = o_text.lower()
                            if pass_exact:
                                hit = low == target_val.lower()
                            else:
                                hit = self._match_chip(o_text, target_val)
                            if hit:
                                hit_opt = o
                                hit_text = o_text
                                break
                        if hit_opt is not None:
                            break

                    if hit_opt is not None:
                        if await self._click_option_verified(hit_opt):
                            r_matched = True
                            log.info(
                                "cutshort.chatbot.answered_field",
                                question=(r_label or "radiogroup")[:70],
                                selected=hit_text[:50],
                            )
                            await human_pause(200, 400)
                            answered += 1
                        else:
                            # run-652: click never registered the selected
                            # state — fail closed and queue for review.
                            log.warning(
                                "cutshort.chatbot.chip_click_unverified",
                                question=(r_label or "radiogroup")[:70],
                                selected=hit_text[:50],
                            )
                    if not r_matched:
                        self._queue_unanswered(r_label or "Employment status", "radio", r_opt_texts)
                except Exception as exc:
                    log.debug("cutshort.chatbot.radiogroup_failed", error=str(exc)[:80])

            # 1B3. Native selects inside the form.
            if form_present:
                try:
                    selects = await form_loc.locator("select").all()
                except Exception:
                    selects = []
                for sel in selects:
                    try:
                        if not await sel.is_visible():
                            continue
                        try:
                            val_now = (await sel.input_value()).strip()
                        except Exception:
                            val_now = ""
                        if val_now:
                            continue  # already set

                        s_label = ""
                        try:
                            s_label = (await sel.get_attribute("aria-label")) or ""
                        except Exception:
                            pass
                        if not s_label:
                            try:
                                sid = await sel.get_attribute("id")
                                if sid:
                                    _f = self.page.locator(f"label[for='{sid}']").first
                                    if await _f.count() > 0:
                                        s_label = (await safe_text(_f)).strip()[:200]
                            except Exception:
                                pass
                        if not s_label:
                            try:
                                _anc = sel.locator("xpath=ancestor::li[1]").first
                                if await _anc.count() > 0:
                                    s_label = (await safe_text(_anc)).strip()[:200]
                            except Exception:
                                pass

                        s_opts = await sel.locator("option").all()
                        s_opt_texts = [(await s_opt.inner_text()).strip() for s_opt in s_opts if (await s_opt.inner_text()).strip()]
                        s_res = self.answers.resolve(ScreeningQuestion(text=s_label, kind="text", options=s_opt_texts))
                        want = s_res.value if s_res and s_res.value else ""
                        if not want and any(k in s_label.lower() for k in ("employment", "currently working", "work status")):
                            want = _employment_status_pick(s_opt_texts) or ""

                        s_matched = False
                        if want:
                            # Exact equality first (run 642): substring fuzzy
                            # would match an "Employed" option against a
                            # resolved "Not employed" through the "employed"
                            # substring and claim the wrong status.
                            for pass_exact in (True, False):
                                for s_opt in s_opts:
                                    s_txt = (await s_opt.inner_text()).strip()
                                    if not s_txt:
                                        continue
                                    hit = (
                                        s_txt.lower() == want.lower()
                                        if pass_exact
                                        else self._match_chip(s_txt, want)
                                    )
                                    if hit:
                                        s_matched = True
                                        await sel.scroll_into_view_if_needed()
                                        await sel.select_option(label=s_txt)
                                        log.info(
                                            "cutshort.chatbot.answered_select",
                                            question=(s_label or "select")[:70],
                                            selected=s_txt[:50],
                                        )
                                        await human_pause(200, 400)
                                        answered += 1
                                        break
                                if s_matched:
                                    break
                        if not s_matched:
                            self._queue_unanswered(s_label or "Select", "select", s_opt_texts)
                    except Exception as exc:
                        log.debug("cutshort.chatbot.select_failed", error=str(exc)[:80])

            # 1B4. File uploads inside the form (run 642: the resume input was
            # never touched, so submit stayed disabled → stall). Fail closed:
            # upload only when a known resume path exists AND the input is
            # unambiguous (label names a resume/CV/upload, or it is the form's
            # only file input).
            if form_present:
                try:
                    file_inputs = await form_loc.locator("input[type='file']").all()
                except Exception:
                    file_inputs = []
                for f_inp in file_inputs:
                    try:
                        if not await f_inp.is_visible():
                            continue
                        try:
                            already_filled = await f_inp.evaluate("el => el.files.length > 0")
                        except Exception:
                            already_filled = False
                        if already_filled:
                            continue

                        f_label = ""
                        try:
                            f_label = (await f_inp.get_attribute("aria-label")) or ""
                        except Exception:
                            pass
                        if not f_label:
                            try:
                                _anc = f_inp.locator("xpath=ancestor::li[1]").first
                                if await _anc.count() > 0:
                                    f_label = (await safe_text(_anc)).strip()[:200]
                            except Exception:
                                pass

                        f_low = f_label.lower()
                        is_resume = any(k in f_low for k in ("resume", "cv", "upload"))
                        is_non_resume = any(k in f_low for k in ("cover", "portfolio"))
                        unambiguous = len(file_inputs) == 1 and not is_non_resume
                        if (
                            self.resume_path
                            and self.resume_path.exists()
                            and (is_resume or unambiguous)
                        ):
                            await f_inp.scroll_into_view_if_needed()
                            await f_inp.set_input_files(str(self.resume_path))
                            log.info("cutshort.chatbot.resume_uploaded", file=self.resume_path.name)
                            await human_pause(800, 1200)
                            answered += 1
                        else:
                            self._queue_unanswered(f_label or "Upload resume (file input)", "file", [])
                    except Exception as exc:
                        log.debug("cutshort.chatbot.file_upload_failed", error=str(exc)[:80])

            # 1C. Audio Questionnaire Challenge (e.g. "Tell us about a difficult project... Pick from 1 saved audios")
            audio_btn = await first_visible(
                self.page,
                [
                    "form [role='button']:has-text('Pick from')",
                    "[role='button']:has-text('Pick from')",
                    "button:has-text('Pick from')",
                    "div:has-text('Pick from'):has-text('saved audio')",
                    "[role='button']:has-text('saved audio')",
                    "button:has-text('saved audio')",
                ],
                timeout_ms=1500,
            )
            if audio_btn:
                try:
                    await audio_btn.scroll_into_view_if_needed()
                    await human_pause(400, 700)
                    try:
                        await audio_btn.click(force=True, timeout=2000)
                    except Exception:
                        await audio_btn.evaluate("el => el.click()")
                    log.info("cutshort.chatbot.clicked_saved_audio_picker")
                    await human_pause(1000, 1500)

                    # Check if an audio selection confirmation dialog/modal or list appears
                    confirm_audio_btn = await first_visible(
                        self.page,
                        [
                            "div[id='modal-root'] button:has-text('Select')",
                            "div[id='modal-root'] button:has-text('Choose')",
                            "div[id='modal-root'] button:has-text('Use')",
                            "div[id='modal-root'] button:has-text('Confirm')",
                            "button:has-text('Select audio')",
                            "button:has-text('Use this audio')",
                            "div[role='dialog'] button:has-text('Confirm')",
                            "div[role='dialog'] button:has-text('Select')",
                        ],
                        timeout_ms=2000,
                    )
                    if confirm_audio_btn:
                        try:
                            await confirm_audio_btn.click(force=True)
                        except Exception:
                            await confirm_audio_btn.evaluate("el => el.click()")
                        await human_pause(500, 1000)
                    answered += 1
                except Exception as exc:
                    log.warning("cutshort.chatbot.audio_picker_failed", error=str(exc))

            if form_present or fieldsets or audio_btn:
                if self.unanswered:
                    return False, answered, "Questionnaire has unresolved answers"
                empty_required = await self._count_empty_required()
                if empty_required:
                    return False, answered, f"{empty_required} required input(s) still empty before submit"
                if not form_present:
                    return False, answered, "Questionnaire form unavailable"
                submit_btn = await first_visible(
                    form_loc, ["button:text-is('Submit')", "button:text-is('Send response')"], timeout_ms=2500,
                )
                if submit_btn is None:
                    return False, answered, "Questionnaire submit control unavailable"
                try:
                    if not await submit_btn.evaluate("el => Boolean(el.form && el.form.checkValidity())"):
                        return False, answered, "Questionnaire has invalid or empty required fields"
                except Exception:
                    return False, answered, "Questionnaire validity could not be verified"
                try:
                    await submit_btn.click(timeout=3000)
                except Exception as exc:
                    log.warning("cutshort.chatbot.submit_click_uncertain", error=str(exc)[:120])

                confirmation = await first_visible(
                    form_loc, [":text-is('Responses cannot be changed after submission. Are you sure?')"],
                    timeout_ms=3000,
                )
                if confirmation is not None:
                    yes = await first_visible(form_loc, ["button:text-is('Yes')"], timeout_ms=1500)
                    if yes is None:
                        return False, answered, "Questionnaire confirmation button unavailable"
                    try:
                        await yes.click(timeout=3000)
                    except Exception as exc:
                        log.warning("cutshort.chatbot.confirm_click_uncertain", error=str(exc)[:120])
                if await first_visible(form_loc, receipt_selectors, timeout_ms=15000) is not None:
                    self.submitted = True
                    log.info("cutshort.chatbot.submitted_form", answered=answered)
                    return True, answered, ""
                log.warning("cutshort.chatbot.submit_unconfirmed")
                return False, answered, "submit unconfirmed: no questionnaire receipt observed"

            # Find incoming recruiter bubbles (excluding candidate's own pitch)
            incoming_bubbles = await self.page.locator(
                "div[class*='incoming'], div[class*='recruiter'], div[class*='bot-msg'], "
                "div[class*='chat-item']:not([class*='sent']):not([class*='outgoing']), "
                "div[class*='message-item']:not([class*='sent']):not([class*='outgoing']), "
                "div.message-content, div[class*='msg-text'], div[class*='message-text'], "
                "div[class*='chat-bubble'], div[class*='msg_body'], p[class*='message'], div[class*='chat_msg']"
            ).all()

            recruiter_text = ""
            for bubble in reversed(incoming_bubbles):
                b_text = (await safe_text(bubble)).strip()
                if not b_text:
                    continue
                # Ignore candidate's own pitch or name signatures
                if b_text.startswith("Hi ") and "I came across the" in b_text:
                    continue
                if (AgentConfig.load().applicant_name or "Applicant") in b_text and len(b_text) > 100:
                    continue
                recruiter_text = b_text
                break

            if not recruiter_text and attempt < min(2, self.max_questions - 1):
                continue
            if "?" in recruiter_text:
                self._queue_unanswered(recruiter_text, "text", [])
                return False, answered, "Recruiter question needs review"
            return True, answered, ""

        return False, answered, "Cutshort chat ended without a completion receipt"

def _parse_cutshort_salary(text: str) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    m_yr = re.search(r"₹?\s*([\d\.]+)\s*[lL]?\s*-\s*₹?\s*([\d\.]+)\s*[lL](?:\s*/\s*yr)?", text, re.IGNORECASE)
    if m_yr:
        try:
            return float(m_yr.group(1)), float(m_yr.group(2))
        except ValueError:
            pass
    m_single = re.search(r"₹?\s*([\d\.]+)\s*[lL](?:\s*/\s*yr)?", text, re.IGNORECASE)
    if m_single:
        try:
            v = float(m_single.group(1))
            return v, v
        except ValueError:
            pass
    m_mo = re.search(r"₹?\s*([\d,]+)\s*[–-]\s*₹?\s*([\d,]+)\s*(?:per month|/month)", text, re.IGNORECASE)
    if m_mo:
        try:
            min_mo = float(m_mo.group(1).replace(",", ""))
            max_mo = float(m_mo.group(2).replace(",", ""))
            return round((min_mo * 12) / 100000.0, 2), round((max_mo * 12) / 100000.0, 2)
        except ValueError:
            pass
    return None, None


def _parse_cutshort_experience(text: str) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*yrs?", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            pass
    m_single = re.search(r"(?:\+?\s*(\d+(?:\.\d+)?)\s*yrs?\+?|(\d+(?:\.\d+)?)\s*\+\s*yrs?)", text, re.IGNORECASE)
    if m_single:
        try:
            val_str = m_single.group(1) or m_single.group(2)
            if val_str:
                return float(val_str), None
        except ValueError:
            pass
    return None, None


_POSTED_AGO_RE = re.compile(
    r"(\d+)\s*(hours?|hrs?|h|days?|d|weeks?|w|months?|mos?|years?|y)\s*ago|\bjust now\b|\btoday\b|\byesterday\b",
    re.IGNORECASE,
)


def _parse_cutshort_posted(text: str) -> int | None:
    """Relative posted age on the card ('3d ago' -> 3). None when unstated.

    Populates `posted_days_ago` so the configured `max_posted_days` gate
    enforces freshness even if the feed's 1-week UI filter silently fails.
    """
    if not text:
        return None
    low = text.lower()
    if any(k in low for k in ("just now", "few minutes", "today", "hour")):
        return 0
    if "yesterday" in low:
        return 1
    m = _POSTED_AGO_RE.search(low)
    if not m or not m.group(1):
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "day").lower()
    if unit.startswith("h"):
        return 0
    if unit.startswith("w"):
        return n * 7
    if unit.startswith("mo"):
        return n * 30
    if unit.startswith("y"):
        return n * 365
    return n


class CutshortPlatform(BaseJobPlatform):
    def __init__(self, page: Page, account: NaukriAccount, artifacts: ArtifactStore, answers: AnswerEngine, policy: RunPolicy, config: AgentConfig | None = None):
        super().__init__(page, account.key, policy)
        self.account = account
        self.artifacts = artifacts
        self.answers = answers
        self.config = config

    @property
    def platform_name(self) -> str:
        return "cutshort"

    async def ensure_logged_in(self) -> bool:
        await self.page.goto("https://cutshort.io/profile/recommended-jobs", wait_until="domcontentloaded")
        await human_pause(1000, 2000)

        login_prompt = await first_visible(
            self.page,
            [
                "text=Candidate login",
                "a[href*='/login']",
                "button:has-text('Sign in with Google')",
                "button:has-text('Login')",
            ],
            timeout_ms=3000,
        )

        if login_prompt:
            log.info(
                "cutshort.auth.google_sso_required",
                msg="Cutshort uses Google SSO. Please log in using 'Sign in with Google' in the opened browser window.",
            )
            print("\n" + "=" * 70)
            print("🔑 CUTSHORT GOOGLE LOGIN REQUIRED:")
            print("Cutshort uses 'Sign in with Google'. Please click 'Sign in with Google' in the opened browser window.")
            print("The agent will wait 90 seconds, save your session, and automatically resume!")
            print("=" * 70 + "\n")

            for _ in range(90):
                await human_pause(1000, 1000)
                if not await first_visible(
                    self.page,
                    ["text=Candidate login", "a[href*='/login']", "button:has-text('Sign in with Google')"],
                    timeout_ms=500,
                ):
                    log.info("cutshort.auth.google_sso_completed")
                    return True

            log.error("cutshort.auth.login_timeout")
            return False

        log.info("cutshort.auth.session_reused")
        return True

    async def _skills_filter_badge(self) -> int:
        """Read the numeric badge inside the skills filter trigger.

        Ground truth (run 641 dashboard dump): the trigger div renders a
        ``<span ...>14</span>`` whose text is the number of registered skill
        chips. Every successful chip selection must increment it; with no
        chips selected the span is absent, so a missing badge reads as 0.
        This is the honest counter the filter block must report — the old
        code logged the *intended* skill count while 4 of 18 chips silently
        failed to register (badge 14 vs logged 18).
        """
        count_loc = self.page.locator(
            "div[data-intercom-target='skills-filter'] span",
            has_text=re.compile(r"\d"),
        )
        try:
            if await count_loc.count() == 0:
                return 0
            digits = (await safe_text(count_loc.first)).strip()
            return int(digits) if digits.isdigit() else 0
        except Exception:
            return 0

    async def _apply_ui_filters(self, profile: JobProfile) -> None:
        """
        Applies verified native UI filters on Cutshort's All Jobs toolbar:
        1. Hiring Activity: In the last 1 week (active/fresh postings)
        2. Role Type: Full time
        3. Minimum Salary: >= 5 LPA (slider set to 500,000 INR)
        4. Experience Range: 0.0 – 3.5 years (slider dual-handles [0.0, 3.5])
        5. Job Category: Expands accordions and selects verified specialisations:
           - Full Stack: Software Development (Frontend, Backend, Full Stack),
                         Testing / QA (Automation Testing, QA),
                         DevOps & IT (DevOps, Site Reliability)
           - AI / Python: Data Science & Analytics (Data Science, ML Engineering, Data Analytics),
                          Software Development (Backend)
        6. Location & Skills: Intentionally untouched so candidate pool is not overly constrained.
        """
        log.info("cutshort.filters.applying_ui", profile=profile.name)

        # 1. Hiring activity on job ("In the last 1 week")
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='hiringActivityOnJob-filter']"], timeout_ms=3000)
            if btn:
                await btn.click(force=True)
                await human_pause(500, 800)
                popover = self.page.locator(".react-tiny-popover-container")

                # Click "In the last"
                radio = popover.locator("text='In the last'").first
                if await radio.is_visible():
                    await radio.click(force=True)
                    await human_pause(200, 400)

                # Set number input to 1 week of active hiring to guarantee top-notch freshness
                num_input = popover.locator("input[type='number']").first
                if await num_input.is_visible():
                    await num_input.fill("1")
                    await human_pause(200, 400)


                # If unit dropdown shows Months or Days, switch to Weeks
                trigger = popover.locator(".component_select_trigger_wrapper").first
                if await trigger.is_visible():
                    cur_unit = (await trigger.inner_text()).strip()
                    if cur_unit != "Weeks":
                        await trigger.click(force=True)
                        await human_pause(300, 500)
                        week_opt = self.page.locator("text='Weeks'").first
                        if await week_opt.is_visible():
                            await week_opt.click(force=True)
                            await human_pause(200, 400)

                await self.page.keyboard.press("Escape")
                await human_pause(400, 600)
                log.info("cutshort.filters.hiring_activity_applied")
        except Exception as exc:
            log.warning("cutshort.filters.activity_error", error=str(exc))

        # 2. Type of role (Full time)
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='roletype-filter']"], timeout_ms=2500)
            if btn:
                await btn.click(force=True)
                await human_pause(500, 800)
                opt = await first_visible(
                    self.page,
                    [
                        "label:has-text('Full time')",
                        "button:has-text('Full time')",
                        "span:has-text('Full time')",
                        "div:has-text('Full time')",
                    ],
                    timeout_ms=1500,
                )
                if opt:
                    await opt.click(force=True)
                    await human_pause(400, 600)
                await self.page.keyboard.press("Escape")
                await human_pause(400, 600)
                log.info("cutshort.filters.role_type_applied")
        except Exception as exc:
            log.warning("cutshort.filters.roletype_error", error=str(exc))

        # 3. Minimum salary (>= 5 LPA)
        min_salary_lpa = profile.filters.min_salary_lpa if (profile.filters and profile.filters.min_salary_lpa) else 5.0
        if min_salary_lpa and min_salary_lpa >= 5.0:
            try:
                btn = await first_visible(self.page, ["div[data-intercom-target='minsal-filter']"], timeout_ms=2500)
                if btn:
                    await btn.click(force=True)
                    await human_pause(500, 800)
                    slider = await first_visible(self.page, ["div[role='slider']"], timeout_ms=1500)
                    if slider:
                        await slider.focus()
                        await self.page.keyboard.press("Home")
                        await human_pause(150, 300)
                        # Step up to desired LPA (each step is 1 LPA / 100,000 INR)
                        steps = int(round(min_salary_lpa))
                        for _ in range(steps):
                            await self.page.keyboard.press("ArrowRight")
                            await human_pause(80, 150)
                        await human_pause(400, 600)
                    await self.page.keyboard.press("Escape")
                    await human_pause(400, 600)
                    log.info("cutshort.filters.minsal_applied", min_salary_lpa=min_salary_lpa)
            except Exception as exc:
                log.warning("cutshort.filters.minsal_error", error=str(exc))

        # 4. Experience range (0.0 – 3.5 years)
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='expRange-filter']"], timeout_ms=2500)
            if btn:
                await btn.click(force=True)
                await human_pause(500, 800)
                sliders = await self.page.locator("div[role='slider']").all()
                if len(sliders) >= 2:
                    min_slider = sliders[0]
                    max_slider = sliders[1]

                    # Ensure min handle is at 0
                    await min_slider.focus()
                    await self.page.keyboard.press("Home")
                    await human_pause(150, 300)

                    # Reset max handle to 0, then step to 3.5 years (7 steps of 0.5y)
                    await max_slider.focus()
                    await self.page.keyboard.press("Home")
                    await human_pause(150, 300)
                    for _ in range(7):
                        await self.page.keyboard.press("ArrowRight")
                        await human_pause(70, 130)
                    await human_pause(400, 600)

                await self.page.keyboard.press("Escape")
                await human_pause(400, 600)
                log.info("cutshort.filters.exp_range_applied", min_exp=0.0, max_exp=3.5)
        except Exception as exc:
            log.warning("cutshort.filters.exp_range_error", error=str(exc))

        # 5. Native Skills Filter (skills-filter) with exact autocomplete options
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='skills-filter']"], timeout_ms=3000)
            if btn:
                await btn.click(force=True)
                await human_pause(600, 1000)
                popover = self.page.locator(".react-tiny-popover-container").first

                # Clear existing skills filter if any active from previous pass
                clear_btn = popover.locator("text='Clear this filter'").first
                if await clear_btn.is_visible():
                    await clear_btn.click(force=True)
                    await human_pause(400, 600)

                prof_low = profile.name.lower()
                is_unified = ("unified" in prof_low) or ("ai" in prof_low and "full stack" in prof_low)
                if is_unified:
                    skills_to_select = CUTSHORT_UNIFIED_SKILLS
                elif "ai" in prof_low or "python" in prof_low:
                    skills_to_select = CUTSHORT_AI_SKILLS
                else:
                    skills_to_select = CUTSHORT_FULLSTACK_SKILLS

                input_el = popover.locator("input[placeholder*='skill' i]").first
                baseline = await self._skills_filter_badge()
                misses: list[str] = []
                for item in skills_to_select:
                    query, exact_name = item if isinstance(item, tuple) else (item, item)
                    registered = False
                    # Two passes: pass 2 types slower and waits longer for the
                    # flyout. Run 641 silently lost 4/18 chips (badge showed
                    # 14 while the code logged 18) — every attempt is now
                    # verified against the filter badge, never assumed.
                    for attempt in range(2):
                        before = await self._skills_filter_badge()
                        try:
                            await input_el.click()
                            await input_el.fill("")
                            if attempt == 0:
                                await human_type(input_el, query)
                                await human_pause(500, 800)
                            else:
                                await human_type(input_el, query, min_delay=60, max_delay=180)
                                await human_pause(900, 1400)
                            flyout = popover.locator(".flyout_el_wrapper").first
                            if await flyout.count() > 0:
                                options = await flyout.locator("div, label, span").all()
                                for opt in options:
                                    if (await safe_text(opt)).strip() == exact_name:
                                        await opt.click(force=True)
                                        await human_pause(400, 700)
                                        break
                        except Exception as skill_err:
                            log.debug("cutshort.filters.skill_selection_failed", skill=exact_name, error=str(skill_err))
                        after = await self._skills_filter_badge()
                        if after > before:
                            registered = True
                            log.debug("cutshort.filters.skill_selected", skill=exact_name, attempt=attempt + 1)
                            break
                    if not registered:
                        misses.append(exact_name)
                        log.warning(
                            "cutshort.filters.skill_not_registered",
                            skill=exact_name,
                            profile=profile.name,
                        )

                await human_pause(400, 600)
                # Close popover by clicking filter button again
                await btn.click(force=True)
                await human_pause(600, 1000)
                final_badge = await self._skills_filter_badge()
                actual = final_badge - baseline if final_badge >= baseline else final_badge
                log.info(
                    "cutshort.filters.skills_applied",
                    profile=profile.name,
                    attempted=len(skills_to_select),
                    count=actual,
                )
                if misses:
                    log.warning(
                        "cutshort.filters.skills_missing",
                        profile=profile.name,
                        skills=misses,
                    )
        except Exception as exc:
            log.warning("cutshort.filters.skills_error", error=str(exc))

        log.info("cutshort.filters.applied_ui_successfully")
        await human_pause(2000, 3000)

    async def _set_recommendation_switch(self, want_on: bool) -> None:
        """Set the All-jobs recommendation switch ON (curated feed) or OFF
        (full inventory). Best-effort: a missing switch leaves the view as-is."""
        switch_el = await first_visible(
            self.page,
            [
                "input[role='switch']",
                "label:has(input[role='switch'])",
                "div:has-text('Turn it OFF to view all jobs') input[role='switch']",
            ],
            timeout_ms=3000,
        )
        if not switch_el:
            return
        try:
            is_checked = await switch_el.is_checked()
        except Exception:
            is_checked = True
        if is_checked == want_on:
            return
        try:
            await switch_el.scroll_into_view_if_needed()
            await human_pause(300, 600)
            try:
                await switch_el.click(force=True)
            except Exception:
                await switch_el.evaluate("el => el.click()")
            await human_pause(2000, 3000)
        except Exception as exc:
            log.debug("cutshort.switch_toggle_failed", want_on=want_on, error=str(exc))

    async def _scroll_feed(self, job_link_sel: str, target_pool: int, tab_name: str):
        """Infinite-scroll the current feed view until the pool target or a
        stagnant end-of-feed. Returns the collected link anchors."""
        last_link_count = 0
        stagnant_scrolls = 0
        while True:
            anchors = await self.page.locator(job_link_sel).all()
            link_count = len(anchors)
            if link_count >= target_pool:
                log.info("cutshort.fetch.target_pool_reached", tab=tab_name, count=link_count, target=target_pool)
                break
            if link_count == last_link_count:
                stagnant_scrolls += 1
                if stagnant_scrolls >= 5:
                    log.info("cutshort.fetch.stagnant_end_of_feed", tab=tab_name, count=link_count)
                    break
                # Scroll up slightly and then back down to kick intersection observer
                try:
                    await self.page.evaluate("window.scrollBy(0, -600)")
                    await human_pause(400, 700)
                except Exception:
                    pass
            else:
                stagnant_scrolls = 0
                last_link_count = link_count

            if anchors:
                try:
                    await anchors[-1].scroll_into_view_if_needed(timeout=2000)
                    await anchors[-1].hover(timeout=1000)
                except Exception:
                    pass

            # Deep scroll to document bottom to trigger infinite lazy load
            try:
                await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            await scroll_page(self.page, steps=4, delay_s=0.5)
            await human_pause(1500, 2500)

        anchors = await self.page.locator(job_link_sel).all()
        log.info("cutshort.fetch.cards_found", tab=tab_name, count=len(anchors))
        return anchors

    async def _parse_feed_anchors(
        self,
        anchors,
        tab_name: str,
        exclude_job_ids: set[str],
        seen_urls: set[str],
        jobs: list[Job],
    ) -> None:
        """Parse job cards from scrolled anchors into Job objects (shared by
        the recommended pass and the all-jobs pass). Appends in place so the
        recommended pass keeps global positions 1..N."""
        for anchor in anchors:
            try:
                url = (await anchor.get_attribute("href")) or ""
                if "/job/" not in url:
                    continue
                if url.startswith("/"):
                    url = f"https://cutshort.io{url}"
                if url.rstrip("/") in seen_urls:
                    continue

                title = (await safe_text(anchor)).strip()
                if not title:
                    log.debug(
                        "cutshort.fetch.empty_title_skipped",
                        url=url[:120],
                    )
                    continue
                seen_urls.add(url.rstrip("/"))

                company = ""
                desc = ""
                card_text = ""
                card = anchor.locator(
                    "xpath=ancestor::div[descendant::a[contains(@href,'/company/')]][1]"
                ).first
                if await card.count() > 0:
                    company = (await safe_text(card.locator("a[href*='/company/']").first)).strip()
                    prose_el = card.locator("div.prose").first
                    if await prose_el.count() > 0:
                        desc = (await safe_text(prose_el)).strip()

                # Comprehensive outer card containing action buttons and metadata (location, exp, salary)
                full_card = anchor.locator(
                    "xpath=ancestor::div[(contains(.,'yrs') or contains(.,'yr') or contains(.,'Exp')) and (descendant::button[contains(.,'Apply') or contains(.,'View') or contains(.,'Applied')])][1]"
                ).first
                if await full_card.count() == 0:
                    full_card = anchor.locator(
                        "xpath=ancestor::div[descendant::button[contains(.,'Apply') or contains(.,'View') or contains(.,'Applied')]][last()]"
                    ).first

                if await full_card.count() > 0:
                    card_text = (await safe_text(full_card)).strip()
                elif await card.count() > 0:
                    card_text = (await safe_text(card)).strip()

                min_sal, max_sal = _parse_cutshort_salary(card_text)
                min_exp, max_exp = _parse_cutshort_experience(card_text)
                posted_days = _parse_cutshort_posted(f"{title} {card_text}")

                match = re.search(r"-([a-zA-Z0-9]+)$", url)
                job_id = f"cutshort-{match.group(1)}" if match else f"cutshort-{Job.stable_id(url, title, company)}"

                if job_id in exclude_job_ids:
                    continue

                jobs.append(
                    Job(
                        job_id=job_id,
                        title=title,
                        company=company,
                        url=url,
                        description=desc,
                        min_salary_lpa=min_sal,
                        max_salary_lpa=max_sal,
                        min_experience=min_exp,
                        max_experience=max_exp,
                        posted_days_ago=posted_days,
                        recommendation_tab=tab_name,
                        recommendation_position=len(jobs) + 1,
                        platform="cutshort",
                    )
                )
            except Exception as exc:
                log.debug("cutshort.fetch.parse_error", error=str(exc))
                continue

    # ---- Dashboard overlay dismissal (runs 640+ user report) -------------
    # Cutshort surfaces two overlays on the dashboard that can swallow clicks
    # on the recommendation switch / filter chips:
    #   1. "Hey X, are you still looking for a job?" availability prompt.
    #      Answering "I am looking" is truthful AND keeps the talent card
    #      active ("visible to employers only when actively interviewing").
    #   2. "Submit feedback to companies" candidate-feedback survey (1/N).
    #      "Skip" dismisses it without leaving feedback on the user's behalf.
    # Signature-gated: we only touch buttons whose visible label exactly
    # matches the known overlay actions, never a generic close-everything
    # sweep that could dismiss an unrelated modal (e.g. a job pitch modal).
    async def _dismiss_dashboard_overlays(self) -> None:
        if await self._dismiss_availability_prompt():
            log.info("cutshort.overlay.availability_answered")
        if await self._skip_feedback_survey():
            log.info("cutshort.overlay.feedback_skipped")

    async def _dismiss_availability_prompt(self) -> bool:
        prompt = await first_visible(
            self.page, ["text=are you still looking for a job"], timeout_ms=900
        )
        if not prompt:
            return False
        if self.policy.dry_run:
            log.info("cutshort.overlay.availability_skipped_dry_run")
            return False
        looking_btn = self.page.locator(
            "button", has_text=re.compile(r"^\s*I am looking\s*$", re.I)
        ).first
        if await _safe_click(looking_btn):
            return True
        log.warning("cutshort.overlay.availability_button_missing")
        return False

    async def _skip_feedback_survey(self) -> bool:
        survey = await first_visible(
            self.page, ["text=submit feedback to companies"], timeout_ms=900
        )
        if not survey:
            return False
        skip_btn = self.page.locator(
            "button", has_text=re.compile(r"^\s*Skip\s*$", re.I)
        ).first
        if await _safe_click(skip_btn):
            return True
        log.warning("cutshort.overlay.skip_button_missing")
        return False

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        log.info("cutshort.fetch.start", profile=profile.name)
        await self.page.goto("https://cutshort.io/profile/all-jobs", wait_until="domcontentloaded")
        await human_pause(2000, 3000)

        # Availability prompt / feedback survey can overlap the feed controls
        # — clear them before touching the recommendation switch or filters.
        try:
            await self._dismiss_dashboard_overlays()
        except Exception as exc:
            log.debug("cutshort.overlay.dismiss_failed", error=str(exc)[:120])

        job_link_sel = "a[href*='/job/']"
        jobs: list[Job] = []
        seen_urls: set[str] = set()

        # PASS 1 — recommended feed first (switch ON): Cutshort's own
        # "recently active" curation outranks the raw grid. Bounded small.
        await self._set_recommendation_switch(True)
        await human_pause(1000, 2000)
        reco_anchors = await self._scroll_feed(job_link_sel, 50, "recommended")
        await self._parse_feed_anchors(reco_anchors, "recommended", exclude_job_ids, seen_urls, jobs)
        log.info("cutshort.fetch.recommended_done", count=len(jobs))

        # PASS 2 — full inventory overflow (switch OFF) + native UI filters.
        await self._set_recommendation_switch(False)
        await self._apply_ui_filters(profile)
        target_pool = max(100, min(profile.platform_limits.get(self.platform_name, 40) * 3, 150))
        all_anchors = await self._scroll_feed(job_link_sel, target_pool, "all_jobs")
        await self._parse_feed_anchors(all_anchors, "all_jobs", exclude_job_ids, seen_urls, jobs)

        # Dump AFTER scrolling so the artifact reflects what was actually parsed
        try:
            dump_path = self.artifacts.dir / "cutshort-dashboard.html"
            dump_path.write_text(await self.page.content(), encoding="utf-8")
            log.info("cutshort.dashboard_html_saved", path=str(dump_path))
        except Exception as exc:
            log.debug("cutshort.dashboard_dump_failed", error=str(exc))

        log.info("cutshort.fetch.done", count=len(jobs))
        return jobs

    async def _cloudflare_wall(self, *, blocked: bool = False) -> bool:
        """True when a genuine Cloudflare challenge is actively intercepting.

        Provenance: runs 375/376 failure dumps showed a real wall — empty
        modal root, a visible challenge widget, zero textareas. Runs 640/641
        then proved the old marker sniffing was wrong: Cutshort embeds
        ambient Turnstile artifacts on EVERY authenticated page — a
        ``challenges.cloudflare.com/turnstile/v0/api.js`` script loader plus
        a hidden ``<input name="cf-turnstile-response" id="cf-chl-widget-*" value="">``
        with zero iframes; verified identical on healthy dashboards where
        scrolling and filtering worked fine. The old ``blocked=True``
        short-circuit therefore misclassified every failed apply as a CF
        wall, pausing the platform and truncating the queue spuriously.

        Genuine wall evidence, any of:
          - interstitial title ("Just a moment", "Attention Required")
          - interstitial body copy only Cloudflare renders ("verifying you
            are human", "enable javascript and cookies to continue", ...)
          - challenge-platform URL artifacts (/cdn-cgi/challenge-platform,
            __cf_chl)
          - a VISIBLE Turnstile challenge iframe — the ambient script loader
            and hidden input never spawn one (run 641 dump: 0 iframes)

        ``blocked`` is advisory only (no actionable modal was found) and no
        longer implies a wall by itself.
        """
        del blocked
        try:
            html = (await self.page.content()).lower()
        except Exception:
            return False
        title_match = re.search(r"<title[^>]*>([^<]*)</title>", html)
        title_low = title_match.group(1).lower() if title_match else ""
        if any(sig in title_low for sig in ("just a moment", "attention required")):
            return True
        if any(
            copy in html
            for copy in (
                "verifying you are human",
                "checking your browser",
                "enable javascript and cookies to continue",
            )
        ):
            return True
        try:
            url_low = (self.page.url or "").lower()
        except Exception:
            url_low = ""
        if "cdn-cgi/challenge-platform" in url_low or "__cf_chl" in url_low:
            return True
        widget = await first_visible(
            self.page,
            ["iframe[src*='challenges.cloudflare.com']"],
            timeout_ms=800,
        )
        return widget is not None

    async def _capture_job_conversation(self, job_url: str) -> str | None:
        """Capture a View conversation link attached to this exact listing."""
        try:
            return await self.page.evaluate(
                """(jobUrl) => {
                    const exact = new URL(jobUrl).pathname.replace(/[/]$/, '');
                    for (const link of document.querySelectorAll('a[href*="thread="]')) {
                        if (!link.getClientRects().length || link.innerText.trim() !== 'View conversation') continue;
                        for (let node = link.parentElement; node && node !== document.body; node = node.parentElement) {
                            const jobs = [...node.querySelectorAll('a[href*="/job/"]')]
                                .map(a => new URL(a.href).pathname.replace(/[/]$/, ''));
                            if (!jobs.length) continue;
                            if (jobs.every(path => path === exact)) return link.href;
                            break;
                        }
                    }
                    // A dedicated listing can omit a self-link. Its candidate
                    // conversation still has to link back to this exact job.
                    if (location.pathname.replace(/[/]$/, '') === exact) {
                        const links = [...document.querySelectorAll('a[href*="thread="]')]
                            .filter(a => a.getClientRects().length && a.innerText.trim() === 'View conversation');
                        if (links.length === 1) return links[0].href;
                    }
                    return null;
                }""",
                job_url,
            )
        except Exception:
            return None

    async def _verify_job_conversation(self, conversation_url: str, job_url: str) -> bool:
        """Open the captured thread read-only and verify its own job link."""
        conversation = urlsplit(conversation_url)
        thread_id = parse_qs(conversation.query).get("thread", [])
        paths = {"/profile/conversations/candidate", "/profile/candidate-conversations"}
        if conversation.hostname not in {"cutshort.io", "www.cutshort.io"} or conversation.path not in paths or len(thread_id) != 1:
            return False
        thread_page = await self.page.context.new_page()
        try:
            await thread_page.goto(conversation_url, wait_until="domcontentloaded", timeout=20000)
            current = urlsplit(thread_page.url)
            if (
                current.hostname != conversation.hostname or current.path not in paths
                or parse_qs(current.query).get("thread", []) != thread_id
            ):
                return False
            link = await first_visible(
                thread_page, ["a:text-is('View full job description')"], timeout_ms=7000,
            )
            href = await link.get_attribute("href") if link is not None else None
            if not href:
                return False
            linked_job = urlsplit(urljoin(thread_page.url, href))
            target_job = urlsplit(job_url)
            return (
                linked_job.hostname == target_job.hostname
                and linked_job.path.rstrip("/") == target_job.path.rstrip("/")
            )
        except Exception:
            return False
        finally:
            await thread_page.close()

    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        self.require_mutation("application.apply_flow")
        log.info("cutshort.apply.start", job_id=job.job_id)

        raw_id = job.job_id.replace("cutshort-", "")

        # 1. Clean slate: dismiss any stale modal or backdrop from a previous job
        try:
            old_modal = await first_visible(self.page, ["#modal__content", "div[role='dialog']"], timeout_ms=300)
            if old_modal:
                await self.page.keyboard.press("Escape")
                await human_pause(300, 600)
        except Exception:
            pass

        # 1b. Cloudflare wall pre-flight: when the challenge widget is up, no
        # pitch modal can ever appear. Fail fast with a distinct reason so the
        # run pauses the platform instead of burning every queued job.
        if await self._cloudflare_wall():
            log.warning("cutshort.apply.cloudflare_wall", job_id=job.job_id)
            return ApplyOutcome(
                status=ApplicationStatus.FAILED,
                reason=SkipReason.CLOUDFLARE_CHALLENGE,
                detail="Cloudflare Turnstile challenge is intercepting Cutshort; pitch modal cannot appear",
            )

        modal = None

        if pre_submit_check is not None:
            target = urlsplit(job.url)
            if target.hostname not in {"cutshort.io", "www.cutshort.io"} or not target.path.startswith("/job/"):
                return ApplyOutcome(ApplicationStatus.FAILED, detail="Cutshort target listing URL is unavailable")
            detail_page = await self.page.context.new_page()
            try:
                await detail_page.goto(job.url, wait_until="domcontentloaded", timeout=20000)
                current = urlsplit(detail_page.url)
                if (current.hostname, current.path.rstrip("/")) != (target.hostname, target.path.rstrip("/")):
                    return ApplyOutcome(ApplicationStatus.FAILED, detail="Cutshort redirected away from the target listing")
                description = await first_visible(
                    detail_page,
                    ["div.prose", "[data-testid='job-description']"],
                    timeout_ms=7000,
                )
                job_description = (await description.inner_text(timeout=2000)).strip() if description is not None else ""
                if not job_description:
                    return ApplyOutcome(ApplicationStatus.FAILED, detail="Cutshort job description did not load before the pre-submit check")
                job.description = job_description
                minimum, maximum = extract_jd_experience(job_description)
                if minimum is not None:
                    job.min_experience, job.max_experience = minimum, maximum
                    job.experience_imputed = False
                decision = pre_submit_check(job)
                if not decision.passed:
                    return ApplyOutcome(ApplicationStatus.SKIPPED, reason=decision.reason, detail=decision.detail)
            except Exception as exc:
                return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Cutshort job detail check failed: {str(exc)[:150]}")
            finally:
                await detail_page.close()

        # Match the exact listing within the feed, including on a redirect.
        card_anchor = f"a:is([href$='/{raw_id}'], [href*='/{raw_id}?'], [href*='-{raw_id}?'], [href$='-{raw_id}'])"
        card_ancestor = (
            "xpath=ancestor::div[descendant::button[normalize-space(.)='Apply now'"
            " or normalize-space(.)='Apply to this job' or normalize-space(.)='Applied']"
            " or descendant::a[normalize-space(.)='View conversation']][1]"
        )
        anchor = await first_visible(self.page, [card_anchor], timeout_ms=2000)

        if anchor:
            card = anchor.locator(card_ancestor).first
            if await card.count() > 0:
                apply_btn = await first_visible(
                    card,
                    ["button:text-is('Apply now')", "button:text-is('Apply to this job')"],
                    timeout_ms=2500,
                )
                if not apply_btn and await first_visible(card, ["button:has-text('Applied')", "a:has-text('View conversation')", "button:has-text('View conversation')"]):
                    return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)

                if apply_btn:
                    try:
                        await apply_btn.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    try:
                        await apply_btn.click(force=True, timeout=3000)
                    except Exception:
                        await apply_btn.evaluate("el => el.click()")
                    await human_pause(1500, 2500)

                    modal = await first_visible(
                        self.page,
                        [
                            "#modal__content",
                            "div.modal__wrapper",
                            "div[role='dialog']",
                            "div#modal-root > div",
                            "div#modal-root div[class*='Modal']",
                            "div#modal-root div[class*='modal']",
                            "div.modal-content",
                            "div[class*='modal']",
                            "form:has(textarea)",
                            "div:has(> textarea[name='message'])",
                        ],
                        timeout_ms=3500,
                    )

        # 3. Fallback or Dedicated Flow: If modal didn't open from feed card, navigate to job.url
        if not modal:
            if not (job.url and job.url.startswith("http")):
                return ApplyOutcome(
                    status=ApplicationStatus.SKIPPED,
                    reason=SkipReason.STALE_JOB,
                    detail=f"No feed card or URL for {job.title} at {job.company}",
                )

            log.info("cutshort.apply.navigating_to_job_url", job_id=job.job_id, url=job.url)
            try:
                await self.page.goto(job.url, wait_until="domcontentloaded")
                await human_pause(1500, 2500)
            except Exception as exc:
                return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Failed to load job page: {str(exc)[:150]}")

            # Check for Already Applied on dedicated job page
            already_applied_marker = await first_visible(
                self.page,
                [
                    "button:has-text('Applied')",
                    "a:has-text('View conversation')",
                    "button:has-text('View conversation')",
                    "text=Already applied",
                    "text=You have already applied",
                    "div:has-text('Already applied')",
                ],
                timeout_ms=1500,
            )
            if already_applied_marker:
                log.info("cutshort.apply.already_applied_on_job_page", job_id=job.job_id)
                return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)

            # Check for Inactive / Closed Job on dedicated job page
            closed_marker = await first_visible(
                self.page,
                [
                    "text=no longer active",
                    "text=job has expired",
                    "text=applications are closed",
                    "text=paused hiring",
                    "text=Job is closed",
                    "text=This job is no longer available",
                ],
                timeout_ms=1200,
            )
            if closed_marker:
                log.info("cutshort.apply.job_closed_on_page", job_id=job.job_id)
                return ApplyOutcome(ApplicationStatus.SKIPPED, reason=SkipReason.STALE_JOB, detail="Job posting is no longer active / closed on Cutshort")

            # Step 3A: On dedicated job page (cutshort.io/job/...), click primary in-viewport CTA button
            # The main job's primary CTA is 'Apply to this job' (in header & floating bar).
            # We prioritize 'Apply to this job' so we never accidentally click 'Apply now' on 'Similar jobs' below!
            target_btn = await first_visible(
                self.page,
                [
                    "button:text-is('Apply to this job')",
                    "a:text-is('Apply to this job')",
                ],
                timeout_ms=3000,
            )

            if target_btn:
                try:
                    await target_btn.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                try:
                    await target_btn.click(force=True, timeout=3000)
                except Exception:
                    await target_btn.evaluate("el => el.click()")
                await human_pause(1500, 2500)

            # Check if modal appeared directly from Step 3A CTA click
            modal = await first_visible(
                self.page,
                [
                    "#modal__content",
                    "div.modal__wrapper",
                    "div[role='dialog']",
                    "div#modal-root > div",
                    "div#modal-root div[class*='Modal']",
                    "div#modal-root div[class*='modal']",
                    "div.modal-content",
                    "div[class*='modal']",
                    "form:has(textarea)",
                    "div:has(> textarea[name='message'])",
                ],
                timeout_ms=3000,
            )

            # Step 3B: Only if modal did NOT appear and the page redirected to /profile/all-jobs?jobid=...
            # check the feed card and click its 'Apply now' button
            if not modal and "/profile/all-jobs" in self.page.url:
                redirect_anchor = await first_visible(self.page, [card_anchor], timeout_ms=3000)
                feed_apply = None
                if redirect_anchor is not None:
                    card = redirect_anchor.locator(card_ancestor).first
                    feed_apply = await first_visible(
                        card,
                        ["button:text-is('Apply now')", "button:text-is('Apply to this job')"],
                        timeout_ms=3000,
                    )
                if feed_apply is None and await self._capture_job_conversation(job.url):
                    return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)

                if feed_apply:
                    try:
                        await feed_apply.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    try:
                        await feed_apply.click(force=True, timeout=3000)
                    except Exception:
                        await feed_apply.evaluate("el => el.click()")
                    await human_pause(1500, 2500)

                    modal = await first_visible(
                        self.page,
                        [
                            "#modal__content",
                            "div.modal__wrapper",
                            "div[role='dialog']",
                            "div#modal-root > div",
                            "div#modal-root div[class*='Modal']",
                            "div#modal-root div[class*='modal']",
                            "div.modal-content",
                            "div[class*='modal']",
                            "form:has(textarea)",
                            "div:has(> textarea[name='message'])",
                        ],
                        timeout_ms=4000,
                    )

        modal_text = (await safe_text(modal)).strip() if modal else ""

        # Only treat typographic dashes as equivalent. Match the full role and employer.
        heading_text = " ".join(f"You are applying for {job.title} at {job.company}".split())
        heading_body = "".join(
            "[-–—−]" if c in "-–—−" else r"\s+" if c == " " else re.escape(c)
            for c in heading_text
        )
        heading = re.compile(rf"^{heading_body}$", re.IGNORECASE)
        heading_sub = re.compile(heading_body, re.IGNORECASE)
        modal_matches_job = False
        if modal is not None:
            try:
                modal_matches_job = await modal.get_by_text(heading).first.is_visible()
            except Exception:
                pass
            if not modal_matches_job and modal_text:
                if heading_sub.search(modal_text):
                    modal_matches_job = True
                else:
                    clean_modal = re.sub(r"[^\w\s]", " ", modal_text).lower()
                    clean_modal_squashed = re.sub(r"\s+", "", clean_modal)
                    norm_company = re.sub(r"[^\w\s]", " ", job.company).lower().strip()
                    norm_company_squashed = re.sub(r"\s+", "", norm_company)
                    company_ok = (
                        not norm_company
                        or norm_company in clean_modal
                        or (norm_company_squashed and norm_company_squashed in clean_modal_squashed)
                    )
                    if company_ok:
                        title_tokens = [w for w in re.sub(r"[^\w\s]", " ", job.title).lower().split() if len(w) > 2]
                        matched_tokens = sum(1 for w in title_tokens if w in clean_modal)
                        if not title_tokens or (matched_tokens / len(title_tokens)) >= 0.5:
                            modal_matches_job = True

        resume_path, target_track = _track_resume_for(job, modal_text)
        resume_verified = resume_path is None
        resume_failure_path = None
        if modal_matches_job and resume_path is not None and resume_path.is_file():
            resume_pattern = _resume_label_pattern(resume_path)
            selected_resume = modal.get_by_text(resume_pattern).first
            stem_base = resume_path.stem.rstrip("_")
            # Substring pattern that still preserves distinct AI (_2026) vs FullStack (_2026_1) tracks
            distinct_sub = (
                re.compile(re.escape(stem_base), re.IGNORECASE)
                if stem_base != resume_path.stem or "_1" in stem_base
                else re.compile(rf"{re.escape(stem_base)}(?!_1\b)", re.IGNORECASE)
            )
            broad_resume = modal.get_by_text(distinct_sub).first
            try:
                is_vis = await selected_resume.is_visible()
                if not is_vis:
                    try:
                        is_vis = await broad_resume.is_visible()
                    except Exception:
                        is_vis = False
                if not is_vis:
                    upload = await first_visible(
                        modal,
                        [
                            "text='Upload another resume'",
                            "text='Upload new resume'",
                            "text='Upload resume'",
                        ],
                        timeout_ms=2500,
                    )
                    if upload is None:
                        upload = modal.get_by_text("Upload another resume", exact=True)
                    async with self.page.expect_file_chooser(timeout=4000) as chooser:
                        await upload.click(timeout=2500)
                    await (await chooser.value).set_files(str(resume_path), timeout=4000)
                try:
                    await selected_resume.wait_for(state="visible", timeout=8000)
                    resume_verified = True
                except Exception:
                    await broad_resume.wait_for(state="visible", timeout=5000)
                    resume_verified = True
            except Exception as exc:
                log.warning("cutshort.apply.resume_unverified", job_id=job.job_id, track=target_track, error=str(exc)[:120])
                resume_failure_path = await self.artifacts.capture_failure(
                    self.page, "cutshort-resume-unverified", profile_name, job.job_id,
                )

        recruiter_name = "Hiring Team"
        rec_match = re.search(r"To\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", modal_text)
        if rec_match:
            full_name = rec_match.group(1).strip()
            if " " in full_name and not any(w in full_name.lower() for w in ["team", "manager", "hr", "recruiter"]):
                recruiter_name = full_name.split()[0]
            else:
                recruiter_name = full_name

        actual_title = job.title
        actual_company = job.company

        # Clean formal entity suffixes (Private Limited, Pvt Ltd, LLP, Inc, etc.)
        clean_company = re.sub(
            r",?\s*(?:Private\s+Limited|Pvt\.?\s*Ltd\.?|LLP|Inc\.?|L\.?L\.?C\.?|Corp\.?|Corporation)\b.*$",
            "",
            actual_company,
            flags=re.IGNORECASE,
        ).strip() or actual_company
        # Shorter conversational company name for body/closing
        short_company = re.sub(
            r"\s+(?:Careers|Technologies|Solutions|Services|Labs|Software|Consulting)$",
            "",
            clean_company,
            flags=re.IGNORECASE,
        ).strip() or clean_company

        # Dynamic tech stack, specialty, and achievement tailored to the role
        text_for_skills = f"{actual_title} {modal_text} {job.description}".lower()
        # Achievements come only from config.yaml `candidate.achievements`.
        # With none configured, leave the proof clause out instead of inventing
        # a claim the candidate never made.
        _cand = getattr(self.config, "candidate", None) if self.config is not None else None
        achievements = list(getattr(_cand, "achievements", None) or [])

        def _pick(idx: int) -> str:
            if idx < len(achievements):
                return achievements[idx]
            return achievements[0] if achievements else ""
        if any(k in text_for_skills for k in ["ai", "llm", "genai", "gpt", "machine learning", "rag", "agent", "prompt"]):
            key_tech_stack = "Python, FastAPI, GenAI, and LLM orchestration (RAG/LangChain)"
            core_specialty = "autonomous AI agents, vector search pipelines, and scalable Python backends"
            specific_achievement = _pick(0)
        elif any(k in text_for_skills for k in ["qa", "testing", "test automation", "sdet"]):
            key_tech_stack = "Python, Playwright, Selenium, and API automation frameworks"
            core_specialty = "robust automated testing pipelines and CI/CD quality gates"
            specific_achievement = _pick(1)
        elif any(k in text_for_skills for k in ["devops", "docker", "kubernetes", "aws", "gcp", "azure"]):
            key_tech_stack = "Docker, Kubernetes, AWS, and CI/CD pipelines"
            core_specialty = "scalable cloud infrastructure and containerized deployments"
            specific_achievement = _pick(2)
        elif "python" in text_for_skills and any(k in text_for_skills for k in ["fullstack", "full stack"]):
            key_tech_stack = "Python (FastAPI/Django), React, and TypeScript"
            core_specialty = "scalable full-stack web applications and robust microservices"
            specific_achievement = _pick(3)
        elif any(k in text_for_skills for k in ["backend", "python", "fastapi", "django"]):
            key_tech_stack = "Python, FastAPI, Django, and PostgreSQL"
            core_specialty = "high-concurrency backend services and database architectures"
            specific_achievement = _pick(4)
        elif any(k in text_for_skills for k in ["frontend", "react", "next"]):
            key_tech_stack = "React, Next.js, and TypeScript"
            core_specialty = "responsive web applications and performant frontend architectures"
            specific_achievement = _pick(5)
        elif any(k in text_for_skills for k in ["fullstack", "full stack", "node"]):
            key_tech_stack = "React, Node.js, TypeScript, and Python"
            core_specialty = "scalable full-stack web applications and robust microservices"
            specific_achievement = _pick(6)
        else:
            key_tech_stack = "Python, Node.js, React, and modern web architectures"
            core_specialty = "scalable backend and full-stack software systems"
            specific_achievement = _pick(7)

        from ..core.gemini_writer import applicant_snapshot
        who = applicant_snapshot()
        cfg = AgentConfig.load()
        applicant_name = who.name if who.name != "a Software Engineer" else (cfg.applicant_name or "").strip()

        links = []
        if cfg.applicant_github:
            links.append(cfg.applicant_github)
        if cfg.applicant_linkedin:
            links.append(cfg.applicant_linkedin)
        links_block = f"\n{' | '.join(links)}" if links else ""

        modal_scope = modal or self.page
        textarea = await first_visible(
            modal_scope,
            [
                "textarea[name='message']",
                "textarea[placeholder*='message' i]",
                "textarea",
                "input[type='text'][placeholder*='note' i]",
                "input[type='text'][placeholder*='message' i]",
            ],
            timeout_ms=3000,
        )
        if not modal and textarea:
            try:
                wrapper = textarea.locator("xpath=ancestor::div[@id='modal-root']/div[1] | ancestor::div[contains(@class,'modal') or contains(@class,'Modal')][1] | ancestor::form[1]").first
                if await wrapper.count() > 0:
                    modal = wrapper
            except Exception:
                pass
        proof_clause = (
            f"—recently delivering {specific_achievement}." if specific_achievement else "."
        )
        if who.experience_label:
            intro_line = (
                f"I have {who.experience_label} of experience. "
                f"My focus is building {core_specialty}{proof_clause}"
            )
        else:
            intro_line = f"I specialize in building {core_specialty}{proof_clause}"
        availability_line = (
            f"I’m interested in work involving {key_tech_stack}. Notice period: {who.notice_label}."
            if who.notice_label
            else f"I’m interested in work involving {key_tech_stack}."
        )
        if modal_matches_job and textarea:
            pitch = (
                f"Hi {recruiter_name},\n\n"
                f"I noticed the {actual_title} opening at {clean_company} and wanted to reach out directly.\n\n"
                f"{intro_line}\n\n"
                f"{availability_line}\n\n"
                f"I'd be glad to discuss how my background can support {short_company}'s engineering goals—let me know when you have a few minutes to connect.\n\n"
                f"Best,\n"
                f"{applicant_name}{links_block}"
            )
            try:
                await textarea.scroll_into_view_if_needed(timeout=1000)
                await textarea.click()
            except Exception:
                pass
            # NOTE: do NOT call dismiss_overlays() inside this modal flow — it
            # ends with an unconditional Escape that closes the pitch modal
            # itself (run 397 headed: 5/5 "pitch textarea gone" came from
            # exactly that call placed here). Overlays were cleared at flow
            # entry; the modal is the foreground by construction.
            try:
                await textarea.fill(pitch, timeout=5000)
            except Exception:
                # The resolved handle can detach on React re-render (runs
                # 388/391: 10s timeouts on the dialog chain). Re-resolve from
                # the PAGE — the old modal handle may itself be detached —
                # then give up. Never submit an empty pitch.
                # (Early return skips the feed nav-back below; the next job's
                # clean slate + URL fallback covers it.)
                try:
                    fresh_modal = await first_visible(
                        self.page,
                        [
                            "#modal__content",
                            "div.modal__wrapper",
                            "div[role='dialog']",
                            "div#modal-root > div",
                            "div#modal-root div[class*='Modal']",
                            "div#modal-root div[class*='modal']",
                            "div.modal-content",
                            "div[class*='modal']",
                            "form:has(textarea)",
                            "div:has(> textarea[name='message'])",
                        ],
                        timeout_ms=2000,
                    )
                    fresh_scope = fresh_modal or self.page
                    fresh = await first_visible(
                        fresh_scope,
                        ["textarea[name='message']", "textarea[placeholder*='message' i]", "textarea"],
                        timeout_ms=2000,
                    )
                    if fresh is None:
                        raise RuntimeError("pitch textarea gone")
                    textarea = fresh
                    modal = fresh_modal or modal
                    await textarea.fill(pitch, timeout=5000)
                except Exception as exc2:
                    try:
                        dump_path = self.artifacts.dir / f"cutshort-pitch-failed-{raw_id}.html"
                        dump_path.write_text(await self.page.content(), encoding="utf-8")
                        log.info("cutshort.pitch_failed_dump", path=str(dump_path))
                    except Exception:
                        pass
                    try:
                        await self.page.keyboard.press("Escape")
                        await human_pause(300, 500)
                    except Exception:
                        pass
                    return ApplyOutcome(
                        ApplicationStatus.FAILED,
                        detail=f"cutshort pitch fill failed for {job.title} at {job.company}: {str(exc2)[:120]}",
                    )
            # Crucial: dispatch synthetic events so React updates controlled component state
            await textarea.dispatch_event("input")
            await textarea.dispatch_event("change")
            await human_pause(300, 600)

        send_btn = None
        if modal_matches_job and resume_verified and textarea is not None:
            send_btn = await first_visible(modal, ["button:text-is('Send')"], timeout_ms=2500)

        submission_attempted = False
        conversation_url = None
        if send_btn is not None:
            self.require_mutation("application.final_submit")
            prior_conversation = await self._capture_job_conversation(job.url)
            if prior_conversation:
                return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)

            try:
                before = self.artifacts.dir / f"cutshort-pitch-before-send-{raw_id}.html"
                before.write_text(await self.page.content(), encoding="utf-8")
            except Exception:
                pass

            submission_attempted = True
            try:
                await send_btn.click(timeout=5000)
            except Exception as exc:
                log.warning("cutshort.apply.send_click_uncertain", job_id=job.job_id, error=str(exc)[:120])

            # Cutshort replaces the same card's actions with View conversation.
            # The listing can disappear if reopened, so observe this page only.
            for _ in range(12):
                conversation_url = await self._capture_job_conversation(job.url)
                if conversation_url:
                    break
                await human_pause(650, 900)

            try:
                after = self.artifacts.dir / f"cutshort-pitch-after-send-{raw_id}.html"
                after.write_text(await self.page.content(), encoding="utf-8")
            except Exception:
                pass

        confirmed = bool(conversation_url and await self._verify_job_conversation(conversation_url, job.url))
        log.info(
            "cutshort.apply.conversation_confirmation",
            job_id=job.job_id,
            link_seen=bool(conversation_url),
            confirmed=confirmed,
        )
        if confirmed:
            if not hasattr(self, "_applied_conversations"):
                self._applied_conversations = {}
            self._applied_conversations[job.url] = conversation_url
            outcome = ApplyOutcome(
                ApplicationStatus.APPLIED,
                detail="Application confirmed",
                confirmation_type="dom_marker",
                confirmation_evidence="New Cutshort View conversation links back to the exact job",
            )
        elif submission_attempted:
            outcome = ApplyOutcome(
                ApplicationStatus.FAILED,
                reason=SkipReason.SUBMISSION_UNCONFIRMED,
                detail="Cutshort Send attempted without a verified conversation for the exact job",
            )
        elif modal is not None and not modal_matches_job:
            outcome = ApplyOutcome(
                ApplicationStatus.FAILED,
                detail=f"Cutshort pitch dialog did not identify {job.title} at {job.company}",
            )
        elif not resume_verified:
            outcome = ApplyOutcome(
                ApplicationStatus.FAILED,
                detail=f"Cutshort could not verify the selected {target_track} résumé",
                screenshot_path=resume_failure_path,
            )
        elif await self._cloudflare_wall(blocked=True):
            outcome = ApplyOutcome(
                ApplicationStatus.FAILED,
                reason=SkipReason.CLOUDFLARE_CHALLENGE,
                detail=f"Cloudflare challenge wall for {job.title} at {job.company}",
            )
        else:
            outcome = ApplyOutcome(
                ApplicationStatus.FAILED,
                detail=f"Cutshort pitch modal or Send button unavailable for {job.title} at {job.company}",
            )

        if not confirmed and modal is not None:
            try:
                cancel = await first_visible(modal, ["button:text-is('Cancel')"], timeout_ms=700)
                if cancel is not None:
                    await cancel.click(timeout=1000)
            except Exception:
                pass

        # Always ensure browser returns cleanly to /profile/all-jobs feed for subsequent jobs
        if "/profile/all-jobs" not in self.page.url:
            try:
                await self.page.goto("https://cutshort.io/profile/all-jobs", wait_until="domcontentloaded")
                await human_pause(800, 1500)
            except Exception:
                pass

        return outcome

    # ---------------------------------------------------------
    # Phase 2 Message Handling (Questionnaire Sweep)
    # ---------------------------------------------------------
    async def handle_messages(self, *, deadline: float | None = None) -> list[dict]:
        """Inspect newly applied jobs, the Pending backlog and recent inbox threads."""
        self.require_mutation("messages.answer_flow")
        timeout_minutes = (getattr(self, "config", None) or AgentConfig.load()).run.cutshort_questionnaire_timeout_minutes
        sweep_deadline = monotonic() + timeout_minutes * 60
        deadline = min(deadline, sweep_deadline) if deadline is not None else sweep_deadline
        self.questionnaire_stats = dict(checked=0, submitted=0, already_submitted=0,
                                        stale=0, unknown_age=0, unconfirmed=0, incomplete=0)
        unanswered: list[dict] = []
        seen_jobs: set[str] = set()
        base_url = "https://cutshort.io/profile/candidate-conversations"
        log.info("cutshort.messages.start")
        for job_url, conversation_url in getattr(self, "_applied_conversations", {}).items():
            if deadline is not None and monotonic() >= deadline:
                self.questionnaire_stats["incomplete"] = 1
                break
            try:
                remaining = max(0, deadline - monotonic()) if deadline is not None else None
                async with asyncio.timeout(remaining):
                    await self.page.goto(conversation_url, wait_until="domcontentloaded", timeout=20000)
                    current = urlsplit(self.page.url)
                    expected = urlsplit(conversation_url)
                    if (current.hostname != expected.hostname
                        or current.path not in {"/profile/conversations/candidate", "/profile/candidate-conversations"}
                        or parse_qs(current.query).get("thread") != parse_qs(expected.query).get("thread")):
                        raise ValueError("Cutshort conversation changed while opening the thread")
                    pane = self.page.locator("div[class*='sc-259ba717-13']:visible").filter(
                        has=self.page.get_by_text("View full job description", exact=True),
                    )
                    href = await pane.get_by_text("View full job description", exact=True).get_attribute(
                        "href", timeout=7000,
                    )
                    if not href or self._message_job_path(href) != self._message_job_path(job_url):
                        raise ValueError("Cutshort conversation points to a different job")
                    if await self._answer_current_chat(pane, job_url, unanswered, False, wait_for_widget=True):
                        seen_jobs.add(self._message_job_path(job_url))
            except TimeoutError:
                self.questionnaire_stats["incomplete"] = 1
                log.warning("cutshort.messages.time_budget_exhausted", phase="new_applications")
                break
            except Exception as exc:
                self.questionnaire_stats["unconfirmed"] += 1
                log.warning("cutshort.messages.applied_thread_unverified", job=job_url, error=str(exc)[:150])
        for pending in (True, False):
            if deadline is not None and monotonic() >= deadline:
                self.questionnaire_stats["incomplete"] = 1
                break
            phase_deadline = deadline
            if pending and deadline is not None:
                rem = max(0.0, deadline - monotonic())
                # Keep up to 25% (max 3m) for Inbox so a large Pending backlog
                # cannot starve fresh 24h inbox questionnaires.
                phase_deadline = deadline - min(180.0, rem * 0.25)
            try:
                url = f"{base_url}?stage=awaiting" if pending else base_url
                await self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
                if pending:
                    tab = self.page.locator("#tab-awaiting")
                    await tab.wait_for(state="visible", timeout=8000)
                    if await tab.get_attribute("aria-selected") != "true":
                        await tab.click(timeout=3000)
                panel = self.page.locator("#tabpanel-awaiting:visible" if pending else "div[role='tabpanel']:visible").first
                await panel.wait_for(state="visible", timeout=8000)
                remaining = max(0, phase_deadline - monotonic()) if phase_deadline is not None else None
                async with asyncio.timeout(remaining):
                    ready = await first_visible(panel, [
                        "ul[role='list'] > div[role='button']",
                        ":text-is('No conversations here.')",
                        ":text-is('No conversations here')",
                    ], timeout_ms=10000)
                    if ready is None:
                        raise ValueError("Cutshort conversation rows or empty state did not load")
                    await self._scan_message_rows(
                        panel, set(), unanswered, phase_deadline, pending=pending, seen_jobs=seen_jobs,
                    )
            except TimeoutError:
                self.questionnaire_stats["incomplete"] = 1
                log.warning("cutshort.messages.time_budget_exhausted", phase="pending" if pending else "inbox")
                if not pending or (deadline is not None and monotonic() >= deadline):
                    break
            except Exception as exc:
                self.questionnaire_stats["incomplete"] = 1
                log.warning("cutshort.messages.scan_failed", pending=pending, error=str(exc)[:150])
        self.questionnaire_stats["questions"] = len(unanswered)
        log.info("cutshort.messages.done", **self.questionnaire_stats)
        return unanswered

    @staticmethod
    def _message_job_path(href: str) -> str:
        job = urlsplit(urljoin("https://cutshort.io", href))
        if job.hostname not in {"cutshort.io", "www.cutshort.io"} or not job.path.startswith("/job/"):
            raise ValueError("Cutshort conversation has no supported job link")
        return job.path.rstrip("/")

    @staticmethod
    def _message_age_hours(text: str) -> float | None:
        text = text.strip().lower()
        if text in {"just now", "today"} or re.fullmatch(r"a few (?:seconds|minutes) ago", text):
            return 0
        if text == "yesterday":
            return None  # A calendar label alone cannot establish a rolling 24-hour age.
        match = re.fullmatch(
            r"(an?|\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|[smhdwy])\s*ago",
            text,
        )
        if not match:
            return None
        amount = 1 if match[1] in {"a", "an"} else int(match[1])
        unit = match[2]
        if unit.startswith("mo"):
            return amount * 720
        return amount * {"s": 1 / 3600, "m": 1 / 60, "h": 1, "d": 24,
                         "w": 168, "y": 8760}[unit[0]]

    async def _resolve_message_pane(
        self, title: str, company: str, prev_href: str = "", timeout_ms: int = 6000,
    ) -> tuple[Locator, str]:
        """Locate the active conversation pane matching (title, company), handling
        anonymized agency threads ('undefined') and whitespace variance."""
        base_pane = self.page.locator("div[class*='sc-259ba717-13']:visible")
        title_pane = base_pane.filter(has=self.page.get_by_text(title, exact=True))
        strict_pane = title_pane.filter(has=self.page.get_by_text(company, exact=True))
        company_low = company.strip().lower()
        norm_company = " ".join(company_low.split())
        pane_deadline = monotonic() + timeout_ms / 1000.0
        matched_pane: Locator | None = None

        while monotonic() < pane_deadline:
            if company_low != "undefined" and await strict_pane.count() > 0:
                matched_pane = strict_pane.first
                break
            if await title_pane.count() > 0:
                cand = title_pane.first
                info = await cand.evaluate("""el => {
                    const norm = s => (s || '').trim().replace(/\\s+/g, ' ').toLowerCase();
                    const vjd = [...el.querySelectorAll('a, button, span, div')]
                        .find(n => (n.textContent || '').trim() === 'View full job description');
                    const href = vjd ? (vjd.getAttribute('href') || '') : '';
                    return {
                        text: norm(el.innerText || el.textContent),
                        href,
                    };
                }""")
                cur_href = (info or {}).get("href") or ""
                pane_text = (info or {}).get("text") or ""
                # Only treat a title-only pane as switched once its href moves
                # off prev_href (when prev_href was populated) OR company/agency fits.
                href_switched = (not prev_href) or (bool(cur_href) and cur_href != prev_href)
                if company_low == "undefined" and href_switched and ("agency job" in pane_text or cur_href):
                    matched_pane = cand
                    break
                if norm_company and norm_company in pane_text and href_switched:
                    matched_pane = cand
                    break
            await asyncio.sleep(0.15)

        if matched_pane is None:
            raise ValueError(f"Cutshort conversation pane did not load for {company} | {title}")

        vjd = matched_pane.get_by_text("View full job description", exact=True)
        if await vjd.count() == 0:
            vjd_deadline = monotonic() + 1.5
            while monotonic() < vjd_deadline:
                if await vjd.count() > 0:
                    break
                await asyncio.sleep(0.15)
        if await vjd.count() > 0:
            href = await vjd.first.get_attribute("href", timeout=2000) or ""
        else:
            # Closed or deleted job threads keep the conversation pane without a
            # job-description link; proceed only if conversation content exists.
            href = ""
        return matched_pane, href

    async def _scan_message_rows(
        self, panel: Locator, processed: set[tuple[str, str]], unanswered: list[dict], deadline: float | None,
        *, pending: bool = False, seen_jobs: set[str] | None = None,
    ) -> None:
        rows = panel.locator("ul[role='list'] > div[role='button']")
        if seen_jobs is None:
            seen_jobs = set()
        at_end = False
        # Use the conversation row's company and full job title, not its changing preview.
        while deadline is None or monotonic() < deadline:
            previous_count = len(processed)
            batch = await rows.evaluate_all(r"""rows => rows.map((row, domIdx) => {
                if (!row.getClientRects().length) return null;
                const normalize = text => text.trim().replace(/\s+/g, ' ');
                const title = [...row.querySelectorAll('div')].find(
                    el => el.children.length === 0 && normalize(el.textContent).startsWith('For job ')
                );
                const img = row.querySelector("img[alt$=' profile picture']");
                const rawAlt = img ? (img.getAttribute('alt') || img.alt || '') : '';
                return {
                    title: title ? normalize(title.textContent) : '',
                    company: rawAlt.replace(/ profile picture$/, '').trim(),
                    alt: rawAlt,
                    age: row.querySelector('.conversation-date')?.textContent || '',
                    questionnaire: row.innerText.includes('[Questionnaire]'),
                    dom_idx: domIdx,
                };
            }).filter(Boolean)""")
            for item in batch:
                if deadline is not None and monotonic() >= deadline:
                    self.questionnaire_stats["incomplete"] = 1
                    return
                company, title = item["company"], item["title"]
                key = (company, title)
                if not all(key):
                    # A visible conversation row with no exact identity needs investigation.
                    log.warning("cutshort.messages.row_identity_unavailable")
                    self.questionnaire_stats["incomplete"] = 1
                    continue
                if key in processed:
                    continue
                processed.add(key)
                expected_questionnaire = item["questionnaire"]
                if not pending and not expected_questionnaire:
                    age = self._message_age_hours(item["age"])
                    if age is None or age >= 24:
                        self.questionnaire_stats["unknown_age" if age is None else "stale"] += 1
                        continue
                try:
                    alt_label = item.get("alt") or f"{company} profile picture"
                    row = rows.filter(has=self.page.get_by_text(title, exact=True)).filter(
                        has=self.page.get_by_alt_text(alt_label, exact=True),
                    )
                    row_count = await row.count() if isinstance(row, Locator) else None
                    if isinstance(row_count, int):
                        prev_href = ""
                        cur_vjd = self.page.locator("div[class*='sc-259ba717-13']:visible").get_by_text(
                            "View full job description", exact=True,
                        ).first
                        try:
                            if await cur_vjd.count() > 0:
                                prev_href = await cur_vjd.get_attribute("href", timeout=500) or ""
                        except Exception:
                            prev_href = ""
                        click_target = row
                        if row_count != 1:
                            if "dom_idx" in item:
                                click_target = rows.nth(item["dom_idx"])
                            elif row_count > 1:
                                click_target = row.first
                        try:
                            await click_target.click(timeout=4000)
                        except Exception as click_exc:
                            if "strict mode violation" in str(click_exc).lower():
                                await click_target.first.click(timeout=4000)
                            else:
                                await click_target.scroll_into_view_if_needed(timeout=1500)
                                await click_target.evaluate("el => el.click()")
                        pane, href = await self._resolve_message_pane(title, company, prev_href=prev_href)
                    else:
                        await row.click(timeout=4000)
                        pane = self.page.locator("div[class*='sc-259ba717-13']:visible").filter(
                            has=self.page.get_by_text(title, exact=True),
                        ).filter(has=self.page.get_by_text(company, exact=True))
                        href = await pane.get_by_text("View full job description", exact=True).get_attribute(
                            "href", timeout=6000,
                        )
                    job_path = (
                        self._message_job_path(href)
                        if href
                        else (f"thread:{company}:{title}" if isinstance(row_count, int) else self._message_job_path(""))
                    )
                    if job_path not in seen_jobs and await self._answer_current_chat(
                        pane, key, unanswered, expected_questionnaire,
                    ):
                        seen_jobs.add(job_path)
                except Exception as exc:
                    self.questionnaire_stats["unconfirmed"] += 1
                    log.warning("cutshort.messages.thread_unverified", item=key, error=str(exc)[:120])
                    if self.questionnaire_stats["unconfirmed"] <= 3:
                        try:
                            await self.artifacts.dump_html(
                                self.page, "messages-unverified", "Cutshort",
                                str(self.questionnaire_stats["unconfirmed"]),
                            )
                        except Exception:
                            pass
            if len(processed) > previous_count:
                at_end = False
                continue
            # Scroll the actual row container. At its end there is no further batch.
            moved = await rows.count() > 0 and await rows.last.evaluate("""row => {
                let box = row.parentElement;
                while (box && box.scrollHeight <= box.clientHeight + 1) box = box.parentElement;
                if (!box) return false;
                const before = box.scrollTop;
                box.scrollTop += Math.max(box.clientHeight - 40, 100);
                return box.scrollTop > before;
            }""")
            if not moved and at_end:
                return
            at_end = not moved
            await human_pause(400, 750)
        self.questionnaire_stats["incomplete"] = 1

    async def _answer_current_chat(
        self, pane: Locator, key: str | tuple[str, str], unanswered: list[dict],
        expected_questionnaire: bool, *, wait_for_widget: bool = False,
    ) -> bool:
        """Inspect only the verified conversation pane."""
        self.questionnaire_stats["checked"] += 1
        index = self.questionnaire_stats["checked"]
        widget_selector = (
            "div:has(> #message_response_type_screening):has(form:visible:is("
            ":has(fieldset), :has(textarea), :has(button:text-is('Submit')), "
            ":has(button:text-is('Send response')), :has(:text-is('Questionnaire submitted!')), "
            ":has(:text-is('Your response has been recorded'))))"
        )
        widgets = pane.locator(widget_selector)
        if expected_questionnaire or wait_for_widget:
            ready = await first_visible(pane, [widget_selector], timeout_ms=5000)
            if ready is None and expected_questionnaire:
                await self.artifacts.dump_html(self.page, "questionnaire-missing", "Cutshort", str(index))
                raise ValueError("Cutshort questionnaire widget did not load")
        scopes = [widget for widget in await widgets.all() if await widget.is_visible()]
        handled = bool(scopes)
        issue = False
        captured = False
        for scope in scopes or [pane]:
            receipt = scope.locator(
                "form :is(:text-is('Questionnaire submitted!'), :text-is('Your response has been recorded')):visible"
            )
            if scopes and await receipt.count():
                self.questionnaire_stats["already_submitted"] += 1
                continue
            if scopes and not captured:
                await self.artifacts.dump_html(self.page, "questionnaire-before", "Cutshort", str(index))
                captured = True
            # A conversation row does not establish which résumé a file question asks for.
            chatbot = CutshortChatbot(scope, self.answers, max_questions=1, resume_path=None)
            success, answered, error_msg = await chatbot.run()
            if chatbot.submitted:
                self.questionnaire_stats["submitted"] += 1
                log.info("cutshort.messages.success", thread=key, answered=answered)
            elif chatbot.already_submitted:
                self.questionnaire_stats["already_submitted"] += 1
            elif not chatbot.unanswered and (not success or scopes):
                issue = True
                log.warning("cutshort.messages.unconfirmed", thread=key, reason=error_msg or "questionnaire form missing")
            for question in chatbot.unanswered:
                if question not in unanswered:
                    unanswered.append(question)
            handled |= chatbot.submitted or chatbot.already_submitted or bool(chatbot.unanswered)
        if issue:
            self.questionnaire_stats["unconfirmed"] += 1
        if captured or issue:
            await self.artifacts.dump_html(self.page, "questionnaire-after", "Cutshort", str(index))
        return handled
