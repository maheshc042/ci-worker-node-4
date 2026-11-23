"""
Skip-only option sets (run 469 reviews #31/#34): a radio whose only option
is "Skip this question" must resolve to Skip, not queue a human review.
No browser or DB needed.
"""
import unittest

from naukri_agent.core.answers import AnswerEngine, ExperienceAnswers
from naukri_agent.core.filters import FilterEngine, FilterRules, Job, impute_seniority
from naukri_agent.core.models import ScreeningQuestion, SkipReason
from naukri_agent.core.ranking import CandidateProfile

# Geography tests supply their own home city fixture; production always
# passes `candidate.location` from config.
_HOME = "Bengaluru, India"


def _engine() -> AnswerEngine:
    return AnswerEngine(
        kb=[],
        profile_answers={},
        strict=True,
        home_location=_HOME,
        experience=ExperienceAnswers(
            total_years=3.0,
            default_years=2.0,
            multi_skill_strategy="max",
            skills={"full stack": 3.0, "python": 2.0, "django": 2.0,
                    "fastapi": 2.0, "java": 0.0, "dsa": 3.0,
                    "data structures": 3.0, "algorithms": 3.0,
                    "langgraph": 2.0, "rag": 2.0, "llm": 2.0},
        ),
    )


class TestSkipOnlyOptions(unittest.TestCase):
    def test_tenure_question_with_only_skip_resolves(self):
        """Exact shape of review #34."""
        q = ScreeningQuestion(
            text="How many year of experience you have as Full Stack Developer?",
            kind="radio",
            options=["Skip this question"],
        )
        res = _engine().resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Skip this question")
        self.assertEqual(res.source, "option-match-skip")

    def test_real_options_still_win_over_skip(self):
        q = ScreeningQuestion(
            text="How many years of experience do you have in Python?",
            kind="radio",
            options=["0-1 years", "2-3 years", "Skip this question"],
        )
        res = _engine().resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "2-3 years")

    def test_none_of_the_above_is_not_skip(self):
        """Unknown tenure needs review even when a low numeric band is offered."""
        q = ScreeningQuestion(
            text="How many years of experience do you have in Cobol?",
            kind="radio",
            options=["0-1 years", "None of the above"],
        )
        res = _engine().resolve(q)
        self.assertIsNone(res)


