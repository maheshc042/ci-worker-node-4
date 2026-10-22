"""
Screening-question answer resolution.

Naukri's post-apply chatbot asks free-form recruiter questions. Answering these 
correctly is the difference between a real application and a wasted one.

Design decisions
----------------
- **User Config Wins First:** The KB is evaluated before generic fallbacks. If the user
  says "No" to a question in config.yaml, it strictly overrides the affirmative fallback.
- **Three matching stages, best-match wins:** regex > substring > token set.
- **Strict mode (default):** If nothing matches confidently we return `None` and queue for human review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher

from ..core.models import ScreeningQuestion
from ..logging_setup import get_logger

log = get_logger(__name__)

YES_TOKENS = ("yes", "yeah", "yep", "sure", "agree", "willing", "true", "available", "ok", "okay", "comfortable", "works", "acceptable")
NO_TOKENS = ("no", "nope", "never", "false")

STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "with", "your", "you", "have", "has", "do", "does", "did", "are", "is", "was", "were", "be", "been", "being", "how", "many", "much", "i", "me", "my", "we", "our", "it", "its", "that", "this", "these", "those", "please", "kindly", "can", "could", "would", "will", "shall", "any", "some", "what", "which", "whom", "whose", "if", "then", "else", "about", "from", "into", "over", "under", "per", "as", "by"]
)

_EXPERIENCE_INTENT = re.compile(
    r"(how\s+(?:many|much)\s+(?:years?|yrs?|months?))"
    r"|(?:total|overall|relevant|work|professional|hands[\s-]?on)\s+experience"
    r"|(?:years?|yrs?)\s+of\s+(?:experience|exp)"
    r"|experience\s+(?:do\s+you\s+have|in|with|on|using|of|\w+ing\b)"
    r"|(?:years?|yrs?)\s+(?:in|with|of)\s+\w+"
    r"|\bexp\s+in\b"
    # Recruiter reframings (sweep 2026-09-30): tenure asked without the
    # word "experience" — still tenure when a skill is named.
    r"|how\s+long\s+(?:have\s+you\s+)?(?:worked|been\s+working)\s+(?:with|on|in|as)"
    r"|number\s+of\s+(?:years?|yrs?)"
    r"|experienc(?:e|ed)\s+(?:in|with|on)",
    re.IGNORECASE,
)

# Fallback answers (default/unlisted/total years) are tenure claims: only
# allowed when the question carries the experience NOUN. Without one, an
# unknown skill ("3 years with Cobol?", "experienced in Cobol?") must fall
# through to review — never inherit a default. Noun-boundary on purpose:
# the adjective "experienced" must not open the fallback door.
_EXP_WORD = re.compile(r"\b(?:experience|knowledge|expertise|tenure|total|overall)\b", re.IGNORECASE)

_TOTAL_EXPERIENCE_INTENT = re.compile(
    r"\b(?:total|overall|cumulative|aggregate)\s+(?:work\s+|professional\s+)?"
    r"(?:experience|exp)\b",
    re.IGNORECASE,
)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")

_LWD_INTENT = re.compile(
    r"\b(?:last\s+working\s+(?:day|date)|lwd|relieving\s+date|end\s+date\s+of\s+(?:notice|employment))\b",
    re.IGNORECASE,
)

_AVAILABILITY_TIMING_INTENT = re.compile(
    r"\b(?:"
    r"(?:l1|l2|bot|interview|technical|discussion|meeting|call)\s+(?:availability|available)\s+(?:date|timing|time|slot)s?"
    r"|(?:l1|l2|technical|discussion|meeting|call|interview)\s+(?:slot|date|timing|time)s?"
    r"|(?:availability|available)\s+(?:for\s+)?(?:l1|l2|interview|discussion|call|meeting)"
    r"|(?:preferred|convenient|select|choose)\s+(?:a\s+)?(?:date|time|timing|slot)s?\s+(?:for\s+)?(?:interview|discussion)?"
    r"|date\s+and\s+time\s+(?:for\s+)?(?:interview|round)"
    r")\b",
    re.IGNORECASE,
)

_WILLINGNESS_INTENT = re.compile(
    r"\b(?:"
    r"willing|open\s+to|ready\s+to|comfortable|agree|able\s+to|attend|available\s+for|"
    r"would\s+you|can\s+you|are\s+you\s+willing|are\s+you\s+open|are\s+you\s+ready|are\s+you\s+able|"
    r"are\s+you\s+fine|are\s+you\s+ok(?:ay)?|do\s+you\s+agree|will\s+you|"
    r"does\s+this\s+work(?:\s+for\s+you)?|works?\s+for\s+you|"
    r"face[\s-]to[\s-]face|f2f|in[\s-]person|offline\s+interview|"
    r"rounds?\s+of\s+(?:technical\s+)?interviews?|technical\s+interviews?|interview\s+rounds?|"
    r"interview\s+process|selection\s+process|hiring\s+process|"
    r"relocate|relocation|"
    r"work\s+from\s+office|wfo|onsite|on[\s-]site|"
    r"hybrid\s+(?:work|model|mode|setup|policy)|"
    r"join\s+immediately|immediate\s+joiner|"
    r"take\s+up\s+an?\s+interview|available\s+to\s+(?:take\s+up|attend|give)\s+(?:an?\s+)?interview|"
    r"available\s+within\s+\d+\s*days?|join\s+within\s+\d+\s*days?|notice\s+period|"
    r"night\s*shifts?|rotational\s*shifts?|day\s*shifts?|shifts?|24\/7|rotational|weekend|weekends|"
    r"bond|service\s*agreement|contract|undertaking|policy|terms?|"
    r"travel|flexible|flexibility|"
    r"location\s+of\s+this\s+job|job\s+location|work\s+location|preferred\s+location"
    r")\b",
    re.IGNORECASE,
)

_NEGATIVE_QUESTIONS = re.compile(
    r"\b(?:need\s+special\s+accommodation|criminal|convicted|disciplinary|backlog|disability|handicapped)\b",
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    return " ".join((text or "").lower().split())


# Contraction mismatches ("I'm" vs "I am") silently break option fitting:
# exact and containment checks fail while polarity stays blind. Expanded
# only inside option fitting (blast radius: one function).
_CONTRACTIONS = {
    "i'm": "i am", "i've": "i have", "i'll": "i will", "i'd": "i would",
    "can't": "cannot", "cant": "cannot", "won't": "will not",
    "don't": "do not", "dont": "do not", "doesn't": "does not",
    "isn't": "is not", "aren't": "are not", "wasn't": "was not",
    "weren't": "were not", "haven't": "have not", "hasn't": "has not",
    "couldn't": "could not", "shouldn't": "should not",
    "wouldn't": "would not", "it's": "it is", "that's": "that is",
}


def _expand_contractions(text: str) -> str:
    """Expand English contractions so equivalent phrasings compare equal."""
    words = (text or "").split()
    return " ".join(_CONTRACTIONS.get(w, w) for w in words)


def _tokenise(text: str) -> frozenset[str]:
    normalized_text = (text or "").lower().replace("-", " ")
    words = re.findall(r"[a-z0-9+#.]+", normalized_text)
    cleaned = set()
    for word in words:
        w = word.strip(".")
        if w and w not in STOPWORDS:
            cleaned.add(w)
            if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
                cleaned.add(w[:-1])
    return frozenset(cleaned)


def _contains_word(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


_REMOTE_PLACE_MARKERS = (
    "remote", "work from home", "work-from-home", "wfh", "hybrid", "anywhere",
)

# Major Indian job hubs. Word-boundary matched (Noida/no lesson). Geography
# only — the candidate's own city comes from config, never this table.
_NON_HOME_CITIES = (
    "delhi", "new delhi", "pune", "mumbai", "bombay", "hyderabad",
    "chennai", "kolkata", "noida", "gurgaon", "gurugram", "ahmedabad",
    "kochi", "cochin", "jaipur", "indore", "nagpur", "surat", "lucknow",
    "chandigarh", "coimbatore", "bhubaneswar", "mysuru", "mysore",
    "bengaluru", "bangalore",
)


def _home_markers(home: str) -> tuple[str, ...]:
    """City tokens that make a location question read as home.

    Derived from the configured location (e.g. "Bengaluru, India" ->
    "bengaluru"), plus remote/hybrid phrasings that are true from anywhere.
    """
    markers: list[str] = []
    low = (home or "").strip().lower()
    if low:
        city = low.split(",")[0].strip()
        if city:
            markers.append(city)
            # Bengaluru/Bangalore spellings are used interchangeably.
            if city == "bengaluru":
                markers.append("bangalore")
            elif city == "bangalore":
                markers.append("bengaluru")
    markers.extend(_REMOTE_PLACE_MARKERS)
    return tuple(dict.fromkeys(m for m in markers if m))


def _job_place_is_home(text: str, home: str = "") -> bool:
    """True when a location question names home/remote — or names no place
    at all (fail-open: unknown never rejects).

    Only False on an explicitly named elsewhere (Delhi, Pune, ...), which
    is exactly when "I am currently in this location" would be a lie.
    Covers Cutshort phrasing ("location of this job will be X") plus any
    phrasing naming a known non-home hub ("Are you currently in Pune?").
    A question naming home/remote anywhere reads as home. Expects
    normalised (lowercased) text. Pure.
    """
    markers = _home_markers(home)
    m = re.search(
        r"location of this job will be ([a-z0-9][a-z0-9\s().,&\-]{0,60})", text or ""
    )
    if m and m.group(1):
        return any(h in m.group(1) for h in markers)
    low = text or ""
    if any(h in low for h in markers):
        return True
    return not any(re.search(rf"\b{re.escape(city)}\b", low) for city in _NON_HOME_CITIES)


def _format_years(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


@dataclass(slots=True)
class ResolvedAnswer:
    value: str
    matched_pattern: str
    source: str
    confidence: float = 1.0


@dataclass(slots=True)
class ExperienceAnswers:
    total_years: float | None = None
    default_years: float | None = None
    multi_skill_strategy: str = "max"
    skills: dict[str, float] | None = None
    # Hybrid skill-absence policy (run 604 fix, user-approved): a screening
    # question names a skill the resume map does NOT cover. Judge it by
    # adjacency to the claimed core stack instead of a blanket No:
    #   - `unclaimed_yes`: tokens that are adjacent to the claimed stack
    #     (tooling/frames around tech the resume does claim) -> "Yes".
    #   - `unclaimed_no`: tokens of clearly unrelated specialisations the
    #     recruiter would cross-check against the resume -> "No".
    # Anything matching neither list defaults to "No" (safe).
    unclaimed_yes: list[str] = field(default_factory=list)
    unclaimed_no: list[str] = field(default_factory=list)

    def merged_with(self, override: ExperienceAnswers | None) -> ExperienceAnswers:
        if override is None:
            return self
        skills = dict(self.skills or {})
        skills.update(override.skills or {})
        return ExperienceAnswers(
            total_years=override.total_years if override.total_years is not None else self.total_years,
            default_years=(override.default_years if override.default_years is not None else self.default_years),
            multi_skill_strategy=override.multi_skill_strategy or self.multi_skill_strategy,
            skills=skills,
            unclaimed_yes=list(self.unclaimed_yes) + list(override.unclaimed_yes),
            unclaimed_no=list(self.unclaimed_no) + list(override.unclaimed_no),
        )

    def unclaimed_verdict(self, skill_fragment: str) -> str | None:
        """Classification of an unclaimed skill: "yes", "no", or None (unknown).

        Matching is word-boundary aware on the normalised fragment, plus the
        reverse containment (claimed-style multiword lists like "web
        development" should also hit a fragment named "web development
        services").
        """
        low = _normalise(skill_fragment)
        if not low:
            return None
        for needle in self.unclaimed_yes:
            phrase = _normalise(needle)
            if phrase and (_contains_word(low, phrase) or _contains_word(phrase, low)):
                return "yes"
        for needle in self.unclaimed_no:
            phrase = _normalise(needle)
            if phrase and (_contains_word(low, phrase) or _contains_word(phrase, low)):
                return "no"
        return None


@dataclass(slots=True)
class _Entry:
    pattern: str
    answer: str
    source: str
    tokens: frozenset[str]
    regex: re.Pattern[str] | None
    index: int


class AnswerEngine:
    def __init__(
        self,
        kb: list[tuple[str, str]],
        profile_answers: dict[str, str] | None = None,
        strict: bool = True,
        experience: ExperienceAnswers | None = None,
        home_location: str = "",
    ) -> None:
        raw: list[tuple[str, str, str]] = [
            (_normalise(pattern), answer, "profile")
            for pattern, answer in (profile_answers or {}).items()
        ]
        raw.sort(key=lambda triple: len(triple[0]), reverse=True)
        raw += [(_normalise(pattern), answer, "kb") for pattern, answer in kb]

        # Profile precedence (config.yaml: "Profile-level answers win over
        # these"): drop kb rows shadowed by a profile row — same intent in
        # different words (profile tokens ⊆ kb tokens, plain patterns only).
        # Without this, longer global phrasings outscore corrected profile
        # entries on token count (ML 2-vs-0 stayed wrong for weeks), and no
        # profile correction can ever win. Regex entries never shadow (their
        # match semantics differ from substring/token matching).
        _profile_toks = [
            _tokenise(pattern)
            for pattern, _answer, source in raw
            if source == "profile" and pattern and not pattern.startswith("re:")
        ]
        if any(_profile_toks):
            _kept: list[tuple[str, str, str]] = []
            for pattern, answer, source in raw:
                if (
                    source == "kb"
                    and pattern
                    and not pattern.startswith("re:")
                    and any(pt and pt <= _tokenise(pattern) for pt in _profile_toks)
                ):
                    log.debug("answers.shadowed", pattern=pattern[:80])
                    continue
                _kept.append((pattern, answer, source))
            raw = _kept

        self.entries: list[_Entry] = []
        for index, (pattern, answer, source) in enumerate(raw):
            if not pattern:
                continue
            compiled: re.Pattern[str] | None = None
            if pattern.startswith("re:"):
                try:
                    compiled = re.compile(pattern[3:], re.IGNORECASE)
                except re.error:
                    log.warning("answers.bad_regex", pattern=pattern[:80])
                    continue
            self.entries.append(
                _Entry(
                    pattern=pattern,
                    answer=answer,
                    source=source,
                    tokens=frozenset() if compiled else _tokenise(pattern),
                    regex=compiled,
                    index=index,
                )
            )

        # Polarity-only entries (tokens ⊆ {yes/no/true/false/...}) may only
        # match via substring/regex — never via token-set. Otherwise any
        # question containing both words ("If Yes ... If No ...") resolves
        # to a fabricated "Yes".
        _polarity_vocab = set(YES_TOKENS) | set(NO_TOKENS) | {"true", "false"}
        self._polarity_only: set[int] = {
            e.index for e in self.entries if e.tokens and e.tokens <= _polarity_vocab
        }

        self.strict = strict
        self.experience = experience or ExperienceAnswers()
        # Candidate's own city comes from config (`candidate.location`), so a
        # relocation/current-location answer never asserts a hardcoded place.
        self.home_location = (home_location or "").strip()
        self.home_city = self.home_location.split(",")[0].strip()
        self.home_markers = _home_markers(self.home_location)
        self.hits: dict[tuple[str, str], int] = {}
        self.fuzzy_threshold: float = 0.80

    def resolve(self, question: ScreeningQuestion) -> ResolvedAnswer | None:
        text = _normalise(question.text)
        if not text:
            return None

        # Stage 0: LWD intent remains absolute highest priority
        lwd = self._resolve_lwd(text, question)
        if lwd is not None:
            return lwd

        # Stage 0.5: notice SERVING status ("are you serving your notice?").
        # Must precede Stage 1: the generic "notice period → 0 days" entry
        # substring-matches these questions and its numeric-zero polarity
        # converts to "No" — answering a status question with a duration.
        serving = self._resolve_notice_status(text, question)
        if serving is not None:
            return serving

        # Stage 1: User Configuration & KB! User config beats generic fallbacks.
        entry = self._best_entry(text)
        if entry is not None:
            fitted = self._fit_to_options(entry.answer, question, entry.pattern, entry.source)
            if fitted is not None:
                if entry.regex is not None:
                    fitted.confidence = 1.0
                elif entry.pattern in text:
                    fitted.confidence = 0.9
                else:
                    fitted.confidence = 0.8
                self._record_hit(entry)
                return fitted

        # Stage 1.5: Dynamic Interview Availability Scheduling
        availability = self._resolve_availability(text, question)
        if availability is not None:
            return availability
        # Stage 1.52: Fluency + self-rating intents (recruiter reframings
        # that generic willingness/fuzzy stages either miss or answer
        # wrongly — e.g. typing "Yes" into a 1-10 rating box).
        for intent in (self._resolve_fluent, self._resolve_self_rating):
            hit = intent(text, question)
            if hit is not None:
                return hit

        # Stage 1.6: Generic Affirmative & Location Fallback
        willingness = self._resolve_willingness(text, question)
        if willingness is not None:
            return willingness
        # Stage 1.65: Motivation ("why us / why this role") free text.
        # Mandatory, unskippable, and unanswerable from any table — so the
        # engine composes one from verified facts only (tenure + top mapped
        # skills + company named in the question). Never invents domains,
        # projects, or enthusiasm specifics beyond the template.
        motivation = self._resolve_motivation(text, question)
        if motivation is not None:
            return motivation
        # Stage 1.7: Known-absent skill ("Do you have X experience?" where X
        # is in no skill map -> honest "No"). Runs before the tenure math so
        # unknown skills never inherit default_years.
        absence = self._resolve_skill_absence(text, question)
        if absence is not None:
            return absence
        # Stage 1.8: "Are you experienced in X?" — Yes for mapped skills,
        # review for unknown ones (never affirm what the map can't verify).
        experienced = self._resolve_experienced_yes(text, question)
        if experienced is not None:
            return experienced
        # Stage 1.85: Tenure comparisons ("more than 5 years?"), presence
        # denials ("based in Gurgaon?" asked in Bengaluru), and seniority
        # self-descriptions ("which best describes your experience ...?").
        # None match any KB intent; without them the job dies in review
        # (run 569 #71, #73; run 571 #94).
        for intent in (self._resolve_tenure_comparison, self._resolve_presence_no,
                       self._resolve_self_describe):
            hit = intent(text, question)
            if hit is not None:
                return hit
        # Stage 2: Experience Math
        experience = self._resolve_experience(text, question)
        if experience is not None:
            return experience

        # Stage 3: Programming Language Proficiency Intent
        lang_prof = self._resolve_language_proficiency(text, question)
        if lang_prof is not None:
            return lang_prof

        # Stage 3.2: Compensation Intent (expected/current CTC & salary)
        compensation = self._resolve_compensation(text, question)
        if compensation is not None:
            return compensation

        # Stage 3.3: Education-completion Intent (verified: B.E., VTU 2023)
        education = self._resolve_education(text, question)
        if education is not None:
            return education

        # Stage 3.5 removed: exam scores (percentile/JEE/CGPA) have no user-data
        # source. Earlier code invented "92"/"88"/"95" here — never fabricate
        # credentials. These questions now fall through to fuzzy/None (human review).

        # Stage 4: Fuzzy Math
        fuzzy = self._resolve_fuzzy(text, question)
        if fuzzy is not None:
            return fuzzy

        log.info("answers.unresolved", question=question.text[:160], kind=question.kind, options=len(question.options), options_preview=str(question.options[:8])[:300])
        return None

    def _resolve_language_proficiency(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        low = text.lower()
        if any(k in low for k in ("programming language", "comparable programming language", "proficiency in python", "proficiency in typescript")):
            ans = "Proficient in Python and TypeScript, with extensive hands-on experience building production APIs and AI applications."
            log.info("answers.language_proficiency_intent", question=question.text[:100])
            return self._fit_to_options(ans, question, "intent:programming_language", "intent-map")
        return None

    def _resolve_lwd(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        if not _LWD_INTENT.search(text):
            return None
        for entry in self.entries:
            if "last working" in entry.pattern or "lwd" in entry.pattern:
                log.info(
                    "answers.lwd_intent",
                    question=question.text[:100],
                    source=entry.source,
                )
                return self._fit_to_options(
                    entry.answer,
                    question,
                    entry.pattern,
                    entry.source,
                )
        return None

    def _resolve_availability(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        if not _AVAILABILITY_TIMING_INTENT.search(text):
            return None
        log.info("answers.availability_intent", question=question.text[:100])
        # If options are given, choose the first reasonable weekday/business hour slot
        if question.options:
            for opt in question.options:
                opt_low = opt.lower()
                if any(w in opt_low for w in ("weekday", "anytime", "immediate", "morning", "afternoon", "10", "11", "2", "3", "4", "5", "6")):
                    return self._fit_to_options(opt, question, "intent:availability", "intent-map")
            # No suitable slot offered: fail closed instead of picking blindly.
            return None
        return ResolvedAnswer(
            "Available on weekdays between 10:00 AM to 6:00 PM IST",
            "intent:availability",
            "intent-map",
        )

    def _resolve_willingness(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        if not _WILLINGNESS_INTENT.search(text) or _NEGATIVE_QUESTIONS.search(text):
            return None

        # Institution-identity questions ("What university did you attend?")
        # match the intent via "attend" but must never get a bare "Yes".
        if re.search(r"\buniversit\w*|\bcollege\b|\bschool\b|\binstitute\b", text) and not re.search(
            r"\b(complet\w*|earn\w*|graduat\w*|hold\w*|degree|bachelor|master)\b", text
        ):
            return None

        # Location-aware and multi-choice willingness handling.
        # Presence-claim quarantine (run 539): for a known-foreign place,
        # "I am currently in this location" asserts a falsehood no matter
        # which matcher below reaches it — substring, polarity, fallback,
        # or the final Yes-fit. Quarantine once up front so the question
        # falls through to review instead of a lie. Willingness is not
        # presence; a false here-claim is misrepresentation.
        _opts = list(question.options or [])
        if _opts and not _job_place_is_home(text, self.home_location):
            _kept = [o for o in _opts if "currently in this location" not in o.lower()]
            if len(_kept) != len(_opts):
                log.info("answers.here_claim_quarantined", question=question.text[:100],
                         dropped=len(_opts) - len(_kept))
                _opts = _kept
                if not _opts:
                    # Nothing truthful left to pick: a bare "Yes" fit below
                    # would still satisfy the run. Route to review instead.
                    log.info("answers.here_claim_quarantine_emptied",
                             question=question.text[:100])
                    return None
        if _opts:
            # 1. Check for preferred locations (home city, Remote, Hybrid)
            for opt in _opts:
                if self._polarity(_normalise(opt)) is False:
                    continue
                opt_low = opt.strip().lower()
                if any(pref in opt_low for pref in self.home_markers):
                    return ResolvedAnswer(opt, "intent:willingness_location_preferred", "intent-map")

            # 2. Check for Cutshort-style affirmative presence / relocation options.
            # Place-aware (run 480, hardened run 539): "I am currently in
            # this location" is only true for home (config location) or
            # remote — foreign instances are quarantined above, so anything
            # reaching here is answerable. Explicit relocate options still win.
            is_home = _job_place_is_home(text, self.home_location)
            opt_lows = [(opt, opt.strip().lower()) for opt in _opts]
            if not is_home:
                # Relocate-intent carve-out (run 544 / review #57/#58):
                # "I am not in this location but can relocate" is the
                # truthful Yes for a relocate-willing candidate — its
                # "not" negates presence, not willingness, so the
                # polarity/negation gates below must not screen it out.
                # Genuine relocation refusals still exclude.
                for opt, opt_low in opt_lows:
                    if ("can relocate" in opt_low or "willing to relocate" in opt_low) \
                            and not (_tokenise(opt_low) & {"unwilling", "cannot", "cant", "unable"}) \
                            and "can't" not in opt_low \
                            and "not willing" not in opt_low \
                            and "not able" not in opt_low:
                        return ResolvedAnswer(opt, "intent:willingness_can_relocate", "intent-map")
                for opt, opt_low in opt_lows:
                    if self._polarity(_normalise(opt)) is False:
                        continue
                    if any(neg in opt_low for neg in ("not", "unwilling", "cannot", "none", "neither", "no")):
                        continue
                    if "can relocate" in opt_low or "willing to relocate" in opt_low:
                        return ResolvedAnswer(opt, "intent:willingness_can_relocate", "intent-map")
            for opt, opt_low in opt_lows:
                if self._polarity(_normalise(opt)) is False:
                    continue
                if any(neg in opt_low for neg in ("not", "unwilling", "cannot", "none", "neither", "no")):
                    continue
                if "currently in this location" in opt_low or "okay with it" in opt_low:
                    return ResolvedAnswer(opt, "intent:willingness_current_location", "intent-map")
                if "can relocate" in opt_low or "willing to relocate" in opt_low:
                    return ResolvedAnswer(opt, "intent:willingness_can_relocate", "intent-map")

            # 3. Check for explicit affirmative tokens (Yes, Willing, Agree, Acceptable)
            for opt in _opts:
                if self._polarity(_normalise(opt)) is True:
                    return ResolvedAnswer(opt, "intent:willingness_affirmative", "intent-map")

            # 4. Fallback for relocation city options (e.g. Mumbai, Navi Mumbai, Pune, Hyderabad):
            # Candidate is willing to relocate almost anywhere in India in this
            # job market. Select the first option that is not explicitly
            # negative. Negation is token-matched (not substring): the old
            # substring guard skipped cities like "Noida" via the "no" in it.
            _reloc_neg = {
                "not", "unwilling", "cannot", "can't", "cant", "none",
                "neither", "no", "never", "nope", "false",
            }
            for opt in _opts:
                if self._polarity(_normalise(opt)) is False:
                    continue
                if _tokenise(opt) & _reloc_neg:
                    continue
                log.info("answers.willingness_location_fallback", selected=opt, question=question.text[:70])
                return ResolvedAnswer(opt, "intent:willingness_location_fallback", "intent-map")

            # No relocation city is ever invented: without an explicit match the
            # question routes to human review via the strict gate below.

        log.info("answers.willingness_intent", question=question.text[:100])
        return self._fit_to_options(
            "Yes", replace(question, options=_opts) if _opts != list(question.options or []) else question,
            "intent:willingness", "intent-map")

    def _best_entry(self, text: str) -> _Entry | None:
        question_tokens = _tokenise(text)
        best: _Entry | None = None
        best_score: tuple[int, int, int, int] | None = None
        # Tenure questions ("how many years ...?") must only be answered by
        # tenure entries (numeric answer or tenure words in the pattern).
        # Otherwise a skill-list entry ("programming language" ->
        # "Python, TypeScript, ...") hijacks the years box via token overlap
        # (run 569 #66: years-with-Python answered with a language list).
        tenure_q = bool(_EXPERIENCE_INTENT.search(text))
        _non_tenure_pat = re.compile(
            r"experienc|years?|yrs?|knowledge|expertise|tenure", re.IGNORECASE
        )

        for entry in self.entries:
            if entry.regex is not None:
                if not entry.regex.search(text):
                    continue
                stage, matched = 3, 3
            elif entry.pattern in text:
                # Short patterns ("nit", "iit", "dtu", "dob", "lwd") must be
                # word-matched: plain substring lets "nit" fire inside
                # "opportunity" and answer a compensation question with "No"
                # (run 569 #79: "<30 lakhs?" answered No via NIT).
                if len(entry.pattern) <= 4 and not _contains_word(text, entry.pattern):
                    continue
                if tenure_q and not _non_tenure_pat.search(entry.pattern):
                    if not _NUMBER.search(entry.answer or ""):
                        continue
                stage, matched = 2, len(entry.tokens)
            elif (
                entry.tokens
                and entry.index not in self._polarity_only
                and entry.tokens <= question_tokens
            ):
                if tenure_q and not _non_tenure_pat.search(entry.pattern):
                    if not _NUMBER.search(entry.answer or ""):
                        continue
                stage, matched = 1, len(entry.tokens)
            else:
                continue

            score = (stage, matched, len(entry.pattern), -entry.index)
            if best_score is None or score > best_score:
                best, best_score = entry, score

        return best

    def _record_hit(self, entry: _Entry) -> None:
        key = (entry.pattern, entry.source)
        self.hits[key] = self.hits.get(key, 0) + 1

    def consume_hits(self) -> dict[tuple[str, str], int]:
        hits, self.hits = self.hits, {}
        return hits

    def _resolve_fuzzy(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        best_ratio = 0.0
        best_entry: _Entry | None = None

        for entry in self.entries:
            if entry.regex is not None:
                continue
            ratio = SequenceMatcher(None, text, entry.pattern, autojunk=False).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_entry = entry

        if best_entry is None or best_ratio < self.fuzzy_threshold:
            return None

        # Guard: the fuzzy winner must share at least one distinctive token
        # with the question. Otherwise a wrong-skill pattern (".net" for a
        # DevOps question at 0.88) dictates the answer.
        _generic_tokens = {
            "experience", "experiences", "year", "years", "yr", "yrs",
            "many", "much", "work", "working", "hands",
        }
        if not (set(best_entry.tokens) & _tokenise(text) - _generic_tokens):
            log.info(
                "answers.fuzzy_rejected_no_skill_overlap",
                question=question.text[:120],
                matched_pattern=best_entry.pattern[:80],
                ratio=round(best_ratio, 3),
            )
            return None

        log.info("answers.fuzzy_match", question=question.text[:120], matched_pattern=best_entry.pattern[:80], ratio=round(best_ratio, 3), answer=best_entry.answer[:40])
        fitted = self._fit_to_options(best_entry.answer, question, best_entry.pattern, "fuzzy")
        if fitted is not None:
            fitted.confidence = round(best_ratio, 3)
            self._record_hit(best_entry)
        return fitted

    def _resolve_notice_status(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """Serving-notice STATUS (yes/no), distinct from notice duration.

        Reads the explicit serving entry ("serving notice" → Yes/No), never
        the duration entry. No serving entry configured → review, never derive.
        """
        if not re.search(r"\bserving\b|\bunder\s+notice\b", text):
            return None
        for entry in self.entries:
            if "serving" in entry.pattern:
                fitted = self._fit_to_options(entry.answer, question, entry.pattern, entry.source)
                if fitted is not None:
                    log.info("answers.notice_status", question=question.text[:120], value=entry.answer[:20])
                    return fitted
        return None

    def _resolve_fluent(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """"Are you fluent in X?" — Yes for mapped skills with real tenure,
        No for mapped-zero ones, review for anything unlisted."""
        m = re.search(r"\bfluen[tcy]*\s+(?:in|with)\s+(.+?)\s*\??$", text)
        if not m:
            return None
        frag = (m.group(1) or "").strip()
        if not frag or len(frag) > 40:
            return None
        for skill, years in (self.experience.skills or {}).items():
            phrase = _normalise(skill)
            if phrase and (_contains_word(frag, phrase) or _contains_word(text, phrase)):
                try:
                    want = "Yes" if float(years) > 0 else "No"
                except (TypeError, ValueError):
                    return None
                fitted = self._fit_to_options(want, question, f"fluent:{phrase}", "skill-map")
                if fitted is not None:
                    log.info("answers.fluent", question=question.text[:120], skill=phrase[:40], value=want)
                return fitted
        return None

    def _resolve_self_rating(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """"Rate yourself 1-10 in X" — 8 for mapped skills with real tenure
        (strong-but-credible house rule, same as fit-rating), review for
        unlisted skills. Never lets willingness type "Yes" into a rating box.
        """
        if not re.search(r"\brate\b", text):
            return None
        if not re.search(r"\b(?:yourself|your)\b", text):
            return None
        if not re.search(r"1\s*(?:-|to|/)\s*10|scale\s+of\s+1\s*-\s*10|out\s+of\s+10", text):
            return None
        for skill, years in (self.experience.skills or {}).items():
            phrase = _normalise(skill)
            if not phrase:
                continue
            if _contains_word(text, phrase):
                try:
                    if float(years) <= 0:
                        return None
                except (TypeError, ValueError):
                    return None
                fitted = self._fit_to_options("8", question, f"self-rating:{phrase}", "skill-map")
                if fitted is not None:
                    log.info("answers.self_rating", question=question.text[:120], skill=phrase[:40])
                return fitted
        return None

    def _resolve_motivation(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        # Job-change questions have their own configured answers — never
        # let the motivation template speak for why the candidate is leaving.
        if re.search(
            r"look(?:ing)?\s+for\s+(?:a\s+)?change|\bleav|\bresign|\bquit\b|"
            r"notice\s+period|reliev|why\s+did\s+you\s+leave|reason\s+for\s+(?:leaving|change)",
            text,
        ):
            return None
        if not re.search(
            r"why\s+(?:do\s+you\s+want\s+(?:to\s+(?:work|join|apply)|this|the)|"
            r"are\s+you\s+interested|should\s+we\s+hire|"
            r"do\s+you\s+want\s+to\s+(?:work|join)|are\s+you\s+a\s+good\s+fit)|"
            r"what\s+interests\s+you|what\s+(?:excites|attracts|motivates)\s+you|"
            r"why\s+(?:this|the)\s+(?:role|position|job|opportunity|company)|"
            r"why\s+us\b|cover\s*letter",
            text,
        ):
            return None
        # Company as named in the question ("... at Vena Solutions?").
        company = ""
        m = re.search(
            r"\bat\s+([A-Z][\w&.,'\-]*(?:\s+[A-Z][\w&.,'\-]*){0,4})",
            question.text or "",
        )
        if m:
            company = m.group(1).strip().strip(".,?")
        if company and len(company) > 40:
            company = ""
        # Top verified skills by tenure — facts only, no invented domains.
        ranked = sorted(
            ((s, float(y)) for s, y in (self.experience.skills or {}).items()),
            key=lambda pair: (-pair[1], pair[0]),
        )
        top = [s for s, y in ranked if y > 0][:4]
        total = None
        try:
            if self.experience.total_years is not None:
                total = _format_years(float(self.experience.total_years))
        except (TypeError, ValueError):
            total = None
        if not top or total is None:
            return None
        at = f" at {company}" if company else ""
        skills = ", ".join(top)
        value = (
            f"I'm excited about this opportunity{at} because it aligns with "
            f"my experience: {total} years building software with {skills}. "
            f"I enjoy solving hard product problems end-to-end and would "
            f"love to bring that to your team."
        )
        fitted = self._fit_to_options(value, question, "intent:motivation", "intent-map")
        if fitted is not None:
            log.info("answers.motivation", question=question.text[:120], company=company[:40])
        return fitted

    def _resolve_experienced_yes(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        # Yes/No skill questions across recruiter framings: "experienced in",
        # "have you built/deployed X", "have you worked with X". Known skill
        # with tenure -> Yes; known-zero skill -> No; unknown -> review.
        # (Run 504: two LangGraph/RAG Yes/No questions went unanswered and the
        # submit still succeeded — the answers must come from the map.)
        # Quantity interrogatives ("how long/many", "number of years") belong
        # to the tenure-number stage below, never to a Yes/No answer.
        if re.search(r"\bhow\s+(many|much|long)\b|\bnumber\s+of\s+", text):
            return None
        m = re.search(
            r"\bexperienc(?:ed|e)\s+(?:in|with|on)\s+(.+?)\s*\??$",
            text,
        )
        if not m and not re.search(
            r"\bhave\s+you\s+(?:built|deployed|developed|implemented|shipped|"
            r"worked\s+(?:with|on)|used|been\s+working\s+(?:with|on))\b",
            text,
        ):
            return None
        frag = ((m.group(1) if m else text) or "").strip()
        if not frag or len(frag) > 120:
            return None
        saw_zero = False
        for skill, years in (self.experience.skills or {}).items():
            phrase = _normalise(skill)
            if not phrase:
                continue
            if _contains_word(frag, phrase) or _contains_word(text, phrase):
                try:
                    skill_years = float(years)
                except (TypeError, ValueError):
                    return None
                if skill_years <= 0:
                    saw_zero = True
                    continue
                fitted = self._fit_to_options("Yes", question, f"experienced:{phrase}", "skill-map")
                if fitted is not None:
                    log.info("answers.experienced_yes", question=question.text[:120], skill=phrase[:40])
                return fitted
        if saw_zero:
            fitted = self._fit_to_options("No", question, "experienced:zero-skill", "skill-map")
            if fitted is not None:
                log.info("answers.experienced_no", question=question.text[:120])
            return fitted
        return None

    def _resolve_tenure_comparison(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """"Do you have more than 5 years of exp?" — compare the threshold
        against verified total tenure instead of typing a number into a
        Yes/No-shaped question (run 569 #71 went to review: "exp" carries
        no tenure noun so the experience stages never fire)."""
        m = re.search(
            r"\b(?:more\s+than|over|above|greater\s+than|at\s+least|minimum(?:\s+of)?|"
            r"atleast|5\s*\+|(\d+)\s*\+\s*(?:years?|yrs?|exp))\s*"
            r"(\d+(?:\.\d+)?)?\s*(?:years?|yrs?|\by\b|\bexp\b)?",
            text,
        )
        if not m:
            return None
        if not re.search(r"\b(?:years?|yrs?|\bexp\b|experience)\b", text):
            return None
        num = m.group(2) or m.group(1)
        # "5+ years" form carries the number outside group 2.
        if num is None:
            m2 = re.search(r"(\d+(?:\.\d+)?)\s*\+", text)
            num = m2.group(1) if m2 else None
        if num is None:
            return None
        try:
            threshold = float(num)
        except (TypeError, ValueError):
            return None
        try:
            total = float(self.experience.total_years) if self.experience.total_years is not None else None
        except (TypeError, ValueError):
            total = None
        if total is None:
            return None
        want = "Yes" if total >= threshold else "No"
        fitted = self._fit_to_options(want, question, f"tenure-compare:{threshold:g}", "tenure-map")
        if fitted is not None:
            log.info("answers.tenure_comparison", question=question.text[:120],
                     threshold=threshold, total=total, value=want)
        return fitted

    def _resolve_self_describe(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """"Which best describes your experience ...?" seniority gradients
        (run 571 #94). The named area must be a mapped skill with real
        tenure; then pick the strongest option that does NOT claim
        lead-level work (guide architecture, review others, mentor, manage,
        lead a team). Never understates to junior ("need guidance") when
        tenure supports independent delivery, never overstates to lead.
        Unknown/zero skills and lead-only option sets stay in review."""
        if not re.search(
            r"\bwhich\b.{0,20}\bbest\b.{0,20}\bdescrib|\bbest\b.{0,20}\bdescrib|\bselect\b.{0,30}\bstatement\b",
            text,
        ):
            return None
        opts = list(question.options or [])
        if len(opts) < 2:
            return None
        area = re.sub(r"^.*describes?\s+(?:your\s+)?", "", text).strip(" ?")
        if not area or len(area) > 80:
            return None
        known_years: float | None = None
        for skill, years in (self.experience.skills or {}).items():
            phrase = _normalise(skill)
            if phrase and _contains_word(area, phrase):
                try:
                    known_years = float(years)
                except (TypeError, ValueError):
                    return None
                break
        if known_years is None or known_years <= 0:
            return None
        lead_marks = (
            "guide architecture", "review other", "mentor", "manage",
            "lead the", "lead a", "leadership",
        )
        indep_marks = (
            "independently", "own", "plan", "build", "test", "deploy",
            "end-to-end", "end to end", "deliver",
        )
        best: str | None = None
        best_score = -1
        for opt in opts:
            low = opt.lower()
            if any(m in low for m in lead_marks):
                continue
            score = sum(1 for m in indep_marks if m in low)
            if score > best_score:
                best, best_score = opt, score
        if best is None:
            log.info("answers.self_describe_lead_only", question=question.text[:120])
            return None
        fitted = self._fit_to_options(best, question, "intent:self_describe", "intent-map")
        if fitted is not None:
            log.info("answers.self_describe", question=question.text[:120],
                     value=best[:60])
        return fitted

    def _resolve_presence_no(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """"Are you currently based in Gurgaon?" asked from the home city.
        The home-city-scoped KB keys must never answer Yes for any city
        (anti-lie rule), but "No" is the truthful Yes/No answer for a
        known-foreign hub — routing it to review burns the application
        (run 569 #73). Free-text variants ("which city...?") still get the
        configured home city, never a bare No."""
        if not re.search(
            r"\bbased\s+in\b|\bcurren\w*\s+(?:based|located|residing|living)\b|"
            r"\bresiding\s+in\b|\blocated\s+in\b|\blive\s+in\b|\bstaying\s+in\b|"
            r"\bcurren\w*\s+in\b",
            text,
        ):
            return None
        if _job_place_is_home(text, self.home_location):
            return None
        opts = list(question.options or [])
        if opts:
            has_yes = any(self._polarity(_normalise(o)) is True for o in opts)
            has_no = any(self._polarity(_normalise(o)) is False for o in opts)
            if has_yes or has_no:
                fitted = self._fit_to_options("No", question, "intent:presence_foreign_no", "intent-map")
                if fitted is not None:
                    log.info("answers.presence_foreign_no", question=question.text[:120])
                return fitted
            return None
        if not self.home_city:
            return None
        fitted = self._fit_to_options(self.home_city, question, "intent:presence_foreign_city", "intent-map")
        if fitted is not None:
            log.info("answers.presence_foreign_city", question=question.text[:120])
        return fitted

    def _resolve_experience(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        config = self.experience
        if not _EXPERIENCE_INTENT.search(text):
            return None

        if _TOTAL_EXPERIENCE_INTENT.search(text) and config.total_years is not None:
            return self._fit_to_options(_format_years(config.total_years), question, "experience:total", "experience-map")

        matched: list[tuple[str, float]] = []
        for skill, years in (config.skills or {}).items():
            phrase = _normalise(skill)
            if phrase and _contains_word(text, phrase):
                matched.append((phrase, float(years)))

        if matched:
            matched.sort(key=lambda pair: len(pair[0]), reverse=True)
            longest = matched[0][0]
            values = [years for phrase, years in matched if phrase == longest or phrase not in longest]
            strategy = (config.multi_skill_strategy or "max").lower()
            
            if strategy == "min": value = min(values)
            elif strategy == "avg": value = round(sum(values) / len(values), 1)
            else: value = max(values)

            if config.total_years is not None:
                value = min(value, config.total_years)

            log.info("answers.experience_map", skills=[phrase for phrase, _ in matched][:6], strategy=strategy, value=value)
            return self._fit_to_options(_format_years(value), question, f"experience:{longest}", "experience-map")

        # Named-ROLE tenure ("... as a Site Reliability Engineer?") with no
        # map hit: recruiters verify titles against the resume, so a default
        # here is fabrication, not generosity. Review — while "in <skill>"
        # keeps the unlisted-1y default below. Skill hits already returned.
        if re.search(
            r"\bas\s+(?:an?\s+|the\s+)?[a-z][a-z0-9\s/&+#.\-]{1,40}?"
            r"(?:developer|engineer|analyst|architect|scientist|manager|"
            r"lead|consultant|specialist|tester|administrator|designer|"
            r"devops|sre|qa)s?\s*\??$",
            text,
        ):
            log.info("answers.role_tenure_review", question=question.text[:120])
            return None

        # For unlisted skills on matched jobs, default to 1 year so automated
        # ATS screening doesn't drop an otherwise-strong candidate.
        m = re.search(
            r"(?:experience|knowledge|expertise)\s+(?:in|with|of)\s+([a-z][a-z0-9+#.\- ]{0,30})\s*\??$",
            text,
        ) or re.search(
            r"(?:in|with|of)\s+([a-z][a-z0-9+#.\- ]{1,30})\s*\??$",
            text,
        )
        if m and _EXP_WORD.search(text):
            maps: set[str] = set()
            for s in (config.skills or {}):
                maps |= _tokenise(s)
            if not (_tokenise(m.group(1)) & maps):
                unlisted_skill = m.group(1).strip()[:30]
                log.info(
                    "answers.unlisted_skill_default_1y",
                    question=question.text[:120],
                    skill=unlisted_skill,
                )
                return self._fit_to_options("1", question, f"experience:unlisted_default:{unlisted_skill}", "experience-map")

        if config.default_years is not None and _EXP_WORD.search(text):
            return self._fit_to_options(_format_years(config.default_years), question, "experience:default", "experience-map")

        if config.total_years is not None and _EXP_WORD.search(text):
            return self._fit_to_options(_format_years(config.total_years), question, "experience:total_fallback", "experience-map")

        # Unknown required facts block the application; never invent experience.
        return None

    def _configured_ctc(self, which: str) -> float | None:
        """First numeric CTC value configured for expected/current."""
        for entry in self.entries:
            if which in entry.pattern and (
                "ctc" in entry.pattern or "salary" in entry.pattern or "pay" in entry.pattern
            ):
                m = _NUMBER.search(entry.answer or "")
                if m:
                    try:
                        return float(m.group())
                    except (TypeError, ValueError):
                        continue
        return None

    def _resolve_compensation(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """Answer salary/CTC questions from the configured expected/current CTC.

        LinkedIn renders these as radio/select ("How much is your expected
        Salary?") where no experience pattern fires; without this stage they
        abort every otherwise-eligible application.
        """
        if not re.search(
            r"\b(?:ctc|salary|salaries|\bpay\b|package|packages|lakh|lakhs|lpa|compensation|stipend|ectc)\b",
            text,
        ):
            return None
        # Budget comparisons ("expected ... less than 30 lakhs?") are
        # Yes/No-shaped: compare the threshold against the configured
        # expected CTC instead of typing "7" into a Yes/No box (run 569
        # #79 also caught a "nit"-in-"opportunity" false positive here).
        m = re.search(
            r"\b(?:less\s+than|under|below|up\s*to|upto|within|max(?:imum)?|budget)\b"
            r"\s*(?:rs\.?|₹|inr)?\s*(\d+(?:\.\d+)?)\s*(?:lakh|lakhs|lpa)?",
            text,
        )
        if m and re.search(r"\bexpect", text):
            try:
                threshold = float(m.group(1))
            except (TypeError, ValueError):
                threshold = None
            exp_val = self._configured_ctc("expected")
            if threshold is not None and exp_val is not None:
                want_bool = "Yes" if exp_val <= threshold else "No"
                fitted = self._fit_to_options(
                    want_bool, question,
                    f"compensation:expected_le_{threshold:g}", "compensation-map",
                )
                if fitted is not None:
                    log.info("answers.compensation_compare",
                             question=question.text[:100],
                             expected=exp_val, threshold=threshold, value=want_bool)
                    return fitted
        norm = text.lower()
        if any(m in norm for m in ("expected", "expectation", "looking for", "desired", "asking", "demand")):
            want = "expected"
        elif any(m in norm for m in ("current", "present", "existing", "drawn", "in hand", "fixed")):
            want = "current"
        else:
            want = "expected"
        for entry in self.entries:
            if want in entry.pattern and (
                "ctc" in entry.pattern or "salary" in entry.pattern or "pay" in entry.pattern
            ):
                fitted = self._fit_to_options(entry.answer, question, entry.pattern, entry.source)
                if fitted is not None:
                    log.info(
                        "answers.compensation",
                        question=question.text[:100],
                        which=want,
                        value=entry.answer[:40],
                    )
                    self._record_hit(entry)
                    return fitted
        return None

    def _resolve_education(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """Degree-completion questions. Verified from the resume: B.E., VTU 2023.

        Fires only when the question names a degree AND completion semantics.
        "What university did you attend?" has no completion verb -> review.
        Master-level degrees (MBA/Masters/M.Tech/PhD) resolve No — the user
        holds a B.E. only, and a fabricated postgraduate credential fails
        document verification worse than never applying. Bachelor-level and
        generic degree questions resolve Yes.
        """
        if not re.search(
            r"\b(bachelor'?s?|master'?s?|mba|b\.?\s*e\b|b\.?\s*tech|m\.?\s*tech|degree|graduat\w+)\b",
            text,
        ):
            return None
        if not re.search(
            r"\b(complet\w*|finish\w*|earn\w*|obtain\w*|hold\w*|passed|have\s+(?:you\s+)?(?:completed|earned|finished))\b",
            text,
        ):
            return None
        if re.search(r"\b(mba|master'?s?|m\.?\s*tech|ph\.?\s*d|doctorate)\b", text) and not re.search(
            r"\b(bachelor'?s?|b\.?\s*e\b|b\.?\s*tech|undergraduate)\b", text
        ):
            fitted = self._fit_to_options("No", question, "intent:education_not_held", "intent-map")
            if fitted is not None:
                log.info("answers.education_not_held", question=question.text[:100])
                return fitted
            return None
        fitted = self._fit_to_options("Yes", question, "intent:education_completed", "intent-map")
        if fitted is not None:
            log.info("answers.education_completed", question=question.text[:100])
            return fitted
        return None

    # Framing words that never name the skill itself. If the captured
    # fragment holds nothing else ("hands-on experience building X"), the
    # whole question is scanned for claimed skills instead.
    _SKILL_FILLER = frozenset({
        "hands", "hand", "on", "relevant", "any", "professional", "working",
        "building", "developing", "using", "similar", "related", "strong",
    })

    def _skill_tenure_in(self, fragment: str) -> tuple[bool, bool]:
        """(has_positive, has_zero) for mapped skills named in ``fragment``."""
        low = _normalise(fragment)
        if not low:
            return False, False
        positive = zero = False
        for skill, years in (self.experience.skills or {}).items():
            phrase = _normalise(skill)
            if not phrase or not _contains_word(low, phrase):
                continue
            try:
                claimed = float(years or 0)
            except (TypeError, ValueError):
                continue
            if claimed > 0:
                positive = True
            else:
                zero = True
        return positive, zero

    def _resolve_skill_absence(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """"Do you have X experience?" answered from the skill map.

        The answer is derived, never assumed: a skill with claimed tenure ->
        Yes, an explicit 0 -> No, and a skill the map never mentions is
        judged HYBRID (user-approved policy): adjacent to the claimed core
        stack -> Yes; a clearly unrelated specialisation -> No; unmatched ->
        No. A blanket No on unclaimed-but-adjacent skills (run 604 fix era)
        wasted apply slots on roles the resume honestly supports.
        """
        m = re.search(
            r"\b(?:do\s+you\s+have|have\s+you(?:\s+got)?)\b[^?]{0,24}?"
            r"\b(?:experience|knowledge|expertise|skills?)\s+"
            r"(?:in|with|of|using)\s+(.+?)\s*\??$",
            text,
        ) or re.search(
            r"\b(?:do\s+you\s+have|have\s+you(?:\s+got)?)\b[^?]{0,24}?"
            r"\b(.+?)\s+(?:experience|knowledge|expertise|skills?)\b",
            text,
        )
        if not m:
            return None
        skill = (m.group(1) or "").strip()
        if not skill or len(skill) > 40:
            return None

        frag = " ".join(t for t in _tokenise(skill) if t not in self._SKILL_FILLER)
        positive, zero = self._skill_tenure_in(frag)
        if not frag or (not positive and not zero):
            # The named fragment was only framing ("hands-on experience
            # building AI agents, RAG pipelines ..."). Judge the question.
            positive, zero = self._skill_tenure_in(text)

        if positive:
            fitted = self._fit_to_options("Yes", question, f"skill-claimed:{skill[:40]}", "skill-map")
            if fitted is not None:
                log.info("answers.skill_claimed", question=question.text[:120], skill=skill[:40])
                return fitted
        label = "skill-zero" if zero else "skill-absent"
        if not zero:
            # Unclaimed skill: hybrid adjacency rule decides Yes/No. The
            # verdict runs on the captured phrase itself — not on the
            # token-join: `_tokenise` yields an unordered frozenset (plus
            # stemmed variants), which scrambles multi-word skills and
            # would miss multi-word list entries ("web technologies").
            verdict = self.experience.unclaimed_verdict(skill)
            if verdict == "yes":
                fitted = self._fit_to_options("Yes", question, f"skill-adjacent:{skill[:40]}", "skill-map")
                if fitted is not None:
                    log.info("answers.skill_unclaimed_adjacent", question=question.text[:120],
                             skill=skill[:40])
                    return fitted
                # A "yes" verdict must never degrade to a fitted "No" just
                # because the form offered no affirmative option yet.
                log.info("answers.skill_unclaimed_adjacent_unfitted", question=question.text[:120],
                         skill=skill[:40], options=len(question.options or ()))
                return None
        fitted = self._fit_to_options("No", question, f"{label}:{skill[:40]}", "skill-map")
        if fitted is not None:
            log.info("answers.skill_not_claimed", question=question.text[:120],
                     skill=skill[:40], reason=label)
        return fitted

    def _fit_to_options(self, answer: str, question: ScreeningQuestion, pattern: str, source: str) -> ResolvedAnswer | None:
        if not question.options:
            return ResolvedAnswer(answer, pattern, source)

        wanted = _expand_contractions(_normalise(answer))
        options = question.options

        def _opt_low(option: str) -> str:
            return _expand_contractions(_normalise(option))

        for option in options:
            if _opt_low(option) == wanted:
                return ResolvedAnswer(option, pattern, source)

        for option in options:
            low = _opt_low(option)
            if _contains_word(low, wanted) or _contains_word(wanted, low):
                return ResolvedAnswer(option, pattern, source)

        # The candidate's own city comes from config; match any spelling of it
        # that an option uses (e.g. "Bengaluru" answer vs "Bangalore" option).
        home_names = {
            marker for marker in self.home_markers
            if marker not in _REMOTE_PLACE_MARKERS
        }
        if home_names and wanted in home_names:
            for option in options:
                low = _opt_low(option)
                if any(name in low for name in home_names):
                    return ResolvedAnswer(option, pattern, source)

        polarity = self._polarity(wanted)
        if polarity is not None:
            for option in options:
                if self._polarity(_opt_low(option)) == polarity:
                    return ResolvedAnswer(option, pattern, "option-match")

        number = _NUMBER.search(wanted)
        if number:
            value = float(number.group())
            exact_single: str | None = None
            for option in options:
                opt_low = option.lower()
                bounds = self._range_bounds(option)

                if len(bounds) >= 2 and min(bounds) <= value <= max(bounds):
                    return ResolvedAnswer(option, pattern, "option-match")
                if len(bounds) == 1:
                    target_b = bounds[0]
                    if target_b == value:
                        exact_single = option
                    elif ("<" in option or "less than" in opt_low or "under" in opt_low) and value <= target_b:
                        return ResolvedAnswer(option, pattern, "option-match")
                    elif (">" in option or "+" in option or "more than" in opt_low or "greater than" in opt_low) and value >= target_b:
                        exact_single = exact_single or option
                elif not bounds:
                    if ("no experience" in opt_low or "none" in opt_low or "not much" in opt_low or "fresher" in opt_low) and value <= 0.5:
                        return ResolvedAnswer(option, pattern, "option-match")
                    if ("served notice" in opt_low or "immediately" in opt_low or "immediate" in opt_low or "0 days" in opt_low or "serving" in opt_low) and value == 0:
                        return ResolvedAnswer(option, pattern, "option-match")
            if exact_single:
                return ResolvedAnswer(exact_single, pattern, "option-match")

            has_yes = any(self._polarity(_normalise(opt)) is True for opt in options)
            has_no = any(self._polarity(_normalise(opt)) is False for opt in options)
            if has_yes and has_no:
                target_polarity = (value > 0)
                for option in options:
                    if self._polarity(_normalise(option)) == target_polarity:
                        return ResolvedAnswer(option, pattern, "option-match")

        # Skip-only option sets (run 469 #31, #34: radio with a single
        # "Skip this question"): no honest answer is selectable, so take the
        # provided Skip instead of queueing a review a human can only skip.
        # Fires ONLY when every option is an explicit skip phrasing — never
        # for "none of the above" (which asserts something about tenure).
        _skip_only = {"skip", "skip this", "skip question", "skip this question",
                      "prefer not to answer", "prefer not to say"}
        if options and all(_normalise(opt) in _skip_only for opt in options):
            log.info("answers.skip_only_option", question=question.text[:120],
                     option=options[0][:60])
            return ResolvedAnswer(options[0], pattern, "option-match-skip")

        if self.strict:
            return None
        return ResolvedAnswer(options[0], pattern, "option-match")

    @staticmethod
    def _range_bounds(option: str) -> list[float]:
        """Numeric bounds inside one option label (years; months → years)."""
        bounds: list[float] = []
        for num_str, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(months?|mos?|years?|yrs?|\+)?", option.lower()):
            val = float(num_str)
            if "month" in unit or unit.strip() == "mo" or unit.strip() == "mos":
                val = val / 12.0
            bounds.append(val)
        return bounds

    def tenure_shortfall(self, question: ScreeningQuestion) -> str | None:
        """Unanswerable-underqualified tenure question (pure diagnosis).

        Returns a detail string when the candidate's EVERY known tenure value
        falls below the lowest option bound (e.g. 3y vs options 4/4+) and no
        skip/zero/catch-all option exists — i.e. no honest selection is
        possible. The caller should SKIP the job, never queue a review asking
        a human to pick a lie. Conservative: any value at/above the floor,
        or no confident value at all, returns None (review as today).
        """
        text = _normalise(question.text)
        options = question.options or []
        if not text or not options:
            return None
        if not _EXPERIENCE_INTENT.search(text):
            return None
        skip_only = {"skip", "skip this", "skip question", "skip this question",
                     "prefer not to answer", "prefer not to say"}
        if any(_normalise(opt) in skip_only for opt in options):
            return None
        bounds: list[float] = []
        for opt in options:
            bounds.extend(self._range_bounds(opt))
        if not bounds:
            return None
        floor = min(bounds)
        values: list[float] = []
        entry = self._best_entry(text)
        if entry is not None:
            m = _NUMBER.search(entry.answer or "")
            if m:
                try:
                    values.append(float(m.group()))
                except (TypeError, ValueError):
                    pass
        shadow = ScreeningQuestion(text=question.text, kind=question.kind, options=[])
        res = self._resolve_experience(text, shadow)
        if res is not None and not (
            res.matched_pattern == "experience:default"
            or res.matched_pattern.startswith("experience:unlisted_default")
        ):
            m = _NUMBER.search(res.value or "")
            if m:
                try:
                    values.append(float(m.group()))
                except (TypeError, ValueError):
                    pass
        values = [v for v in values if v is not None]
        if not values:
            return None
        if all(v < floor for v in values):
            return (
                f"requires {floor:g}y minimum ({'/'.join(options)[:80]}), "
                f"candidate at {max(values):g}y — no honest option exists"
            )
        return None

    @staticmethod
    def _polarity(text: str) -> bool | None:
        tokens = re.findall(r"[a-z']+", text.strip().lower())
        if not tokens:
            return None

        if any(w in ("no", "nope", "never", "false", "cannot", "can't", "cant", "unwilling", "unavailable", "disagree", "not") for w in tokens):
            return False
        if any(w in YES_TOKENS for w in tokens):
            return True
        if any(w in ("can", "ready", "immediately", "immediate", "acceptable") for w in tokens):
            return True
        return None
