import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_agent.config import AgentConfig
from naukri_agent.core.answers import AnswerEngine, ExperienceAnswers
from naukri_agent.core.filters import FilterEngine
from naukri_agent.core.models import Job, ScreeningQuestion, SkipReason
from naukri_agent.core.orchestrator import Orchestrator
from naukri_agent.notify.notifier import header_prefix
from naukri_agent.naukri.apply import ApplyEngine
from naukri_agent.naukri.search import JobSearcher


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


class TestNaukriExperienceSearch(unittest.IsolatedAsyncioTestCase):
    async def test_verified_search_deduplicates_and_rejects_a_lost_filter(self):
        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(ok=True))
        slider = MagicMock(count=AsyncMock(return_value=1), wait_for=AsyncMock())
        cards = MagicMock(count=AsyncMock(return_value=2))
        cards.first.wait_for = AsyncMock()
        duplicate = MagicMock(get_attribute=AsyncMock(return_value="123456789012"))
        new = MagicMock(get_attribute=AsyncMock(return_value="123456789013"))
        new.locator.return_value.first.count = AsyncMock(return_value=1)
        cards.all = AsyncMock(return_value=[duplicate, new])
        page.locator.side_effect = lambda selector: cards if "srp-jobtuple" in selector else slider
        searcher = JobSearcher(page, 0, 0)
        parsed = Job("reco-123456789013", "Python Developer", "Example",
                     "https://www.naukri.com/job-listings-123456789013")
        searcher._parse_card = AsyncMock(return_value=parsed)
        page.url = "https://www.naukri.com/python-developer-jobs?experience=3"
        jobs = await searcher.search_with_experience(["python developer"], 3, {"reco-123456789012"})
        self.assertEqual(jobs, [parsed])
        self.assertEqual(searcher._parse_card.await_count, 1)

        page.url = "https://www.naukri.com/python-developer-jobs"
        jobs = await searcher.search_with_experience(["python developer"], 3, set())
        self.assertEqual(jobs, [])
        self.assertEqual(searcher._parse_card.await_count, 1)


class TestArchivedMandatoryRequirements(unittest.TestCase):
    def test_run721_specialist_requirements_block_but_python_sql_analytics_remains(self):
        profile = AgentConfig.load().active_profiles(account="primary")[0]
        filters = FilterEngine(profile.filters_for("recommended"))
        job = Job("audit", "Data Analyst", "Example", "https://example.com/job")
        for description in (
            "Candidates must have hands-on experience with complex financial datasets.",
            "We require strong expertise in Advanced Excel and Power BI.",
            "Job Requirements: Proficiency in data visualization tools such as Tableau or Power BI.",
            "Responsibilities: Design, train, and evaluate machine learning and deep learning models.",
        ):
            with self.subTest(description=description):
                job.description = description
                self.assertEqual(filters.evaluate_detail(job).reason, SkipReason.FILTER_DESCRIPTION)
        job.description = "Analyze data in Python and SQL. Tableau or Power BI is a plus."
        self.assertTrue(filters.evaluate_detail(job).passed)

    def test_naukri_ambiguous_tester_uses_the_description(self):
        profile = AgentConfig.load().active_profiles(account="primary")[0]
        filters = FilterEngine(profile.filters_for("recommended"), profile.to_candidate_profile())
        for title in ("Tester", "Testing Engineer"):
            with self.subTest(title=title):
                job = Job("audit", title, "Example", "https://example.com/job")
                self.assertTrue(filters.evaluate_card(job).passed)
                job.description = "Perform electrical testing and implement automated testing scripts."
                self.assertEqual(filters.evaluate_detail(job).reason, SkipReason.FILTER_DESCRIPTION)
                job.description = "Software testing of SaaS products with Postman and Playwright."
                self.assertTrue(filters.evaluate_detail(job).passed)

    def test_aiml_alias_still_enforces_experience_bounds(self):
        profile = AgentConfig.load().active_profiles(account="primary")[0]
        filters = FilterEngine(profile.filters_for("recommended"), profile.to_candidate_profile())
        job = Job("audit", "AIML Engineer", "Example", "https://example.com/job",
                  min_experience=2, max_experience=3)
        self.assertTrue(filters.evaluate_card(job).passed)
        job.max_experience = 11
        self.assertEqual(filters.evaluate_card(job).reason, SkipReason.FILTER_EXPERIENCE)
        job.max_experience = 10
        job.description = "Strong proficiency in Python with expertise in machine learning libraries such as scikit-learn or TensorFlow."
        self.assertEqual(filters.evaluate_detail(job).reason, SkipReason.FILTER_DESCRIPTION)

    def test_water_consultancy_does_not_match_software_qa(self):
        config = AgentConfig.load()
        for account in ("primary", "secondary"):
            with self.subTest(account=account):
                profile = config.active_profiles(account=account)[0]
                filters = FilterEngine(profile.filters_for("recommended"))
                job = Job(
                    job_id="reco-051126926708", title="Water Quality Analyst/Modeller",
                    company="AtkinsRéalis", url="https://www.naukri.com/job-listings-051126926708",
                    min_experience=2, max_experience=4,
                )
                self.assertEqual(filters.evaluate_card(job).reason, SkipReason.FILTER_TITLE)

                job.title = "Quality Analyst"
                self.assertTrue(filters.evaluate_card(job).passed)
                job.description = "Degree in Civil Engineering with 2 to 4 years of experience in the water industry. Python is a plus."
                self.assertEqual(filters.evaluate_detail(job).reason, SkipReason.FILTER_DESCRIPTION)

                job.description = "Automate browser tests with Playwright for our water quality analytics software."
                self.assertTrue(filters.evaluate_detail(job).passed)

    def test_primary_detail_catches_run716_requirements_hidden_by_generic_titles(self):
        profile = AgentConfig.load().active_profiles(account="primary")[0]
        filters = FilterEngine(profile.filters_for("recommended"))
        cases = [
            ("Data Analyst", "The Lead Data Analyst will remain hands-on while mentoring analysts."),
            ("Data Engineer", "Analytical Databases: Hands-on experience with ClickHouse."),
        ]
        for title, description in cases:
            with self.subTest(title=title):
                job = Job("audit", title, "Example", "https://example.com/job")
                self.assertTrue(filters.evaluate_card(job).passed)
                job.description = description
                self.assertEqual(filters.evaluate_detail(job).reason, SkipReason.FILTER_DESCRIPTION)

                job.description = "Build SQL and Python workflows. ClickHouse familiarity is a plus."
                self.assertTrue(filters.evaluate_detail(job).passed)

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
