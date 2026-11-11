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


if __name__ == "__main__":
    unittest.main()
