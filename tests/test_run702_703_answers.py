import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_agent.config import AgentConfig
from naukri_agent.core.answers import AnswerEngine, ExperienceAnswers
from naukri_agent.core.filters import FilterEngine
from naukri_agent.core.models import Job, ScreeningQuestion, SkipReason
from naukri_agent.core.orchestrator import Orchestrator
from naukri_agent.notify.notifier import header_prefix
from naukri_agent.naukri.apply import ApplyEngine


class TestArchivedAnswerRegressions(unittest.TestCase):
    def test_question_alert_has_review_status(self):
        self.assertEqual(header_prefix("review"), "❓ REVIEW")

    def test_mobile_experience_cannot_use_phone_number(self):
        engine = AnswerEngine(
            [], profile_answers={"mobile": "1234567890"},
            experience=ExperienceAnswers(total_years=3, default_years=1, skills={"python": 3}),
        )
        self.assertIsNone(engine.resolve(ScreeningQuestion(
            "How many years of experience do you have in Mobile Automation Testing?", "text",
        )))

    def test_unlisted_numeric_tenure_is_reviewed(self):
        engine = AnswerEngine([], experience=ExperienceAnswers(
            total_years=3, default_years=1, skills={"python": 3},
        ))
        for skill in ("Postman", "Websocket", "Site Reliability Engineering", "Webdriver IO"):
            with self.subTest(skill=skill):
                self.assertIsNone(engine.resolve(ScreeningQuestion(
                    f"How many years of experience do you have in {skill}?", "text",
                )))

    def test_generic_testing_cannot_override_explicit_zero(self):
        engine = AnswerEngine([], profile_answers={"experience in testing": "3"},
                              experience=ExperienceAnswers(skills={"selenium": 0, "automation": 1}))
        for skill in ("Selenium Testing", "Automation Selenium"):
            answer = engine.resolve(ScreeningQuestion(
                f"How many years of experience do you have in {skill}?", "text",
            ))
            self.assertIsNotNone(answer)
            self.assertEqual(answer.value, "0")


class TestProfileSkillAnswer(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_title_and_url_do_not_prove_submission(self):
        engine = object.__new__(ApplyEngine)
        engine.page = MagicMock()
        engine.page.goto = AsyncMock()
        engine.page.title = AsyncMock(return_value="Apply Confirmation")
        engine.page.url = "https://www.naukri.com/apply/confirmation"
        engine.nav_timeout_ms = 1000
        with (
            patch("naukri_agent.naukri.apply.first_visible", new=AsyncMock(return_value=None)),
            patch("naukri_agent.naukri.apply.human_pause", new=AsyncMock()),
        ):
            self.assertEqual(await engine._verify_post_submit(
                Job(job_id="audit", title="Engineer", company="Example", url="https://www.naukri.com/job-listings-example")
            ), "unknown")
        engine.page.title.assert_not_awaited()

    async def test_skills_use_profile_and_preserve_learned_answer(self):
        config = AgentConfig.load()
        profile = config.active_profiles(account="secondary")[0]
        agent = object.__new__(Orchestrator)
        agent.config = config
        agent.repo = MagicMock()
        agent.repo.seed_answer_kb = AsyncMock()
        agent.repo.load_answer_kb = AsyncMock(return_value=[])
        engine = await agent._build_answer_engine(profile)
        result = engine.resolve(ScreeningQuestion("Top Technical Skills", "text"))
        self.assertEqual(result.value, ", ".join(profile.core_skills[:10]))
        agent.repo.load_answer_kb.return_value = [("top technical skills", "Approved skill answer")]
        engine = await agent._build_answer_engine(profile)
        self.assertEqual(engine.resolve(ScreeningQuestion("Top Technical Skills", "text")).value,
                         "Approved skill answer")


class TestArchivedMandatoryRequirements(unittest.TestCase):
    def test_explicit_unsupported_requirements_block_generic_titles(self):
        config = AgentConfig.load()
        profile = config.active_profiles(account="secondary")[0]
        filters = FilterEngine(profile.filters_for("recommended"))
        descriptions = [
            "Excellent proficiency in QA automation using RestAssured (Java).",
            "Minimum of 3 years of experience in Web Automation testing using Selenium with Java.",
            "Strong experience with\nJava and AWS.",
            "Mandatory Skills: Strong hands-on experience in\nSelenium with Java/JavaScript.",
            "Strong in Kubernetes and GKE.",
            "Required Skills Backend:\nC#, .NET    Core / ASP.NET Core.",
            "Cloud Infrastructure & Virtualization Engineer",
            "Strong programming skills in Java and automation frameworks.",
            "Mandatory knowledge of Kubernetes.",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                result = filters.evaluate_detail(Job(
                    job_id="audit", title="Software Engineer", company="Example",
                    url="https://example.com/job", description=description,
                ))
                self.assertFalse(result.passed)
                self.assertEqual(result.reason, SkipReason.FILTER_DESCRIPTION)
        for description in (
            "Strong experience with Java or Python.",
            "Automation testing backend services in Java or Python and JavaScript or TypeScript.",
            "Python is a must-have. Java, or an equivalent language is an advantage.",
        ):
            self.assertTrue(filters.evaluate_detail(Job(
                job_id="audit", title="Software Engineer", company="Example",
                url="https://example.com/job", description=description,
            )).passed)