class TestTenureFramings(unittest.TestCase):
    """Sweep 2026-09-30: recruiter reframings must resolve from the map —
    no per-phrasing config entries needed."""

    def _resolve(self, text, options=None):
        return _engine().resolve(ScreeningQuestion(
            text=text, kind="radio", options=options or []))

    def test_how_long_worked_known_skill(self):
        res = self._resolve("How long have you worked with FastAPI?")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "2")

    def test_how_long_worked_unknown_skill_reviews(self):
        res = self._resolve("How long have you worked with Cobol?")
        self.assertIsNone(res)

    def test_number_of_years_framing(self):
        res = self._resolve("Number of years working as a Python developer?")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "2")

    def test_experienced_in_known_skill_yes(self):
        res = self._resolve("Are you experienced in Django?", ["Yes", "No"])
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_experienced_in_unknown_skill_reviews(self):
        res = self._resolve("Are you experienced in Cobol?", ["Yes", "No"])
        self.assertIsNone(res)

    def test_experienced_in_zero_skill_no(self):
        res = self._resolve("Are you experienced in Java?", ["Yes", "No"])
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")

    def test_bare_years_unknown_skill_reviews(self):
        """No experience-word + unknown skill: never inherit a default."""
        res = self._resolve("3 years with Cobol?")
        self.assertIsNone(res)

    def test_built_langgraph_yes(self):
        """Run 504 shape: built/deployed framings resolve from the map."""
        res = _engine().resolve(ScreeningQuestion(
            text="Have you built and deployed multi-step AI agent workflows using LangGraph in production?",
            kind="radio", options=["Yes", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_built_unknown_skill_reviews(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Have you built data pipelines with Cobol?",
            kind="radio", options=["Yes", "No"]))
        self.assertIsNone(res)

    def test_built_zero_skill_no(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Have you deployed Java microservices?",
            kind="radio", options=["Yes", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")


class TestTenureShortfall(unittest.TestCase):
    """Review #38: 3y vs options 4/4+ — no honest selection exists, so the
    JOB skips instead of queueing a human to pick a lie."""

    def _engine(self):
        return AnswerEngine(
            kb=[],
            profile_answers={"relevant experience": "3"},
            strict=True,
            experience=ExperienceAnswers(
                total_years=3.0, default_years=2.0,
                skills={"full stack": 3.0},
            ),
        )

    def _q(self, text, options):
        from naukri_agent.core.models import ScreeningQuestion
        return ScreeningQuestion(text=text, kind="radio", options=options)

    def test_shortfall_detected(self):
        eng = self._engine()
        detail = eng.tenure_shortfall(self._q(
            "How many year of experience you have as Full Stack Developer?",
            ["4 Yrs", "4+ Yrs"]))
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertIn("4", detail)

    def test_no_shortfall_when_option_fits(self):
        eng = self._engine()
        self.assertIsNone(eng.tenure_shortfall(self._q(
            "How many years of experience in Full Stack?",
            ["0-1 years", "2-4 years"])))

    def test_no_shortfall_without_bounds(self):
        eng = self._engine()
        self.assertIsNone(eng.tenure_shortfall(self._q(
            "How many years of experience in Full Stack?", ["Fresher", "Experienced"])))

    def test_no_shortfall_with_skip_option(self):
        eng = self._engine()
        self.assertIsNone(eng.tenure_shortfall(self._q(
            "How many years of experience in Full Stack?",
            ["4 Yrs", "Skip this question"])))

    def test_no_shortfall_overqualified(self):
        eng = self._engine()
        self.assertIsNone(eng.tenure_shortfall(self._q(
            "How many years of experience in Full Stack?", ["0-1", "1-2"])))

    def test_chatbot_result_has_unfit(self):
        from naukri_agent.naukri.chatbot import ChatbotResult
        self.assertIsNone(ChatbotResult().unfit)

    def test_motivation_names_company_no_invention(self):
        res = _engine().resolve(ScreeningQuestion(
            text="What interests you about this opportunity at Vena Solutions?",
            kind="text", options=[]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("Vena Solutions", res.value)
        self.assertIn("3", res.value)
        self.assertNotIn("fintech", res.value.lower())

    def test_motivation_without_company(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Why are you a good fit for this role?",
            kind="text", options=[]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertNotIn("  ", res.value)

    def test_motivation_skips_job_change(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Why are you looking for a change?", kind="text", options=[]))
        self.assertIsNone(res)

    def test_motivation_skips_learning_question(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Why do you want to learn Python?", kind="text", options=[]))
        self.assertIsNone(res)

    def test_serving_status_yes_not_duration_no(self):
        """'Are you SERVING …' is status (Yes), never duration-derived No."""
        eng = AnswerEngine(
            kb=[], profile_answers={"serving notice": "Yes", "notice period": "0 days"},
            strict=True, experience=ExperienceAnswers(total_years=3.0, default_years=2.0),
        )
        res = eng.resolve(ScreeningQuestion(
            text="Are you serving your notice period?",
            kind="radio", options=["Yes", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_notice_duration_dropdown_immediate(self):
        eng = AnswerEngine(
            kb=[], profile_answers={"serving notice": "Yes", "notice period": "0 days"},
            strict=True, experience=ExperienceAnswers(total_years=3.0, default_years=2.0),
        )
        res = eng.resolve(ScreeningQuestion(
            text="What is your current notice period?", kind="dropdown",
            options=["Immediate Joiner", "15 Days", "30 Days"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Immediate Joiner")

    def test_named_role_tenure_reviews(self):
        """SRE-style named roles with no map hit: review, never a default."""
        res = _engine().resolve(ScreeningQuestion(
            text="How many years of experience do you have as a Site Reliability Engineer/Developer?",
            kind="radio", options=[]))
        self.assertIsNone(res)

    def test_non_home_prefers_relocate_option(self):
        """Delhi job offering both claims: relocate wins (run 480)."""
        res = _engine().resolve(ScreeningQuestion(
            text="The location of this job will be Delhi. Are you okay with this?",
            kind="radio",
            options=["I am currently in this location and okay with it",
                     "I am willing to relocate to this location"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("relocate", res.value.lower())

    def test_home_keeps_current_location(self):
        res = _engine().resolve(ScreeningQuestion(
            text="The location of this job will be Bengaluru. Are you okay with this?",
            kind="radio",
            options=["I am currently in this location and okay with it",
                     "I am willing to relocate to this location"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("currently in this location", res.value.lower())

    def test_non_home_no_relocate_reviews(self):
        """No relocate option for a non-home job: the here-claim is a FALSE
        statement of presence, not a Yes-carrier (run 539: claimed current
        location for Delhi and Pune jobs from Bengaluru). Must fall through
        to review, never resolve."""
        res = _engine().resolve(ScreeningQuestion(
            text="The location of this job will be Delhi. Are you okay with this?",
            kind="radio",
            options=["I am currently in this location and okay with it", "No"]))
        self.assertIsNone(res)

    def test_non_home_single_here_claim_reviews(self):
        """Sole here-claim option for a foreign place: quarantine empties the
        list and must NOT fall back to a bare Yes fit."""
        res = _engine().resolve(ScreeningQuestion(
            text="The location of this job will be Pune. Are you okay with this?",
            kind="radio",
            options=["I am currently in this location and okay with it"]))
        self.assertIsNone(res)

    def test_non_home_relocate_still_wins(self):
        """The truthful relocate option still resolves when present."""
        res = _engine().resolve(ScreeningQuestion(
            text="The location of this job will be Delhi. Are you okay with this?",
            kind="radio",
            options=["I am currently in this location and okay with it",
                     "I am willing to relocate to this location"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("relocate", res.value.lower())

    def test_non_home_negated_presence_relocate_resolves(self):
        """Review #57/#58 verbatim: "I am not in this location but can
        relocate" — its "not" negates presence, not willingness. The
        polarity/negation gates must not screen out the truthful answer."""
        for place in ("Delhi", "Pune"):
            res = _engine().resolve(ScreeningQuestion(
                text=f"The location of this job will be {place}. Are you okay with this?",
                kind="radio",
                options=["I am currently in this location and okay with it",
                         "I am not in this location but can relocate",
                         "I am not okay with this location"]))
            self.assertIsNotNone(res)
            assert res is not None
            self.assertIn("can relocate", res.value.lower())

    def test_non_home_relocate_refusal_reviews(self):
        """"I am not willing to relocate" is a genuine refusal: the
        carve-out must not match it, and nothing else may either."""
        res = _engine().resolve(ScreeningQuestion(
            text="The location of this job will be Delhi. Are you okay with this?",
            kind="radio",
            options=["I am currently in this location and okay with it",
                     "I am not willing to relocate",
                     "I am not okay with this location"]))
        self.assertIsNone(res)

    def test_home_here_claim_kept(self):
        """Bengaluru job: the here-claim is true and still resolves."""
        res = _engine().resolve(ScreeningQuestion(
            text="The location of this job will be Bengaluru. Are you okay with this?",
            kind="radio",
            options=["I am currently in this location and okay with it", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("currently in this location", res.value.lower())

    def test_location_pick_home_city(self):
        """'Which of these locations' with home present: pick home."""
        res = _engine().resolve(ScreeningQuestion(
            text="Which of these locations are you willing to relocate to?",
            kind="checkbox",
            options=["Bengaluru", "Mumbai", "Delhi", "Pune", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Bengaluru")

    def test_location_pick_relocate_city(self):
        """No home city: first non-negative city (relocate-willing policy)."""
        res = _engine().resolve(ScreeningQuestion(
            text="Which of these locations are you willing to relocate to?",
            kind="checkbox",
            options=["Mumbai", "Delhi", "Pune"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Mumbai")

    def test_take_up_interview_yes(self):
        """Mined gap: 'take up an interview' never reaches review."""
        res = _engine().resolve(ScreeningQuestion(
            text="Are you available to take up an interview(virtual)?",
            kind="radio", options=["Yes", "No", "Skip this question"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")


class TestFluencyAndRating(unittest.TestCase):
    """Mined gaps: fluency questions and 1-10 self-ratings."""

    def test_fluent_known_skill_yes(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Are you fluent in Python Programming?",
            kind="radio", options=["Yes", "No", "Skip this question"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_fluent_zero_skill_no(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Are you fluent in Java?", kind="radio",
            options=["Yes", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")

    def test_fluent_unknown_skill_reviews(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Are you fluent in Cobol?", kind="radio",
            options=["Yes", "No"]))
        self.assertIsNone(res)

    def test_self_rating_known_skill_eight(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Rate yourself from 1-10 in Data Structures and Algorithms?",
            kind="text", options=[]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "8")

    def test_self_rating_unknown_skill_reviews(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Rate yourself 1 to 10 in Cobol?", kind="text", options=[]))
        self.assertIsNone(res)

    def test_rating_never_types_yes(self):
        """Willingness must not answer a rating box with 'Yes'."""
        res = _engine().resolve(ScreeningQuestion(
            text="How would you rate your Python skills on a scale of 1-10?",
            kind="text", options=[]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "8")


class TestTitleDevAndEngineeringEquivalence(unittest.TestCase):
    """Verify 'dev' <-> 'developer' and 'engineering' <-> 'engineer' title normalisation."""

    def setUp(self):
        self.rules = FilterRules(
            title_must_include_any=[
                "ai developer", "ai full stack developer", "platform engineer"
            ],
            title_must_exclude_any=["manager", "lead"],
        )
        self.candidate = CandidateProfile(
            title_keywords=["ai", "python"],
            core_skills=["python", "fastapi"],
            secondary_skills=["react"],
            target_experience_years=2.5,
        )
        self.engine = FilterEngine(self.rules, self.candidate)

    def test_dev_suffix_matches_developer(self):
        job = Job(job_id="t1", platform="linkedin", title="Junior AI full stack dev", company="C", url="http://x")
        dec = self.engine.evaluate_card(job)
        self.assertTrue(dec.passed)

    def test_engineering_suffix_matches_engineer(self):
        job = Job(job_id="t2", platform="linkedin", title="AI Platform Engineering (B2B Integration)", company="C", url="http://x")
        dec = self.engine.evaluate_card(job)
        self.assertTrue(dec.passed)

    def test_senior_level_3_still_rejected_by_experience(self):
        # Even when title is included (e.g. software engineer), level-3 seniority rejects by experience ceiling
        rules_swe = FilterRules(
            title_must_include_any=["software engineer", "software development engineer"],
            title_must_exclude_any=["manager", "lead"],
        )
        engine_swe = FilterEngine(rules_swe, self.candidate)
        job = Job(job_id="t3", platform="linkedin", title="Software Development Engineer - 3 (Backend)", company="C", url="http://x")
        impute_seniority(job)
        dec = engine_swe.evaluate_card(job)
        self.assertFalse(dec.passed)
        self.assertEqual(dec.reason, SkipReason.FILTER_EXPERIENCE)

    def test_negative_answer_maps_to_skip_checkbox(self):
        """Review #613 shape: explicit 'No' maps to 'Skip this question' when checklist lacks 'No'."""
        eng = AnswerEngine(
            kb=[],
            profile_answers={"do you have the experience in below industry/ domain": "No"},
            strict=True,
            home_location=_HOME,
        )
        q = ScreeningQuestion(
            text="Do you have the experience in below industry/ Domain",
            kind="checkbox",
            options=["Healthcare", "Bio", "Medical", "Hospitality", "Clinical", "Drug", "Skip this question"],
        )
        res = eng.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Skip this question")
        self.assertEqual(res.source, "option-match-skip")

    def test_manual_testing_compound_override_and_qa_variants(self):
        """Run #756 fix: compound automation + manual testing titles and QA variants pass."""
        rules_qa = FilterRules(
            title_must_include_any=[
                "qa engineer", "test engineer", "testing engineer", "qa tester",
                "frontend qa", "qa specialist", "database tester", "qa manual test engineer",
            ],
            title_must_exclude_any=["manual testing", "manual test"],
        )
        engine_qa = FilterEngine(rules_qa, self.candidate)
        for title in (
            "QA Engineer - Automation & Manual testing WITH Product Company",
            "Qa Manual Test Engineer",
            "Frontend QA",
            "QA Tester",
            "Testing Engineer I",
            "Sr QA Specialist",
            "Database Tester",
        ):
            job = Job(job_id="qa-1", platform="naukri", title=title, company="Corp", url="http://x", min_experience=1.0, max_experience=3.0)
            self.assertTrue(engine_qa.evaluate_card(job).passed, f"Expected pass for {title}")

    def test_cutshort_resume_pattern_extended_suffix(self):
        from pathlib import Path
        from naukri_agent.platforms.cutshort import _resume_label_pattern
        fs_path = Path("resumes/CV_Mahesh_Chitakoti_2026_1_.pdf")
        ai_path = Path("resumes/CV_Mahesh_Chitakoti_2026.pdf")
        self.assertIsNotNone(_resume_label_pattern(fs_path).fullmatch("CV_Mahesh_Chitakoti_2026_1_X9aB1234.pdf"))
        self.assertIsNone(_resume_label_pattern(ai_path).fullmatch("CV_Mahesh_Chitakoti_2026_1_X9aB1234.pdf"))


class TestRun758FilterFixes(unittest.TestCase):
    """Run #758 filter audits: AI systems override, FDSE/DET inclusion, frontend guard."""

    @classmethod
    def setUpClass(cls):
        from naukri_agent.config import AgentConfig
        cls.cfg = AgentConfig.load()

    def _eval(self, profile, title: str, platform: str = "naukri", min_exp: float = 2.0, max_exp: float = 4.0):
        cand = profile.to_candidate_profile(self.cfg)
        engine = FilterEngine(profile.filters, cand)
        job = Job(
            job_id=f"t-{title[:20]}",
            platform=platform,
            title=title,
            company="TestCorp",
            url="https://example.com/job/1",
            min_experience=min_exp,
            max_experience=max_exp,
        )
        impute_seniority(job)
        return engine.evaluate_card(job)

    def test_ai_systems_engineer_override_and_systems_guard(self):
        ai_prof = next(p for p in self.cfg.profiles if p.name == "AI / Python Engineer")
        profiles = [
            ("naukri", ai_prof),
            ("wellfound", self.cfg.get_unified_wellfound_profile()),
            ("instahyre", self.cfg.get_unified_instahyre_profile()),
            ("cutshort", self.cfg.get_unified_cutshort_profile()),
        ]
        for platform, prof in profiles:
            dec = self._eval(prof, "AI Systems Engineer (3+ Years Experience)", platform=platform, min_exp=3.0, max_exp=5.0)
            self.assertTrue(dec.passed, f"Expected pass on {platform}: {dec}")
            for blocked in ("Systems Engineer", "Embedded Systems Engineer", "Senior Systems Engineer"):
                blk = self._eval(prof, blocked, platform=platform, min_exp=2.0, max_exp=4.0)
                self.assertFalse(blk.passed, f"Expected {blocked!r} blocked on {platform}")

    def test_fdse_and_det_included_while_frontend_stays_blocked(self):
        fs_prof = next(p for p in self.cfg.profiles if p.name == "Full Stack / Web Developer")
        ai_prof = next(p for p in self.cfg.profiles if p.name == "AI / Python Engineer")
        insta_prof = self.cfg.get_unified_instahyre_profile()

        self.assertTrue(self._eval(ai_prof, "Forward Deployed Software Engineer", platform="naukri").passed)
        self.assertTrue(self._eval(fs_prof, "Forward Deployed Software Engineer", platform="naukri").passed)
        self.assertTrue(self._eval(insta_prof, "Senior Development Engineer in Test - 2", platform="instahyre").passed)

        for blocked_fe in ("Frontend Engineer", "Front End Developer"):
            blk = self._eval(fs_prof, blocked_fe, platform="naukri")
            self.assertFalse(blk.passed, f"Intentional block violated for {blocked_fe!r}")


class TestCutshortSweepFixes(unittest.IsolatedAsyncioTestCase):
    """Verify real-DOM Cutshort sweep edge cases and Pending->Inbox time budget split."""

    async def test_scan_message_rows_real_dom_leading_space_undefined_duplicates_closed(self):
        from time import monotonic
        from unittest.mock import AsyncMock, MagicMock
        from playwright.async_api import async_playwright
        from naukri_agent.platforms.cutshort import CutshortPlatform

        html = """<!doctype html><html><body>
        <div id="tabpanel-awaiting" role="tabpanel">
          <ul role="list" style="height:600px;overflow:auto;">
            <div role="button" data-idx="0">
              <img alt=" Insurance advisory and distribution platform profile picture" />
              <div> Insurance advisory and distribution platform</div>
              <div>For job Senior Backend Engineer</div>
              <span>[Questionnaire]</span>
            </div>
            <div role="button" data-idx="1">
              <img alt="undefined profile picture" />
              <div></div>
              <div>For job Application Engineer</div>
              <span>[Questionnaire]</span>
            </div>
            <div role="button" data-idx="2">
              <img alt="Acme Corp profile picture" />
              <div>Acme Corp</div>
              <div>For job Full Stack Engineer</div>
              <span>[Questionnaire]</span>
            </div>
            <div role="button" data-idx="3">
              <img alt="Acme Corp profile picture" />
              <div>Acme Corp</div>
              <div>For job Full Stack Engineer</div>
              <span>[Questionnaire]</span>
            </div>
            <div role="button" data-idx="4">
              <img alt="OldStartup profile picture" />
              <div>OldStartup</div>
              <div>For job Python Developer</div>
              <span>[Questionnaire]</span>
            </div>
          </ul>
        </div>
        <div id="pane" class="sc-259ba717-13">Select a thread</div>
        <script>
        const panes = {
          "0": `<div>Insurance advisory and distribution platform</div><div>For job Senior Backend Engineer</div><a href="https://cutshort.io/job/ins-1">View full job description</a>`,
          "1": `<div>adhesives</div><div>Agency job</div><div>For job Application Engineer</div><a href="https://cutshort.io/job/agency-2">View full job description</a>`,
          "2": `<div>Acme Corp</div><div>For job Full Stack Engineer</div><a href="https://cutshort.io/job/acme-3">View full job description</a>`,
          "3": `<div>Acme Corp</div><div>For job Full Stack Engineer</div><a href="https://cutshort.io/job/acme-3b">View full job description</a>`,
          "4": `<div>OldStartup</div><div>For job Python Developer</div><div>Closed conversation thread</div>`,
        };
        document.querySelectorAll("div[role='button']").forEach(btn => {
          btn.addEventListener("click", () => {
            document.getElementById("pane").innerHTML = panes[btn.dataset.idx];
          });
        });
        </script>
        </body></html>"""

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                plat = object.__new__(CutshortPlatform)
                plat.page = page
                plat.artifacts = MagicMock(dump_html=AsyncMock())
                plat.questionnaire_stats = dict(
                    checked=0, submitted=0, already_submitted=0,
                    stale=0, unknown_age=0, unconfirmed=0, incomplete=0,
                )
                opened = []

                async def fake_answer(pane, key, unanswered, expected_questionnaire, **kwargs):
                    opened.append(key)
                    return True

                plat._answer_current_chat = fake_answer
                seen_jobs: set[str] = set()
                await plat._scan_message_rows(
                    page.locator("#tabpanel-awaiting"),
                    set(),
                    [],
                    deadline=monotonic() + 20.0,
                    pending=True,
                    seen_jobs=seen_jobs,
                )
                self.assertEqual(plat.questionnaire_stats["unconfirmed"], 0)
                self.assertIn(("Insurance advisory and distribution platform", "For job Senior Backend Engineer"), opened)
                self.assertIn(("undefined", "For job Application Engineer"), opened)
                self.assertIn(("Acme Corp", "For job Full Stack Engineer"), opened)
                self.assertIn(("OldStartup", "For job Python Developer"), opened)
                self.assertIn("thread:OldStartup:For job Python Developer", seen_jobs)
            finally:
                await browser.close()

    async def test_handle_messages_pending_timeout_still_runs_inbox(self):
        from time import monotonic
        from unittest.mock import AsyncMock, MagicMock, patch
        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.platforms.cutshort import CutshortPlatform

        plat = object.__new__(CutshortPlatform)
        plat.policy = RunPolicy(dry_run=False, side_effects_enabled=True)
        plat.page = MagicMock()
        plat.page.goto = AsyncMock()
        tab = MagicMock(wait_for=AsyncMock(), get_attribute=AsyncMock(return_value="true"), click=AsyncMock())
        panel = MagicMock(wait_for=AsyncMock())

        def locator_side_effect(selector: str):
            if selector == "#tab-awaiting":
                return tab
            return MagicMock(first=panel)

        plat.page.locator.side_effect = locator_side_effect
        phases: list[bool] = []

        async def fake_scan(p_loc, processed, unanswered, phase_deadline, *, pending=False, seen_jobs=None):
            phases.append(pending)
            if pending:
                raise TimeoutError("pending backlog exhausted budget")

        plat._scan_message_rows = fake_scan
        with patch("naukri_agent.platforms.cutshort.first_visible", new=AsyncMock(return_value=MagicMock())):
            await plat.handle_messages(deadline=monotonic() + 30.0)

        self.assertEqual(phases, [True, False])
        self.assertEqual(plat.questionnaire_stats["incomplete"], 1)


class TestRun806AnswerResolutions(unittest.TestCase):
    """Test resolution of Run 806 reviews #720-#724."""

    @classmethod
    def setUpClass(cls):
        from naukri_agent.config import AgentConfig
        cls.cfg = AgentConfig.load()
        cls.prof = next(p for p in cls.cfg.profiles if p.name == "AI / Python Engineer")
        answers = {**cls.cfg.answers, **cls.prof.answers}
        cls.eng = AnswerEngine(
            kb=[],
            profile_answers=answers,
            experience=cls.cfg.experience_for(cls.prof),
            home_location=cls.cfg.candidate.location,
            strict=True,
        )

    def test_salutation(self):
        q = ScreeningQuestion("SALUTATION", "radio", ["Mr.", "Mrs.", "Miss"])
        res = self.eng.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Mr.")

    def test_ai_platforms_checkbox(self):
        q = ScreeningQuestion(
            "Which AI platforms have you used professionally?",
            "checkbox",
            ["ChatGPT / OpenAI", "Claude", "Gemini", "Multiple of the above", "None"],
        )
        res = self.eng.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Multiple of the above")

    def test_large_scale_data_processing(self):
        q = ScreeningQuestion("Have you worked on optimizing large-scale data processing jobs?", "text", [])
        res = self.eng.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_tsql_experience(self):
        q = ScreeningQuestion("Experience in TSQL?", "text", [])
        res = self.eng.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "3")

    def test_cloudwatch(self):
        q = ScreeningQuestion("Have you worked on CloudWatch?", "text", [])
        res = self.eng.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")


class TestRun808AnswerResolutions(unittest.TestCase):
    """Test resolution of Run 808 reviews #729-#747 and Cutshort resume matching."""

    @classmethod
    def setUpClass(cls):
        from naukri_agent.config import AgentConfig
        cls.cfg = AgentConfig.load()
        cls.ai_prof = next(p for p in cls.cfg.profiles if p.name == "AI / Python Engineer")
        cls.fs_prof = next(p for p in cls.cfg.profiles if p.name == "Full Stack / Web Developer")
        cls.eng_ai = AnswerEngine(
            kb=[],
            profile_answers={**cls.cfg.answers, **cls.ai_prof.answers},
            experience=cls.cfg.experience_for(cls.ai_prof),
            home_location=cls.cfg.candidate.location,
            strict=True,
        )
        cls.eng_fs = AnswerEngine(
            kb=[],
            profile_answers={**cls.cfg.answers, **cls.fs_prof.answers},
            experience=cls.cfg.experience_for(cls.fs_prof),
            home_location=cls.cfg.candidate.location,
            strict=True,
        )

    def test_cutshort_resume_pattern_with_counter_suffix(self):
        from pathlib import Path
        from naukri_agent.platforms.cutshort import _resume_label_pattern, _resume_name_matches

        fs_path = Path("resumes/CV_Mahesh_Chitakoti_2026_1_.pdf")
        for candidate in [
            "CV_Mahesh_Chitakoti_2026_1_.pdf",
            "CV_Mahesh_Chitakoti_2026_1__1.pdf",
            "CV_Mahesh_Chitakoti_2026_1_1.pdf",
            "CV_Mahesh_Chitakoti_2026_1_ab12.pdf",
            "Mahesh_Chitakoti_FullStack_Engineer.pdf",
        ]:
            self.assertTrue(_resume_name_matches(candidate, fs_path), f"Failed to match {candidate}")

    def test_email_id(self):
        res = self.eng_ai.resolve(ScreeningQuestion("Your Email ID", "text", []))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "maheshrwd042@gmail.com")

    def test_az_certification_no(self):
        res = self.eng_ai.resolve(ScreeningQuestion("Do you Have certification in AZ 104 OR AZ 900?", "radio", ["YES", "NO"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "NO")

    def test_trufflehog_no(self):
        res = self.eng_ai.resolve(ScreeningQuestion("Do you have experience in operational management and global governance of TruffleHog secret-scanning platform?", "text", []))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")

    def test_qualification_specialization(self):
        res = self.eng_ai.resolve(ScreeningQuestion("Qualification With Specialization?", "text", []))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "B.E. in Computer Science and Engineering")

    def test_apis_and_integrations(self):
        res = self.eng_ai.resolve(ScreeningQuestion("Have you worked with APIs and system integrations?", "radio", ["Extensive experience", "Moderate experience", "Basic experience", "No experience"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Extensive experience")

    def test_fintech_and_incident_management(self):
        res_fintech = self.eng_fs.resolve(ScreeningQuestion("Have you integrated with Indian fintech services such as KYC, credit bureaus, UPI AutoPay, eNACH or payment gateways?", "radio", ["Yes", "No"]))
        self.assertIsNotNone(res_fintech)
        assert res_fintech is not None
        self.assertEqual(res_fintech.value, "Yes")

        res_incident = self.eng_fs.resolve(ScreeningQuestion("Have you worked on production incident management, monitoring, debugging and resolution?", "radio", ["Yes", "No"]))
        self.assertIsNotNone(res_incident)
        assert res_incident is not None
        self.assertEqual(res_incident.value, "Yes")

    def test_rbi_compliance_no(self):
        res = self.eng_fs.resolve(ScreeningQuestion("Are you familiar with RBI Digital Lending Directions and related compliance requirements?", "radio", ["Yes", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")

    def test_webhooks_and_aws(self):
        res_wh = self.eng_fs.resolve(ScreeningQuestion("Have you implemented webhooks, retries, idempotency and reconciliation mechanisms?", "radio", ["Yes", "No"]))
        self.assertIsNotNone(res_wh)
        assert res_wh is not None
        self.assertEqual(res_wh.value, "Yes")

        res_aws = self.eng_fs.resolve(ScreeningQuestion("Have you independently managed or maintained production applications on AWS?", "radio", ["Yes", "No"]))
        self.assertIsNotNone(res_aws)
        assert res_aws is not None
        self.assertEqual(res_aws.value, "Yes")

    def test_jest_vitest_automated_tests(self):
        res = self.eng_fs.resolve(ScreeningQuestion("Have you written automated tests using frameworks such as Vitest or Jest?", "radio", ["Yes", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_years_brackets_and_cicd(self):
        res_years = self.eng_fs.resolve(ScreeningQuestion("Please indicate your experience in number of years", "radio", ["2-4", "4-6", "6-8", "8+"]))
        self.assertIsNotNone(res_years)
        assert res_years is not None
        self.assertEqual(res_years.value, "2-4")

        res_cicd = self.eng_fs.resolve(ScreeningQuestion("Describe a CI/CD pipeline you set up from scratch. What tools did you use, and how did it improve deployment reliability or speed?", "text", []))
        self.assertIsNotNone(res_cicd)
        assert res_cicd is not None
        self.assertTrue("GitHub Actions" in res_cicd.value)

    def test_voice_speech_ai_and_experience_bracket(self):
        res_voice = self.eng_ai.resolve(ScreeningQuestion("do you have exp Voice/Speech AI + LLMs + Prompt Engineering", "radio", ["Yes", "No"]))
        self.assertIsNotNone(res_voice)
        assert res_voice is not None
        self.assertEqual(res_voice.value, "Yes")

        res_exp = self.eng_ai.resolve(ScreeningQuestion("do you have exp in 3 to  8 years", "radio", ["Yes", "No"]))
        self.assertIsNotNone(res_exp)
        assert res_exp is not None
        self.assertEqual(res_exp.value, "Yes")

    def test_education_percentages(self):
        res_edu = self.eng_fs.resolve(ScreeningQuestion("Please Mentioned your 10th, 12th, Btech/MCA/Mtech %", "text", []))
        self.assertIsNotNone(res_edu)
        assert res_edu is not None
        self.assertEqual(res_edu.value, "10th: 67%, 12th: 74%, B.E.: 60%")


class TestRun809AnswerResolutions(unittest.TestCase):
    """Run #809 fixes: RAG project description and Location vs Organization resolution."""

    @classmethod
    def setUpClass(cls):
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cls.engine = AnswerEngine(
            kb=list(cfg.get("answers", {}).items()),
            home_location=cfg.get("candidate", {}).get("location", "Bengaluru, India"),
        )

    def test_rag_langchain_fastapi_project_description(self):
        q = ScreeningQuestion("Describe your latest project using RAG, Langchain and FastAPI", "text", [])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertTrue("WorkBridge" in res.value)
        self.assertTrue("FastAPI" in res.value)
        self.assertTrue("LangChain" in res.value or "LangGraph" in res.value)

    def test_current_location_others_when_not_home(self):
        q = ScreeningQuestion("What is your current location?", "radio", ["Mumbai", "Navi Mumbai", "Others"])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Others")

    def test_current_location_home_when_present(self):
        q = ScreeningQuestion("What is your current location?", "radio", ["Bengaluru", "Mumbai", "Others"])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Bengaluru")

    def test_current_location_text(self):
        q = ScreeningQuestion("What is your current location?", "text", [])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Bengaluru")

    def test_fuzzy_domain_mismatch_location_vs_org(self):
        # A location question should never fuzzy-match an organization answer
        engine_limited = AnswerEngine(
            kb=[("what is your current organization?", "Independent Software Consultant")],
            home_location="Bengaluru, India",
        )
        q = ScreeningQuestion("What is your current location?", "radio", ["Mumbai", "Navi Mumbai", "Others"])
        res = engine_limited.resolve(q)
        # Should NOT return "Independent Software Consultant" or fit it
        self.assertIsNone(res)


class TestRun811Reviews(unittest.TestCase):
    """Reviews #752 - #768: Experience checkboxes, Gender, DO-178C, Avionics, RSU, etc."""

    @classmethod
    def setUpClass(cls):
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cls.engine = AnswerEngine(
            kb=list(cfg.get("answers", {}).items()),
            home_location=cfg.get("candidate", {}).get("location", "Bengaluru, India"),
            candidate_gender=cfg.get("candidate", {}).get("gender", "Male"),
        )

    def test_hands_on_all_of_the_above(self):
        q = ScreeningQuestion(
            "In which of the following do you have hands-on working experience? (2+)",
            "checkbox",
            ["Python Coding", "NLP", "RAG", "LLM", "GenAI / Agentic AI", "Cloud (AWS/Azure/GCP)", "Computer Vision (Basics)", "Client Engagement", "End to End Project Delivery", "All of the above"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "All of the above")

    def test_gender_resolution(self):
        q = ScreeningQuestion(
            "Gender*",
            "select",
            ["Select an option", "Male", "Female", "Trans", "Others", "Do not wish to disclose"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Male")

    def test_current_unvested_rsu(self):
        q = ScreeningQuestion("Current Unvested Options/RSU value", "text", [])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "0")

    def test_do_178c_no(self):
        q = ScreeningQuestion("Experience in DO-178C?", "radio", ["Yes", "No"])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")

    def test_debugging_root_cause_yes(self):
        q = ScreeningQuestion("Experience in debugging and root-cause analysis?", "radio", ["Yes", "No"])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_automated_testing_frameworks_yes(self):
        q = ScreeningQuestion("Understanding of automated testing methodologies and frameworks?", "radio", ["Yes", "No"])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_avionics_automotive_no(self):
        q = ScreeningQuestion(
            "Experience in the Avionics or Automotive industry will be highly preferred, with familiarity?",
            "radio",
            ["Yes", "No"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")

    def test_happiest_minds_no(self):
        q = ScreeningQuestion("Have you been employed by Happiest Minds before?", "radio", ["Yes", "No"])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")

    def test_communication_skills_excellent(self):
        q = ScreeningQuestion(
            "communication skills?",
            "radio",
            ["Average", "Good", "Excellent", "Skip this question"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Excellent")

    def test_automated_test_code_months(self):
        q = ScreeningQuestion(
            "How many months of professional experience do you have personally writing and maintaining automated test code, using any automation framework?",
            "text",
            [],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "30")

    def test_what_made_you_apply(self):
        q = ScreeningQuestion(
            "What made you apply for this role? Pick one specific thing from the job description where you think you can shine, and tell us why.",
            "text",
            [],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertTrue("I applied because" in res.value or "FastAPI" in res.value or "Python" in res.value)

    def test_visa_type_other(self):
        q = ScreeningQuestion(
            "Please select your visa type",
            "radio",
            ["H1B", "F1 -OPT/CPT", "L1", "B2", "Other"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Other")

    def test_playwright_built_improved_yes(self):
        q = ScreeningQuestion(
            "Have you personally built or improved reusable Playwright framework parts, e.g. fixtures, page objects, test data, test isolation or CI/CD?",
            "radio",
            ["Yes", "No"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_wipro_subsidiaries_prior_employer_no(self):
        q = ScreeningQuestion(
            "Have you ever been employed with Wipro or its subsidiaries?",
            "radio",
            ["Yes", "No"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")

    def test_pf_for_all_employments_yes(self):
        q = ScreeningQuestion(
            "Do you have PF for all of your employments?",
            "text",
            [],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_legally_authorized_to_work_yes(self):
        q = ScreeningQuestion(
            "Are you legally authorized to work in the country where you are applying?",
            "radio",
            ["Yes", "No"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_c_cpp_algorithms_dsa_yes(self):
        q = ScreeningQuestion(
            "Having knowledge about C/C++ , algorithms & data structure?",
            "radio",
            ["Yes", "No", "Skip this question"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_describe_you_gets_things_done(self):
        q = ScreeningQuestion(
            "How do people who've worked closely with you typically describe you?",
            "radio",
            ["Gets things done without being told twice", "Asks sharp questions", "Thinks it through before jumping in"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Gets things done without being told twice")

    def test_where_in_2_years(self):
        q = ScreeningQuestion(
            "Where do you want to be in 2 years — and why does this role fit that path? (3 lines max)",
            "text",
            [],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("2 years", res.value)

    def test_raw_imperfect_datasets_extensively(self):
        q = ScreeningQuestion(
            "Have you worked with raw or imperfect datasets involving data cleaning, quality assessment, validation and combining data from multiple sources?",
            "radio",
            ["Yes, extensively", "Yes, occasionally", "Limited exposure", "No"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes, extensively")

    def test_genai_courses_certifications(self):
        q = ScreeningQuestion(
            "Have you done any GenAI/AI/Agentic AI related courses?",
            "text",
            [],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("courses and certifications", res.value)

    def test_databricks_azure_skip_only(self):
        q = ScreeningQuestion(
            "Databricks , Azure ?",
            "radio",
            ["Skip this question"],
        )
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Skip this question")


class TestCutshortResumeVerification(unittest.TestCase):
    def test_cutshort_candidate_resume_matching(self):
        from pathlib import Path
        from naukri_agent.platforms.cutshort import _is_candidate_resume, _resume_name_matches

        fs_path = Path("resumes/CV_Mahesh_Chitakoti_2026_1_.pdf")
        ai_path = Path("resumes/CV_Mahesh_Chitakoti_2026.pdf")

        # Candidate's uploaded resume with Cutshort suffix
        displayed_on_cutshort = "CV_Mahesh_Chitakoti_2026_3XGe"
        self.assertTrue(_is_candidate_resume(displayed_on_cutshort))
        self.assertTrue(_resume_name_matches(displayed_on_cutshort, ai_path))
        self.assertTrue(_resume_name_matches(displayed_on_cutshort, fs_path))

        # Check other variations
        self.assertTrue(_is_candidate_resume("CV_Mahesh_Chitakoti_2026_1__6Lth"))
        self.assertTrue(_resume_name_matches("CV_Mahesh_Chitakoti_2026_1__6Lth", fs_path))

        # Other candidate should NOT match
        self.assertFalse(_is_candidate_resume("John_Doe_Resume_2026"))
        self.assertFalse(_resume_name_matches("John_Doe_Resume_2026", fs_path))


class TestRun818ResolversAndCutshort(unittest.TestCase):
    def setUp(self):
        self.engine = AnswerEngine(
            kb=[],
            profile_answers={},
            strict=True,
            home_location="Bengaluru, Karnataka, India",
        )

    def test_pincode_resolution(self):
        q = ScreeningQuestion(text="Pincode", kind="text")
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        self.assertEqual(res.value, "560060")

    def test_consent_resolution(self):
        q = ScreeningQuestion(text="I consent", kind="radio", options=["I consent"])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        self.assertEqual(res.value, "I consent")

    def test_flutter_resolution(self):
        q = ScreeningQuestion(text="Have you published a Flutter app on both the App Store and Play Store?", kind="radio", options=["Yes", "No"])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        self.assertEqual(res.value, "No")

    def test_founding_engineer_resolution(self):
        q = ScreeningQuestion(text="Are you interested to work and stay along with founders as a founding engineer?", kind="radio", options=["Yes", "No"])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        self.assertEqual(res.value, "Yes")

        q2 = ScreeningQuestion(text="Have you been one of the first engineers on a product and taken it from an empty repo to live users?", kind="radio", options=["Yes", "No"])
        res2 = self.engine.resolve(q2)
        self.assertIsNotNone(res2)
        self.assertEqual(res2.value, "Yes")

    def test_time_series_resolution(self):
        q = ScreeningQuestion(text="Do you have hands-on experience with time-series forecasting models such as ARIMA, SARIMA, SARIMAX, and XGBoost?", kind="radio", options=["Yes", "No", "Skip this question"])
        res = self.engine.resolve(q)
        self.assertIsNotNone(res)
        self.assertEqual(res.value, "Skip this question")


if __name__ == "__main__":
    unittest.main()
