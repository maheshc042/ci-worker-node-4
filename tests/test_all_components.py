"""
Unit tests for core components, filter engine, LinkedIn platform, and answers.
"""
import unittest
from unittest.mock import MagicMock

from naukri_agent.config import AgentConfig
from naukri_agent.core.filters import FilterEngine
from naukri_agent.core.models import Job
from naukri_agent.naukri.chatbot import ChatbotHandler
from naukri_agent.naukri.parser import parse_salary_lpa
from naukri_agent.platforms.linkedin import (
    LinkedInPlatform,
)


class TestNaukriSalaryParser(unittest.TestCase):
    def test_documented_formats(self):
        self.assertEqual(parse_salary_lpa("12-18 Lacs PA"), (12.0, 18.0))
        self.assertEqual(parse_salary_lpa("Rs 8,00,000 - 12,00,000 PA"), (8.0, 12.0))
        self.assertEqual(parse_salary_lpa("50,000 - 1,00,000 P.A. monthly"), (6.0, 12.0))
        self.assertEqual(parse_salary_lpa("Not disclosed"), (None, None))
        self.assertEqual(parse_salary_lpa("7 LPA"), (7.0, None))

    def test_hourly_rates_fail_open(self):
        """Hourly rates are never annual salaries: None flows through the
        salary gate instead of a phantom 500 LPA pass."""
        self.assertEqual(parse_salary_lpa("Rs 500 per hour"), (None, None))
        self.assertEqual(parse_salary_lpa("1000/hour"), (None, None))

    def test_markerless_figures_fail_open(self):
        """Bonus/shares/equity fragments and bare ranges must not invent lakhs:
        sub-5 figures once wrong-rejected good jobs, large ones phantom-passed."""
        self.assertEqual(parse_salary_lpa("500 bonus"), (None, None))
        self.assertEqual(parse_salary_lpa("0.5% equity"), (None, None))
        self.assertEqual(parse_salary_lpa("6-10"), (None, None))
        self.assertEqual(parse_salary_lpa("2-5 years experience"), (None, None))
        self.assertEqual(parse_salary_lpa("10,000 joining bonus"), (None, None))


class TestPresubmitButtonGone(unittest.IsolatedAsyncioTestCase):
    def _engine(self):
        from unittest.mock import MagicMock

        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.naukri.apply import ApplyEngine

        page = MagicMock()
        page.on = MagicMock()
        return ApplyEngine(
            page,
            MagicMock(),
            MagicMock(),
            policy=RunPolicy(dry_run=True, side_effects_enabled=False),
        )

    async def test_gone_when_no_button(self):
        from unittest.mock import AsyncMock, patch

        from naukri_agent.naukri import apply as apply_mod

        engine = self._engine()
        with patch.object(apply_mod, "first_visible", new=AsyncMock(return_value=None)):
            self.assertTrue(await engine._presubmit_button_gone())

    async def test_present_when_button_lingers(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.naukri import apply as apply_mod

        engine = self._engine()
        with patch.object(
            apply_mod, "first_visible", new=AsyncMock(return_value=MagicMock())
        ):
            self.assertFalse(await engine._presubmit_button_gone())

    async def test_exception_fails_closed(self):
        from unittest.mock import AsyncMock, patch

        from naukri_agent.naukri import apply as apply_mod

        engine = self._engine()
        with patch.object(
            apply_mod,
            "first_visible",
            new=AsyncMock(side_effect=RuntimeError("detached")),
        ):
            self.assertFalse(await engine._presubmit_button_gone())


class TestJdExperienceExtractor(unittest.TestCase):
    def test_bands(self):
        from naukri_agent.naukri.parser import extract_jd_experience as e

        self.assertEqual(
            e("We need 6-8 years of experience in Python.\nGood team."), (6.0, 8.0))
        self.assertEqual(e("Experience: 2-4 Yrs\nLocation: Bangalore"), (2.0, 4.0))
        self.assertEqual(e("5+ years of experience required"), (5.0, None))

    def test_traps(self):
        from naukri_agent.naukri.parser import extract_jd_experience as e

        # Establishment year is not tenure; headcount is not tenure.
        self.assertEqual(
            e("Year of establishment: 2019\nWe need 2 years experience"), (2.0, 2.0))
        self.assertEqual(e("We have 8-10 openings across teams"), (None, None))
        self.assertEqual(e("Great culture, free lunch"), (None, None))
        self.assertEqual(e(""), (None, None))

    def test_identical_high_bands_fail_open(self):
        from naukri_agent.naukri.parser import extract_jd_experience as e
        from naukri_agent.naukri.parser import parse_experience as p

        # "24-24" is headcount, never tenure; genuine bands intact.
        self.assertEqual(p("24-24"), (None, None))
        self.assertEqual(p("12-12"), (None, None))
        self.assertEqual(p("3-8 Yrs"), (3.0, 8.0))
        self.assertEqual(p("12 years"), (12.0, 12.0))
        self.assertEqual(
            e("We have 24-24 openings for experienced staff"), (None, None))


class TestSpelledOutSeniority(unittest.TestCase):
    def test_spelled_out_levels_read_senior(self):
        from naukri_agent.core.filters import _title_suggests_senior

        self.assertTrue(_title_suggests_senior("Software Development Engineer 4"))
        self.assertFalse(_title_suggests_senior("Software Development Engineer II - Fullstack"))
        self.assertFalse(_title_suggests_senior("Software Development Engineer in Test"))

    def test_unified_profiles_exclude_core_ml(self):
        cfg = AgentConfig.load()
        for meth in ("get_unified_instahyre_profile",
                     "get_unified_cutshort_profile",
                     "get_unified_wellfound_profile"):
            profile = getattr(cfg, meth)()
            engine = FilterEngine(profile.filters_for("recommended"))
            blocked = engine.evaluate_card(Job(
                job_id="t", title="ML Engineer", company="C",
                location="Bengaluru", url="http://test.com", platform="x"))
            wanted = engine.evaluate_card(Job(
                job_id="t", title="AI Engineer", company="C",
                location="Bengaluru", url="http://test.com", platform="x"))
            self.assertFalse(blocked.passed, meth)
            self.assertTrue(wanted.passed, meth)


class TestRoleHeadCheck(unittest.TestCase):
    def _decision(self, title):
        cfg = AgentConfig.load()
        ai_profile = next(p for p in cfg.profiles if "AI" in p.name)
        engine = FilterEngine(ai_profile.filters_for("recommended"))
        return engine.evaluate_card(Job(
            job_id="t", title=title, company="C", location="Bengaluru",
            url="http://test.com", platform="naukri"))

    def test_non_tech_head_with_garnish_blocked(self):
        for title in ["HR Analyst (Python)", "Operations Executive (SQL)",
                      "Marketing Specialist (Python)"]:
            self.assertFalse(self._decision(title).passed, title)

    def test_breadth_heads_untouched(self):
        # Breadth policy: analyst heads pass with or without garnish.
        for title in ["Business Analyst (Python)", "Data Analyst",
                      "AI Engineer", "AI Engineer (Python, Remote)"]:
            decision = self._decision(title)
            self.assertTrue(decision.passed, f"{title}: {decision.reason}")


class TestDetailStageRegate(unittest.TestCase):
    def _engine(self):
        cfg = AgentConfig.load()
        ai_profile = next(p for p in cfg.profiles if "AI" in p.name)
        return FilterEngine(
            ai_profile.filters_for("recommended"),
            candidate=ai_profile.to_candidate_profile(cfg))

    def _job(self, title, description, min_exp, max_exp):
        return Job(
            job_id="t", title=title, company="C", location="Bengaluru",
            url="http://test.com", platform="naukri", description=description,
            min_experience=min_exp, max_experience=max_exp,
            experience_imputed=False)

    def test_jd_senior_band_regates(self):
        # Card said junior, JD demands senior: detail stage must reject so
        # the slot is not spent on a guaranteed recruiter reject.
        job = self._job("Python Developer", "We need 6-8 years of experience.",
                        2.0, 4.0)
        job.min_experience, job.max_experience = 6.0, 8.0
        decision = FilterEngine._experience_verdict(
            self._engine(), job, job.title.lower())
        self.assertIsNotNone(decision)
        self.assertFalse(decision.passed)

    def test_unchanged_numbers_idempotent(self):
        # Same numbers as card phase: verdict must reproduce the card pass.
        engine = self._engine()
        job = self._job("Python Developer", "We need 2-4 years of experience.",
                        2.0, 4.0)
        self.assertTrue(engine.evaluate_card(job).passed)
        self.assertTrue(engine.evaluate_detail(job).passed)


class TestChatbotBlindExit(unittest.IsolatedAsyncioTestCase):
    async def test_drawer_open_no_question_sets_error(self):
        """Blind exit with the drawer still open must carry a diagnostic
        error, not a silent break that surfaces as generic 'no submission
        confirmation' downstream."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.naukri import chatbot as cb_mod

        page = MagicMock()
        handler = cb_mod.ChatbotHandler(
            page, MagicMock(), RunPolicy(dry_run=True, side_effects_enabled=False))
        handler._current_question = AsyncMock(return_value=None)
        handler.is_open = AsyncMock(return_value=True)
        with patch.object(cb_mod, "first_visible", new=AsyncMock(return_value=None)):
            with patch.object(cb_mod, "dismiss_overlays", new=AsyncMock()):
                result = await handler.run()
        self.assertFalse(result.completed)
        self.assertIsNotNone(result.error)
        self.assertIn("unknown widget", result.error)
        self.assertEqual(result.unanswered, [])


class TestFilterEngine(unittest.TestCase):
    def setUp(self):
        self.cfg = AgentConfig.load()
        self.fs_profile = next(p for p in self.cfg.profiles if "Full Stack" in p.name)
        self.ai_profile = next(p for p in self.cfg.profiles if "AI" in p.name)

    def test_title_hyphen_normalization(self):
        """Verify that titles with spaces around hyphens match target rules."""
        engine = FilterEngine(self.fs_profile.filters_for("recommended"))
        job = Job(
            job_id="test-1",
            title="Full - Stack Engineer",
            company="Test Corp",
            location="Bengaluru",
            url="http://test.com",
            platform="instahyre",
        )
        decision = engine.evaluate_card(job)
        self.assertTrue(decision.passed, f"Failed with reason: {decision.reason}")

    def test_software_development_engineer_equivalence(self):
        """Verify SDE 1 and SDE 2 titles pass filter gates."""
        engine = FilterEngine(self.fs_profile.filters_for("recommended"))
        for title in [
            "Software Development Engineer 1",
            "Software Development Engineer 2",
            "Software Development Engineer in Test",
            "Quality Engineer",
            "Implementation Engineer",
        ]:

            job = Job(
                job_id=f"test-{title}",
                title=title,
                company="Amazon",
                location="Bengaluru",
                url="http://test.com",
                platform="instahyre",
            )
            decision = engine.evaluate_card(job)
            self.assertTrue(decision.passed, f"{title} failed with reason: {decision.reason}")

    def test_ai_profile_breadth_policy(self):
        """Product policy (owner call): the AI profile is breadth-first for
        interviews — analyst/support families PASS (Python transfers, details
        get sorted at interview); core-ML titles are EXCLUDED to save daily
        window (recruiter would reject an applied-AI background anyway)."""
        engine = FilterEngine(self.ai_profile.filters_for("recommended"))
        for title in [
            "Data Analyst",
            "Technical Support Engineer",
            "Product Support Executive",
            "Customer Success Engineer",
        ]:
            job = Job(
                job_id=f"test-ai-{title}",
                title=title,
                company="Test Corp",
                location="Bengaluru",
                url="http://test.com",
                platform="naukri",
            )
            decision = engine.evaluate_card(job)
            self.assertTrue(decision.passed, f"{title!r} must pass the AI profile: {decision.reason}")
        for title in ["Machine Learning Engineer", "Senior Machine Learning Engineer"]:
            job = Job(
                job_id=f"test-ai-{title}",
                title=title,
                company="Test Corp",
                location="Bengaluru",
                url="http://test.com",
                platform="naukri",
            )
            decision = engine.evaluate_card(job)
            self.assertFalse(decision.passed, f"{title!r} must be excluded as core-ML")
        for title in ["AI Engineer", "Python Developer", "Data Engineer"]:
            job = Job(
                job_id=f"test-ai-{title}",
                title=title,
                company="Test Corp",
                location="Bengaluru",
                url="http://test.com",
                platform="naukri",
            )
            decision = engine.evaluate_card(job)
            self.assertTrue(decision.passed, f"{title!r} must still pass: {decision.reason}")

    def test_disjoint_specialization_blocks(self):
        """Verify that strictly non-relevant roles remain blocked."""
        engine = FilterEngine(self.fs_profile.filters_for("recommended"))
        for blocked_title in [
            "Data Scientist",
            "Fullstack Developer - .NET",
            "SDET II (ETL Testing)",
            "SDET - 1 (Java)",
            "Security Engineer I",
        ]:
            job = Job(
                job_id=f"test-blocked-{blocked_title}",
                title=blocked_title,
                company="Block Corp",
                location="Bengaluru",
                url="http://test.com",
                platform="instahyre",
            )
            decision = engine.evaluate_card(job)
            self.assertFalse(decision.passed, f"{blocked_title} should have been blocked!")

    def test_dotnet_family_blocks(self):
        """Dotnet-family frameworks (Blazor/Razor/ASP.NET) must hit the same
        block as '.NET' — run 377 applied to a Blazor senior role."""
        engine = FilterEngine(self.fs_profile.filters_for("recommended"))
        for blocked_title in [
            "Senior Software Engineer - Blazor",
            "Backend Developer (ASP.NET Core)",
            "Software Engineer - Razor Components",
        ]:
            job = Job(
                job_id=f"test-dotnet-{blocked_title}",
                title=blocked_title,
                company="Block Corp",
                location="Bengaluru",
                url="http://test.com",
                platform="naukri",
            )
            decision = engine.evaluate_card(job)
            self.assertFalse(decision.passed, f"{blocked_title} should have been blocked!")

    def test_universal_junior_family_cap(self):
        """QA/DevOps/support roles above 2y must fail on EVERY profile's rules.

        All platforms share FilterEngine, so the junior-family cap holds
        everywhere by construction (run 388: Manual QA applied only because
        its card carried no experience signal, never a rule gap).
        """
        for prof in (self.fs_profile, self.ai_profile):
            engine = FilterEngine(prof.filters_for("recommended"))
            job = Job(
                job_id="test-junior-cap",
                title="Manual QA Engineer",
                company="Test Corp",
                location="Bengaluru",
                url="http://test.com",
                platform="linkedin",
                min_experience=3.0,
                max_experience=5.0,
            )
            decision = engine.evaluate_card(job)
            self.assertFalse(
                decision.passed,
                f"{prof.name}: Manual QA at 3-5y must be rejected, got {decision.reason}",
            )

    def test_strategist_titles_blocked(self):
        """Strategy/advisory titles are not engineering even with an AI
        prefix (run 430 applied to an AI Activation Strategist)."""
        for prof in (self.fs_profile, self.ai_profile):
            engine = FilterEngine(prof.filters_for("recommended"))
            job = Job(
                job_id="test-strategist",
                title="AI Activation Strategist",
                company="Test Corp",
                location="Bengaluru",
                url="http://test.com",
                platform="naukri",
            )
            decision = engine.evaluate_card(job)
            self.assertFalse(
                decision.passed,
                f"{prof.name}: Strategist title must be rejected, got {decision.reason}",
            )


    def test_creative_roles_blocked(self):
        """Graphics/game/creator roles hire portfolios, not engineering tenure
        (run 391 applied to a 2d/3d senior role and an AI Artist posting)."""
        for prof in (self.fs_profile, self.ai_profile):
            engine = FilterEngine(prof.filters_for("recommended"))
            for blocked_title in [
                "Software Developer 2d/3d - Senior",
                "AI Artist",
                "Unity Developer",
            ]:
                job = Job(
                    job_id=f"test-creative-{blocked_title}",
                    title=blocked_title,
                    company="Block Corp",
                    location="Bengaluru",
                    url="http://test.com",
                    platform="naukri",
                )
                decision = engine.evaluate_card(job)
                self.assertFalse(
                    decision.passed,
                    f"{prof.name}: {blocked_title} should have been blocked!",
                )




class TestDescriptionMetadata(unittest.TestCase):
    def test_cf_cookie_strip_keeps_logins(self):
        """Poisoned Cloudflare cookies must be stripped on restore while
        site logins persist (runs 457/458 looped on a tainted session)."""
        from naukri_agent.browser.manager import _strip_cf_cookies

        state = {"cookies": [
            {"name": "cf_clearance"}, {"name": "__cf_bm"},
            {"name": "li_at"}, {"name": "JSESSIONID"},
        ]}
        out = _strip_cf_cookies(state)
        self.assertEqual(
            [c["name"] for c in out["cookies"]], ["li_at", "JSESSIONID"]
        )
        self.assertIsNone(_strip_cf_cookies(None))

    def test_generic_intake_inboxes_ignored(self):
        """hrintern/careers/jobs inboxes never convert; named recruiters pass."""
        from naukri_agent.core.models import extract_description_metadata

        links, emails = extract_description_metadata(
            "Apply now. Contact hrintern@corp.com or careers@corp.com or "
            "jobs@corp.com. Recruiter: pooja.roy@esolglobal.com"
        )
        self.assertEqual(emails, ["pooja.roy@esolglobal.com"])


class TestRunSummary(unittest.TestCase):
    def test_platform_only_breakdown_with_forensics(self):
        """Summary shows per-platform (never per-profile) lines carrying
        scraped + plan-rejected counts, so a 0/0/0 platform is self-explanatory."""
        from naukri_agent.core.models import RunStats
        from naukri_agent.notify.notifier import format_run_summary

        stats = RunStats()
        stats.bump("AI / Python Engineer", "scraped", 10, platform="naukri")
        stats.bump("AI / Python Engineer", "plan_rejected", 6, platform="naukri")
        stats.bump("AI / Python Engineer", "applied", 3, platform="naukri")
        body = format_run_summary(stats, duration_s=60, run_id=1)
        self.assertNotIn("PER-PROFILE", body)
        self.assertIn("PER-PLATFORM", body)
        self.assertIn("10 scraped", body)
        self.assertIn("6 plan-rejected", body)


class TestLinkedInPlatform(unittest.TestCase):
    def setUp(self):
        self.cfg = AgentConfig.load()
        self.platform = LinkedInPlatform(
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), config=self.cfg
        )

    def test_default_and_config_days(self):
        """LinkedIn platform should default to 1 day (24h freshness) from config."""
        self.assertEqual(self.platform.days, 1)

    def test_search_url_freshness_parameter(self):
        """Verify search URLs contain f_TPR=r86400 (1 day default) and f_TPR=r604800 (7 days)."""
        ai_prof = next(p for p in self.cfg.profiles if "AI" in p.name)
        fs_prof = next(p for p in self.cfg.profiles if "Full Stack" in p.name)

        url_1d = self.platform._get_search_url(ai_prof)
        self.assertIn("f_TPR=r86400", url_1d)
        self.assertIn("f_AL=true", url_1d)
        self.assertIn("f_E=2%2C3", url_1d)

        url_7d = self.platform._get_search_url(fs_prof, days=7)
        self.assertIn("f_TPR=r604800", url_7d)

    def test_suitability_requires_stack_overlap(self):
        """A bare 'Software Engineer' title with zero candidate-stack overlap
        must be rejected even though the role name matches (run 377 filler)."""
        from naukri_agent.core.models import Job

        bare = Job(
            job_id="test-bare-swe",
            title="Software Engineer",
            company="Generic Corp",
            url="https://www.linkedin.com/jobs/view/1/",
            location="India (Remote)",
            description="We are looking for a great engineer to join our team.",
            platform="linkedin",
        )
        suitable, _, _ = self.platform.evaluate_job_suitability(bare)
        self.assertFalse(suitable, "stack-empty generic title should be rejected")

        stacked = Job(
            job_id="test-stacked-swe",
            title="Software Engineer",
            company="Stack Corp",
            url="https://www.linkedin.com/jobs/view/2/",
            location="India (Remote)",
            description="Build backend services with Python, FastAPI and PostgreSQL. REST APIs on AWS.",
            platform="linkedin",
        )
        suitable, _, score = self.platform.evaluate_job_suitability(stacked)
        self.assertTrue(suitable, "stack-matching title should pass")
        self.assertGreaterEqual(score, 60)


    def test_suitability_punctuation_and_substring(self):
        """Punctuation terms (c#, .net) must block; short acronyms (rag,
        mean) must not substring-match ordinary words (audit fixes)."""
        from naukri_agent.core.models import Job as _Job
        from naukri_agent.platforms.linkedin import _word_hit

        self.assertTrue(_word_hit("c#", "c# developer"))
        self.assertTrue(_word_hit(".net", ".net developer"))
        self.assertTrue(_word_hit(".net", "a .net engineer"))
        self.assertFalse(_word_hit("rag", "cloud storage solutions"))
        self.assertFalse(_word_hit("mean", "by all means"))
        self.assertFalse(_word_hit("java", "javascript developer"))

        blocked = [
            ("C# Developer", "Block Corp"),
            (".NET Developer", "Block Corp"),
            ("Backend Developer (C#, .NET)", "Block Corp"),
        ]
        for title, company in blocked:
            job = _Job(
                job_id="t-%s" % title[:8], title=title, company=company,
                location="Bengaluru", url="http://t", platform="linkedin",
            )
            suitable, _, _ = self.platform.evaluate_job_suitability(job)
            self.assertFalse(suitable, "%s should be blocked" % title)

        # Substring-only overlap must not rescue a generic title. ("cloud
        # storage" would still match the genuine "cloud" DevOps skill, so
        # use a JD with zero candidate-stack words at all.)
        vague = _Job(
            job_id="t-vague", title="Software Engineer", company="Box Corp",
            location="India (Remote)",
            description="We offer secure document management solutions for teams.",
            url="http://t", platform="linkedin",
        )
        suitable, _, _ = self.platform.evaluate_job_suitability(vague)
        self.assertFalse(suitable, "stack-empty JD must not pass")

    def test_track_queries_have_and_clause(self):
        """Every LinkedIn query variant carries a tech AND clause so no
        profile run scrapes unfiltered generic vacancies."""
        from naukri_agent.platforms.linkedin import (
            AI_TARGET_QUERY,
            FULL_TARGET_QUERY,
            FULLSTACK_TARGET_QUERY,
        )

        for q in (FULL_TARGET_QUERY, AI_TARGET_QUERY, FULLSTACK_TARGET_QUERY):
            self.assertIn(") AND (", q)
class TestNaukriChatbotFuzzyMatching(unittest.TestCase):
    def test_match_option_text_affirmative(self):
        """Affirmative choices should match 'yes', 'agree', 'willing'."""
        self.assertTrue(ChatbotHandler._match_option_text("Yes, I am willing to relocate", "yes"))
        self.assertTrue(ChatbotHandler._match_option_text("Comfortable with Bengaluru", "yes"))
        self.assertFalse(ChatbotHandler._match_option_text("No, not interested", "yes"))

    def test_match_option_text_city(self):
        """City options should match Bengaluru / Bangalore fuzzy."""
        self.assertTrue(ChatbotHandler._match_option_text("Bengaluru / Bangalore", "Bengaluru"))
        self.assertTrue(ChatbotHandler._match_option_text("Bangalore / Bengaluru", "Bengaluru"))
        self.assertFalse(ChatbotHandler._match_option_text("Delhi / NCR", "Bengaluru"))


class TestInstahyreUnifiedConfiguration(unittest.TestCase):
    def setUp(self):
        self.cfg = AgentConfig.load()
        self.unified_profile = self.cfg.get_unified_instahyre_profile()
        self.engine = FilterEngine(self.unified_profile.filters)

    def test_instahyre_search_skills_and_exp(self):
        """Instahyre configuration must contain the exact 15 skills and 2 years experience."""
        expected_skills = [
            "SDET", "Python", "Node.js", "React.js", "TypeScript",
            "FastAPI", "Next.js", "Generative AI", "JavaScript",
            "LangChain", "LangGraph", "MLOps", "AWS Bedrock",
            "API Testing", "Quality Assurance",
        ]
        self.assertEqual(self.cfg.instahyre.skills, expected_skills)
        self.assertEqual(self.cfg.instahyre.experience_years, 2)

    def test_unified_profile_accepts_both_ai_and_fullstack(self):
        """Unified Instahyre profile must accept both AI and Full Stack roles without cross-profile rejection."""
        roles = [
            ("KuKu FM", "AI Engineer"),
            ("Swiggy", "React.js Developer"),
            ("Impact Analytics", "Node.js Developer"),
            ("Tranzact", "Full Stack Developer - Python / Django / React.js"),
            ("Amazon", "Software Development Engineer 2"),
            ("Quince", "SDET II"),
            ("Gravity", "Lead React/Next.js Developer"),
        ]
        for company, title in roles:
            job = Job(
                job_id=f"test-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://www.instahyre.com/candidate/opportunities/",
                platform="instahyre",
            )
            decision = self.engine.evaluate_card(job)
            self.assertTrue(decision.passed, f"Job {company} - {title} failed: {decision.reason}")

    def test_unified_profile_rejects_forbidden_roles(self):
        """Unified Instahyre profile must reject Java, .NET, Manager, Architect (lead is allowed)."""
        forbidden_roles = [
            ("Oracle", "Java Developer"),
            ("Microsoft", ".NET Backend Engineer"),
            ("Infosys", "Senior Architect"),
            ("Accenture", "Engineering Manager"),
            ("TCS", "BPO Process Associate"),
        ]
        for company, title in forbidden_roles:
            job = Job(
                job_id=f"test-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://www.instahyre.com/candidate/opportunities/",
                platform="instahyre",
            )
            decision = self.engine.evaluate_card(job)
            self.assertFalse(decision.passed, f"Job {company} - {title} should have been rejected!")

    def test_unified_profile_allows_devops_cloud_support_desktop(self):
        """Unified Instahyre profile must allow DevOps, Cloud, Support, and Desktop Support roles."""
        allowed_roles = [
            ("Deutsche Bank", "DevOps Engineer"),
            ("Razorpay", "Cloud Engineer"),
            ("Manifest", "Technical Support Engineer"),
            ("Zendesk", "Support Engineer"),
            ("Wipro", "Desktop Support Engineer"),
        ]
        for company, title in allowed_roles:
            job = Job(
                job_id=f"test-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://www.instahyre.com/candidate/opportunities/",
                platform="instahyre",
            )
            decision = self.engine.evaluate_card(job)
            self.assertTrue(decision.passed, f"Job {company} - {title} failed: {decision.reason}")

    def test_unified_profile_rejects_mobile_sre_phd(self):
        """Unified Instahyre profile must reject Mobile (React Native/Android/iOS/Flutter), pure SRE, and PhD."""
        forbidden_roles = [
            ("Bhanzu", "App Developer - React Native"),
            ("PhonePe", "Android Developer"),
            ("CRED", "iOS Engineer"),
            ("Groww", "Flutter Developer"),
            ("Uber", "Site Reliability Engineer"),
            ("Google", "SRE"),
            ("Microsoft Research", "PhD Research Scientist"),
        ]
        for company, title in forbidden_roles:
            job = Job(
                job_id=f"test-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://www.instahyre.com/candidate/opportunities/",
                platform="instahyre",
            )
            decision = self.engine.evaluate_card(job)
            self.assertFalse(decision.passed, f"Job {company} - {title} should have been rejected!")


class TestHiringCafeUnifiedConfiguration(unittest.TestCase):
    def setUp(self):
        self.cfg = AgentConfig.load()
        self.unified_profile = self.cfg.get_unified_hiringcafe_profile()

    def test_single_unified_profile(self):
        """One profile per run: per-profile passes would re-scrape the same
        login-free feed N times for zero additional coverage."""
        p = self.unified_profile
        self.assertEqual(p.name, "HiringCafe Unified (AI & Full Stack)")
        self.assertEqual(p.account, "primary")
        self.assertTrue(p.use_recommended)
        self.assertGreater(p.platform_limits.get("hiringcafe", 0), 0)
        self.assertEqual(p.filters.require_easy_apply, False)
        self.assertEqual(p.filters.max_posted_days, 3)

    def test_search_terms_persona_balanced(self):
        """Interleaved title keywords keep the platform's [:8] search-term
        slice balanced across AI and Full-Stack personas."""
        terms = self.unified_profile.title_keywords[:8]
        ai_markers = ("ai", "python", "llm", "genai", "ml")
        fs_markers = ("full stack", "fullstack", "react", "node", "mern", "sdet", "qa")
        blob = " | ".join(terms).lower()
        self.assertTrue(any(m in blob for m in ai_markers), terms)
        self.assertTrue(any(m in blob for m in fs_markers), terms)
        # The platform consumes the same keywords with an 8-term slice.
        from naukri_agent.platforms.hiringcafe import HiringCafePlatform

        used = HiringCafePlatform._search_terms(self.unified_profile)
        self.assertLessEqual(len(used), 8)
        self.assertGreater(len(used), 0)


class TestCutshortUnifiedConfiguration(unittest.TestCase):
    def setUp(self):
        self.cfg = AgentConfig.load()
        self.unified_profile = self.cfg.get_unified_cutshort_profile()
        self.engine = FilterEngine(self.unified_profile.filters)

    def test_cutshort_skills_constants(self):
        """Verify Cutshort divided skills lists contain verified autocomplete targets."""
        from naukri_agent.platforms.cutshort import (
            CUTSHORT_AI_SKILLS,
            CUTSHORT_FULLSTACK_SKILLS,
            CUTSHORT_UNIFIED_SKILLS,
        )
        # AI Skills
        ai_exact = [s[1] if isinstance(s, tuple) else s for s in CUTSHORT_AI_SKILLS]
        for expected in [
            "Python", "Generative AI", "Agentic AI", "Large Language Models (LLM)",
            "Retrieval Augmented Generation (RAG)", "Artificial Intelligence (AI)",
            "MLOps", "Large Language Models (LLM) tuning", "AWS Bedrock", "FastAPI",
            "LangGraph", "Prompt engineering", "NodeJS (Node.js)", "TypeScript", "Javascript"
        ]:
            self.assertIn(expected, ai_exact)

        # Full Stack Skills
        fs_exact = [s[1] if isinstance(s, tuple) else s for s in CUTSHORT_FULLSTACK_SKILLS]
        for expected in [
            "React.js", "NextJs (Next.js)", "Javascript", "NodeJS (Node.js)",
            "TypeScript", "Python", "FastAPI"
        ]:
            self.assertIn(expected, fs_exact)

        # Unified Skills
        unified_exact = [s[1] if isinstance(s, tuple) else s for s in CUTSHORT_UNIFIED_SKILLS]
        for expected in [
            "Python", "React.js", "NextJs (Next.js)", "Generative AI", "Javascript",
            "NodeJS (Node.js)", "Agentic AI", "Large Language Models (LLM)", "TypeScript",
            "FastAPI", "Retrieval Augmented Generation (RAG)", "Artificial Intelligence (AI)",
            "MLOps", "Large Language Models (LLM) tuning", "AWS Bedrock", "LangGraph",
            "Prompt engineering"
        ]:
            self.assertIn(expected, unified_exact)


    def test_cutshort_unified_accepts_roles(self):
        """Verify unified profile accepts both AI and Full Stack roles gathered on Cutshort."""
        roles = [
            ("Techno Wise", "Python Full Stack Developer"),
            ("Shuling technology", "Senior Node.js Backend Engineer"),
            ("ChicMic Studios", "React Js Developer"),
            ("BigThinkCode Technologies", "Junior Node.js Developer"),
            ("TopGrep Tech Private Limi", "FS MERN Developer"),
            ("CLOUDSUFI", "CloudSufi is Hiring! SSE-AI Full Stack"),
            ("IntelliSavvy", "Fullstack Developer"),
            ("Gravity Engineering Services Pvt Ltd", "Lead React/Next.js Developer"),
        ]
        for company, title in roles:
            job = Job(
                job_id=f"test-cutshort-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://cutshort.io/profile/all-jobs",
                platform="cutshort",
            )
            decision = self.engine.evaluate_card(job)
            self.assertTrue(decision.passed, f"Job {company} - {title} failed: {decision.reason}")

    def test_cutshort_unified_rejects_presales_and_forbidden(self):
        """Verify Cutshort unified profile rejects presales, pre-sales, bpo, java, .net."""
        forbidden = [
            ("SaaSify", "Pre-Sales Consultant"),
            ("InfraSys", "Presales Solution Specialist"),
            ("Wipro", "Java Developer"),
            ("TechM", ".NET Core Backend Developer"),
            ("Accenture", "Sales Manager"),
        ]
        for company, title in forbidden:
            job = Job(
                job_id=f"test-cutshort-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://cutshort.io/profile/all-jobs",
                platform="cutshort",
            )
            decision = self.engine.evaluate_card(job)
            self.assertFalse(decision.passed, f"Job {company} - {title} should have been rejected!")

    def test_dynamic_resume_selection_logic(self):
        """Verify resume selection maps AI jobs to AI CV and Full Stack to Full Stack CV."""
        from naukri_agent.config import PROJECT_ROOT

        ai_titles = [
            "AI Engineer",
            "Generative AI Specialist",
            "LLM Application Developer",
            "Agentic AI Engineer",
            "Python Backend / FastAPI Engineer",
        ]
        for title in ai_titles:
            text = f"{title}".lower()
            is_ai = any(k in text for k in ["ai", "llm", "genai", "gpt", "machine learning", "ml", "rag", "agent", "prompt", "nlp", "chatbot", "fastapi"])
            self.assertTrue(is_ai, f"{title} should be recognized as AI job")
            rel_path = "resumes/CV_Mahesh_Chitakoti_2026.pdf" if is_ai else "resumes/CV_Mahesh_Chitakoti_2026_1_.pdf"
            self.assertEqual(rel_path, "resumes/CV_Mahesh_Chitakoti_2026.pdf")
            self.assertTrue((PROJECT_ROOT / rel_path).exists())

        fs_titles = [
            "React Js Developer",
            "Fullstack Developer",
            "Node.js Backend Engineer",
            "Frontend Web Developer",
            "MERN Stack Developer",
        ]
        for title in fs_titles:
            text = f"{title}".lower()
            is_ai = any(k in text for k in ["ai", "llm", "genai", "gpt", "machine learning", "ml", "rag", "agent", "prompt", "nlp", "chatbot", "fastapi"])
            self.assertFalse(is_ai, f"{title} should be recognized as Full Stack job")
            rel_path = "resumes/CV_Mahesh_Chitakoti_2026.pdf" if is_ai else "resumes/CV_Mahesh_Chitakoti_2026_1_.pdf"
            self.assertEqual(rel_path, "resumes/CV_Mahesh_Chitakoti_2026_1_.pdf")
            self.assertTrue((PROJECT_ROOT / rel_path).exists())

    def test_linkedin_per_profile_queries(self):
        """No unified pass: each profile selects its own targeted track query
        (sequential per-profile runs are the default)."""
        from unittest.mock import MagicMock

        from naukri_agent.platforms.linkedin import LinkedInPlatform

        self.assertFalse(hasattr(self.cfg, "get_unified_linkedin_profile"))
        ai_prof = next(p for p in self.cfg.profiles if "AI" in p.name)
        fs_prof = next(p for p in self.cfg.profiles if "Full Stack" in p.name)

        platform = LinkedInPlatform(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), config=self.cfg)
        ai_url = platform._get_search_url(ai_prof)
        fs_url = platform._get_search_url(fs_prof)
        # AI track query must not contain Full Stack terms and vice versa.
        self.assertNotIn("Full%20Stack", ai_url)
        self.assertNotIn("AI%20Engineer", fs_url)
        self.assertIn("f_TPR=r86400", ai_url)
        self.assertIn("f_TPR=r86400", fs_url)

    def test_wellfound_unified_profile(self):
        """Verify Wellfound unified profile enforces 7-day freshness and combined filters."""
        from naukri_agent.platforms.wellfound import MAX_POSTED_DAYS, _parse_posted_days

        p = self.cfg.get_unified_wellfound_profile()
        self.assertEqual(p.name, "Wellfound Unified (AI & Full Stack)")
        self.assertEqual(p.account, "primary")
        expected_limit = max(
            (pp.platform_limits.get("wellfound", 50) for pp in self.cfg.profiles if pp.enabled),
            default=50,
        )
        self.assertEqual(p.platform_limits.get("wellfound"), expected_limit)
        self.assertEqual(p.filters.max_posted_days, 7)
        self.assertEqual(MAX_POSTED_DAYS, 7)

        # Freshness parsing checks
        self.assertEqual(_parse_posted_days("Posted 2d ago"), 2)
        self.assertEqual(_parse_posted_days("Active 1w ago"), 7)
        self.assertEqual(_parse_posted_days("just now"), 0)
        self.assertEqual(_parse_posted_days("Posted 2 weeks ago"), 14)
        self.assertGreater(_parse_posted_days("Posted 2 weeks ago"), MAX_POSTED_DAYS)


class TestGeminiBreaker(unittest.TestCase):
    def _http_503(self, *args, **kwargs):
        import urllib.error

        raise urllib.error.HTTPError(
            "https://generativelanguage.googleapis.com/", 503,
            "Service Unavailable", {}, None,
        )

    def test_three_503_trips_breaker(self):
        """Three straight 503s must trip the breaker: the 4th call returns
        None without touching the network (run 398 burned ~20s per job)."""
        from unittest.mock import patch

        from naukri_agent.core.gemini_writer import GeminiWriter

        writer = GeminiWriter(api_key="test-key", model="test-model")
        with patch("urllib.request.urlopen", side_effect=self._http_503) as mock_open:
            for _ in range(3):
                self.assertIsNone(writer.generate_email_body("Dev", "desc", "Co"))
            self.assertTrue(writer._model_broken)
            self.assertIsNone(writer.generate_email_body("Dev", "desc", "Co"))
            self.assertEqual(mock_open.call_count, 3)

    def test_success_resets_503_count(self):
        """A success between failures must reset the consecutive count."""
        import json
        from unittest.mock import patch

        from naukri_agent.core.gemini_writer import GeminiWriter

        body = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "Hello world pitch text here"}]}}]}
        ).encode()
        writer = GeminiWriter(api_key="test-key", model="test-model")
        with patch("urllib.request.urlopen", side_effect=self._http_503):
            writer.generate_email_body("Dev", "desc", "Co")
            writer.generate_email_body("Dev", "desc", "Co")
        self.assertEqual(writer._server_errors, 2)
        self.assertFalse(writer._model_broken)

        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        with patch("urllib.request.urlopen", return_value=resp):
            out = writer.generate_email_body("Dev", "desc", "Co")
        self.assertTrue(out)
        self.assertEqual(writer._server_errors, 0)


class TestMailerTrackRouting(unittest.TestCase):
    def test_ai_track_word_boundaries(self):
        """AI-track routing must use word boundaries: Retail/Training are
        full-stack roles, not AI roles. Gemini forced off so the template
        routing itself is pinned."""
        from unittest.mock import patch

        from naukri_agent.core.mailer import ColdEmailer

        mailer = ColdEmailer(sender_email="a@b.com", app_password="x")
        with patch(
            "naukri_agent.core.gemini_writer.GeminiWriter.generate_email_body",
            return_value=None,
        ):
            self.assertIn("Python, FastAPI", mailer._generate_body("AI Engineer", "", "Co"))
            self.assertIn("Python, FastAPI", mailer._generate_body("ML Engineer", "", "Co"))
            self.assertIn("React, Node.js", mailer._generate_body("Retail Associate", "", "Co"))
            self.assertIn("React, Node.js", mailer._generate_body("Training Manager", "", "Co"))
            self.assertIn("React, Node.js", mailer._generate_body("Full Stack Developer", "", "Co"))

    def test_backend_track_routing(self):
        """Generic software/backend titles get a Python-backend pitch, not a
        React-frontend one: SDE/Backend/Software roles carry no AI token."""
        from unittest.mock import patch

        from naukri_agent.core.mailer import ColdEmailer

        mailer = ColdEmailer(sender_email="a@b.com", app_password="x")
        with patch(
            "naukri_agent.core.gemini_writer.GeminiWriter.generate_email_body",
            return_value=None,
        ):
            for title in ("SDE", "SDE-II", "Software Engineer", "Backend Engineer"):
                body = mailer._generate_body(title, "", "Co")
                self.assertIn("Python, FastAPI, Django", body, title)
                self.assertIn("Backend Development", body, title)
                self.assertNotIn("React frontends", body, title)

    def test_handle_names_cleaned(self):
        """Alphanumeric poster handles still produce a human salutation."""
        from unittest.mock import patch

        from naukri_agent.core.gemini_writer import clean_first_name
        from naukri_agent.core.mailer import ColdEmailer

        self.assertEqual(clean_first_name("shubhampundir220"), "Shubhampundir")
        self.assertEqual(clean_first_name("Chandana Rao"), "Chandana")
        self.assertEqual(clean_first_name("anushka@innthink.com"), "Anushka")
        self.assertEqual(clean_first_name("anushka.sharma@innthink.com"), "Anushka")
        self.assertEqual(clean_first_name("hr@innthink.com"), "")
        self.assertEqual(clean_first_name("careers@innthink.com"), "")
        self.assertEqual(clean_first_name(""), "")
        self.assertEqual(clean_first_name("12345"), "")
        mailer = ColdEmailer(sender_email="a@b.com", app_password="x")
        with patch(
            "naukri_agent.core.gemini_writer.GeminiWriter.generate_email_body",
            return_value=None,
        ):
            body = mailer._generate_body(
                "AI Engineer", "", "", angle="referral", recipient_name="shubhampundir220")
            self.assertTrue(body.startswith("Hi Shubhampundir,"))

    def test_name_salutation_and_company(self):
        """Recipient name + company thread into the fallback template."""
        from unittest.mock import patch

        from naukri_agent.core.mailer import ColdEmailer

        mailer = ColdEmailer(sender_email="a@b.com", app_password="x")
        with patch(
            "naukri_agent.core.gemini_writer.GeminiWriter.generate_email_body",
            return_value=None,
        ):
            body = mailer._generate_body(
                "AI Engineer", "", "Acme", angle="referral", recipient_name="Chandana")
            self.assertTrue(body.startswith("Hi Chandana,"))
            anon = mailer._generate_body("AI Engineer", "", "", angle="referral")
            self.assertTrue(anon.startswith("Hi,"))
            self.assertNotIn("Hi there", anon)

    def test_subject_shapes(self):
        from naukri_agent.core.mailer import ColdEmailer

        build = ColdEmailer.build_subject
        self.assertEqual(
            build(role="AI Engineer", name="Mahesh Chitakoti", immediate=True),
            "AI Engineer application, Mahesh Chitakoti (immediate joiner)")
        self.assertEqual(
            build(role="AI Engineer", name="", immediate=False),
            "AI Engineer application")
        self.assertEqual(
            build(role="Software Engineer", name="Mahesh Chitakoti", immediate=True,
                  stack_summary="React / Node.js / TS", years="2.5 Yrs Exp"),
            "Application: Software Engineer – Mahesh Chitakoti (2.5 Yrs Exp | React / Node.js / TS | Immediate Joiner)")
        self.assertIn("Immediate Joiner", build(role="X", name="Z", immediate=True, referral=True))
        self.assertNotIn("Acme", build(role="AI Engineer", name="Mahesh Chitakoti"))

    def test_prompt_has_verified_inventory(self):
        import json
        from unittest.mock import patch

        from naukri_agent.core.gemini_writer import GeminiWriter

        captured = {}

        class FakeResp:
            status = 200

            def read(self):
                return json.dumps(
                    {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode())
            return FakeResp()

        writer = GeminiWriter(api_key="test-key", model="test-model")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            writer.generate_email_body("AI Engineer", "Build RAG pipelines", "Acme")
        prompt = captured["payload"]["contents"][0]["parts"][0]["text"]
        self.assertIn("Verified Skill Inventory", prompt)
        self.assertIn("never present a requirement", prompt.lower())

    def test_stub_body_falls_back(self):
        """A truncated model reply (<200 chars) must never send."""
        from unittest.mock import patch

        from naukri_agent.core.mailer import ColdEmailer

        mailer = ColdEmailer(sender_email="a@b.com", app_password="x")
        with patch(
            "naukri_agent.core.gemini_writer.GeminiWriter.generate_email_body",
            return_value="Hello, see attached",
        ):
            body = mailer._generate_body("AI Engineer", "", "Co")
            self.assertIn("Candidate Snapshot", body)

    def test_finalize_email_body_enforces_signoff_and_contact(self):
        """Even if Gemini truncates sign-off or breaks mobile line, finalize_email_body fixes it."""
        from naukri_agent.core.gemini_writer import finalize_email_body, applicant_snapshot

        raw_truncated = (
            "Hi Deepa,\n\n"
            "I am applying for the Full Stack Engineer role you posted, bringing 2.5 years of experience "
            "building production backend systems. My background spans algorithms, and secure business process workflows.\n\n"
            "Candidate Snapshot:\n"
            "• Mobile:\n"
            "+91 9481777227\n\n"
            "My resume is attached for review."
        )
        who = applicant_snapshot()
        out = finalize_email_body(raw_truncated, who)
        self.assertIn("Best regards,\n" + who.name, out)
        self.assertIn("GitHub: " + who.github, out)
        self.assertIn("LinkedIn: " + who.linkedin, out)
        self.assertIn("Contact: +91 9481777227", out)
        self.assertNotIn("algorithms, and secure business process workflows", out)


    def test_inbox_reply_detection(self):
        """Fake IMAP server: replies from contacted senders surface, own
        mail and strangers do not."""
        import email as email_pkg
        from unittest.mock import MagicMock, patch

        from naukri_agent.core.mailer import ColdEmailer

        def _raw(frm, subject, body):
            m = email_pkg.message.EmailMessage()
            m["From"] = frm
            m["Subject"] = subject
            m["Date"] = "Thu, 02 Oct 2026 10:00:00 +0000"
            m.set_content(body)
            return ("OK", [(b"1", m.as_bytes())])

        imap = MagicMock()
        imap.select.return_value = ("OK", [])
        imap.search.return_value = ("OK", [b"1"])
        imap.fetch.side_effect = [
            _raw("recruiter@acme.com", "Re: role", "Interested, let's talk"),
            _raw("maheshrwd042@gmail.com", "Re: role", "my own copy"),
            _raw("stranger@x.com", "Hi", "spam"),
        ]
        mailer = ColdEmailer(sender_email="maheshrwd042@gmail.com", app_password="x")
        with patch("imaplib.IMAP4_SSL", return_value=imap):
            replies, err = mailer.check_inbox_replies(
                ["recruiter@acme.com", "maheshrwd042@gmail.com", "quiet@acme.com"], 14)
        self.assertIsNone(err)
        self.assertEqual([r["from"] for r in replies], ["recruiter@acme.com"])

    def test_no_duplicate_contact_footer(self):
        """Mobile/location live in the snapshot bullets only — the signoff
        carries name + links, never a repeated contact line."""
        from unittest.mock import patch

        from naukri_agent.core.mailer import ColdEmailer

        mailer = ColdEmailer(sender_email="a@b.com", app_password="x")
        with patch(
            "naukri_agent.core.gemini_writer.GeminiWriter.generate_email_body",
            return_value=None,
        ):
            body = mailer._generate_body("AI Engineer", "", "Co")
            self.assertEqual(body.count("+91 9481777227"), 1)
            self.assertIn("Best regards,\nMahesh Chitakoti\n", body)

    def test_referral_angle_fallback(self):
        """Referral framing in the deterministic template (Gemini off):
        referral ask present, facts identical, no call pressure."""
        from unittest.mock import patch

        from naukri_agent.core.mailer import ColdEmailer

        mailer = ColdEmailer(sender_email="a@b.com", app_password="x")
        with patch(
            "naukri_agent.core.gemini_writer.GeminiWriter.generate_email_body",
            return_value=None,
        ):
            body = mailer._generate_body("AI Engineer", "", "Co", angle="referral")
            self.assertIn("referring me", body)
            self.assertIn("Python, FastAPI", body)
            self.assertNotIn("10-minute chat", body)
            plain = mailer._generate_body("AI Engineer", "", "Co")
            self.assertIn("10-minute chat", plain)
            self.assertNotIn("referring me", plain)


class TestGeminiPrompt(unittest.TestCase):
    def _prompt_text(self, role_name: str = "AI Engineer", **kwargs):
        import json
        from unittest.mock import patch

        from naukri_agent.core.gemini_writer import GeminiWriter

        captured = {}

        class FakeResp:
            status = 200

            def read(self):
                return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}
            ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode())
            return FakeResp()

        writer = GeminiWriter(api_key="test-key", model="test-model")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            writer.generate_email_body(role_name, "Build RAG pipelines", "Acme", **kwargs)
        parts = captured["payload"]["contents"][0]["parts"]
        return parts[0]["text"]

    def test_referral_prompt_shape(self):
        prompt = self._prompt_text(angle="referral", recipient_name="Chandana")
        self.assertIn("to ask for a referral", prompt)
        self.assertIn('Salutation: "Hi Chandana,"', prompt)
        self.assertIn("under 150 words", prompt)

    def test_application_prompt_shape(self):
        prompt = self._prompt_text()
        self.assertIn("direct job application", prompt)
        self.assertIn('Salutation: "Hi Acme Team,"', prompt)
        self.assertIn("under 150 words", prompt)
        self.assertNotIn("referral", prompt.split("RULES")[0])

    def test_strip_company_from_title(self):
        from naukri_agent.core.gemini_writer import strip_company_from_title

        self.assertEqual(
            strip_company_from_title("Fullstack AI/GenAI Engineer – DharmikVibes", "DharmikVibes"),
            "Fullstack AI/GenAI Engineer",
        )
        self.assertEqual(
            strip_company_from_title("Fullstack AI/GenAI Engineer – DharmikVibes", "Dharmik Vibes"),
            "Fullstack AI/GenAI Engineer",
        )
        self.assertEqual(
            strip_company_from_title("AI Engineer at Acme Corp", "Acme Corp"),
            "AI Engineer",
        )
        self.assertEqual(
            strip_company_from_title("Backend Developer | TechCorp", "TechCorp"),
            "Backend Developer",
        )
        self.assertEqual(
            strip_company_from_title("Python Developer - Remote", ""),
            "Python Developer - Remote",
        )

    def test_wellfound_pitch_prompt_shape(self):
        import json
        from unittest.mock import patch
        from naukri_agent.core.gemini_writer import GeminiWriter

        captured = {}

        class FakeResp:
            status = 200

            def read(self):
                return json.dumps(
                    {"candidates": [{"content": {"parts": [{"text": "High impact pitch"}]}}]}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode())
            return FakeResp()

        writer = GeminiWriter(api_key="test-key", model="test-model")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            writer.generate_wellfound_pitch(
                role_name="Fullstack AI/GenAI Engineer – DharmikVibes",
                job_description="Build spiritual tech platform using Python, APIs, and AWS",
                company_name="DharmikVibes",
            )
        prompt = captured["payload"]["contents"][0]["parts"][0]["text"]
        self.assertIn("Wellfound", prompt)
        self.assertIn('Salutation: "Hi DharmikVibes Team,"', prompt)
        self.assertIn('"Fullstack AI/GenAI Engineer"', prompt)
        self.assertNotIn("DharmikVibes role", prompt)
        self.assertNotIn("3.9 LPA", prompt)
        self.assertNotIn("7 LPA", prompt)
        self.assertNotIn("9481777227", prompt)
        self.assertNotIn("Mobile / WhatsApp", prompt)
        self.assertNotIn("Quick Candidate Snapshot (clean bullet points", prompt)
        self.assertIn("Production AI Pipelines", prompt)
        self.assertIn("10-minute chat", prompt)

    def test_ml_role_prompt_shape(self):
        prompt = self._prompt_text(role_name="Machine Learning Engineer", recipient_name="Mohammed")
        self.assertIn("Machine Learning & AI Engineering", prompt)
        self.assertIn("Python, PyTorch, LangChain, Vector Search, FastAPI, AWS, Docker", prompt)
        self.assertIn("DO NOT mention React or frontend UI development", prompt)
        self.assertIn('Salutation: "Hi Mohammed,"', prompt)

    def test_recipient_resolution_prioritizes_mailbox_owner(self):
        from naukri_agent.linkedin.analyzer import resolve_recipient_first_name

        self.assertEqual(
            resolve_recipient_first_name("Ali Noumaan", "mohammed.noumaan@vertage.com"),
            "Mohammed",
        )
        self.assertEqual(
            resolve_recipient_first_name("Anushka Sharma", "anushka@innthink.com"),
            "Anushka",
        )
        self.assertEqual(
            resolve_recipient_first_name("Deepa HR", "hr@company.com"),
            "Deepa",
        )

    def test_finalize_email_body_fixes_broken_contact_and_hallucinated_email(self):
        from naukri_agent.core.gemini_writer import finalize_email_body, applicant_snapshot

        who = applicant_snapshot()
        raw = """Hi Mohammed,

I am writing regarding the Machine Learning Engineer role. Over the past 2.5 years, I have specialized in building production software end-to-end—developing responsive user interfaces in React and scalable backend services using Python, APIs, and cloud infrastructure.

Candidate Snapshot:
- Total Experience: 2.5 years (AI & Backend Development)
- Primary Stack: Python, APIs, AWS, Azure, Backend Development
- Current Location: Bengaluru, India
- Notice Period: Immediate (0 days)
- Current CTC: ₹3.9 LPA
- Expected CTC: ₹7 LPA (Negotiable)
- Contact: 
+91 9481777227 | maheshchitakoti@gmail.com

My resume is attached for your review.

Best regards,
Mahesh Chitakoti
"""
        finalized = finalize_email_body(raw, who=who, role_name="Machine Learning Engineer")
        self.assertIn("Machine Learning & AI Engineering", finalized)
        self.assertIn("PyTorch", finalized)
        self.assertNotIn("responsive user interfaces in React", finalized)
        self.assertIn("building and deploying production AI systems", finalized)
        self.assertNotIn("maheshchitakoti@gmail.com", finalized)
        self.assertIn("maheshrwd042@gmail.com", finalized)
        self.assertNotIn("Contact: \n", finalized)
        self.assertIn("- Contact: +91 9481777227 | maheshrwd042@gmail.com", finalized)
        self.assertIn("GitHub: https://github.com/maheshchichkoti", finalized)
        self.assertIn("LinkedIn: https://www.linkedin.com/in/maheshchitakoti", finalized)

    def test_ml_subject_and_body_in_mailer(self):
        from naukri_agent.core.mailer import ColdEmailer

        sub = ColdEmailer.build_subject(
            role="Machine Learning Engineer",
            name="Mahesh Chitakoti",
            immediate=True,
            stack_summary="Python / ML / GenAI / AWS",
            years="2.5 Yrs Exp",
        )
        self.assertEqual(
            sub,
            "Application: Machine Learning Engineer – Mahesh Chitakoti (2.5 Yrs Exp | Python / ML / GenAI / AWS | Immediate Joiner)",
        )


class TestVerifyPostSubmit(unittest.IsolatedAsyncioTestCase):
    """Runs 488/490: post-click confirmation missed on a stale drawer DOM.
    A fresh reload + marker re-check must recover applied/already states —
    and never invent success from an empty page."""

    def _engine(self, page):
        from unittest.mock import MagicMock

        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.naukri.apply import ApplyEngine

        artifacts = MagicMock()
        policy = RunPolicy(dry_run=False, side_effects_enabled=True)
        engine = ApplyEngine(page, MagicMock(), artifacts, policy=policy)
        return engine

    def _job(self):
        from naukri_agent.core.models import Job

        return Job(job_id="reco-1", title="SDE", company="Acme",
                   url="https://www.naukri.com/job-listings-1")

    async def test_recovers_applied_marker(self):
        from unittest.mock import AsyncMock, patch

        from naukri_agent.naukri import apply as apply_mod

        page = MagicMock()
        page.goto = AsyncMock()
        page.title = AsyncMock(return_value="Software Engineer")
        page.url = "https://www.naukri.com/job-listings-1"
        engine = self._engine(page)
        with patch.object(apply_mod, "first_visible", new=AsyncMock(return_value=object())):
            self.assertEqual(await engine._verify_post_submit(self._job()), "applied")

    async def test_empty_page_stays_unknown(self):
        from unittest.mock import AsyncMock, patch

        from naukri_agent.naukri import apply as apply_mod

        page = MagicMock()
        page.goto = AsyncMock()
        page.title = AsyncMock(return_value="Software Engineer")
        page.url = "https://www.naukri.com/job-listings-1"
        engine = self._engine(page)
        with patch.object(apply_mod, "first_visible", new=AsyncMock(return_value=None)):
            self.assertEqual(await engine._verify_post_submit(self._job()), "unknown")


class TestLateDrawerRecovery(unittest.IsolatedAsyncioTestCase):
    """A chatbot drawer hydrating AFTER the 15s post-click poll must still be
    answered instead of recording 'No success confirmation within SLA'."""

    def _engine(self, page):
        from unittest.mock import MagicMock

        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.naukri.apply import ApplyEngine

        return ApplyEngine(page, MagicMock(), MagicMock(),
                           policy=RunPolicy(dry_run=False, side_effects_enabled=True))

    def _page(self, btn):
        from unittest.mock import AsyncMock, MagicMock

        meta = MagicMock()
        meta.count = AsyncMock(return_value=0)
        page = MagicMock()
        page.url = "https://www.naukri.com/job-listings-1"
        page.title = AsyncMock(return_value="Software Engineer")
        page.locator = MagicMock(side_effect=lambda sel: (
            btn if sel == "button#apply-button" else meta
        ))
        return page

    def _btn(self):
        from unittest.mock import AsyncMock

        btn = AsyncMock()
        btn.is_visible = AsyncMock(return_value=True)
        btn.scroll_into_view_if_needed = AsyncMock()
        btn.click = AsyncMock()
        btn.evaluate = AsyncMock()
        return btn

    def _completed_result(self):
        from unittest.mock import MagicMock

        res = MagicMock()
        res.unfit = None
        res.unanswered = []
        res.error = None
        res.completed = True
        res.answered = 2
        return res

    def _job(self):
        from naukri_agent.core.models import Job

        return Job(job_id="reco-9", title="SDE", company="Acme",
                   url="https://www.naukri.com/job-listings-1")

    async def _submit_with_drawer(self, drawer_on_first_check):
        from unittest.mock import AsyncMock, patch

        from naukri_agent.naukri import apply as apply_mod

        btn = self._btn()
        page = self._page(btn)
        page.locator.return_value.all = AsyncMock(return_value=[])
        engine = self._engine(page)
        drawer_calls = []

        async def fast_check(sels):
            if sels == apply_mod.S.CHATBOT_DRAWER:
                drawer_calls.append(1)
                # First poll sees no drawer; the late check sees it.
                return drawer_on_first_check if len(drawer_calls) == 1 else True
            return False

        completed = self._completed_result()
        fake_bot = AsyncMock()
        fake_bot.run = AsyncMock(return_value=completed)

        async def no_markers(scope, sels, timeout_ms=6000):
            sels = list(sels)
            if sels and sels[0] == "button#apply-button":
                return btn
            return None

        with patch.object(apply_mod, "first_visible", new=AsyncMock(side_effect=no_markers)), \
             patch.object(apply_mod, "dismiss_overlays", new=AsyncMock()), \
             patch.object(apply_mod, "human_pause", new=AsyncMock()), \
             patch.object(engine, "_fast_check", new=AsyncMock(side_effect=fast_check)), \
             patch.object(engine, "_wait_for_post_apply_event", new=AsyncMock(return_value="timeout")), \
             patch.object(engine, "_verify_post_submit", new=AsyncMock(return_value="unknown")), \
             patch.object(apply_mod, "ChatbotHandler", return_value=fake_bot):
            return await engine._submit(self._job(), "P", 1, MagicMock())

    async def test_late_drawer_converts_to_applied(self):
        from naukri_agent.core.models import ApplicationStatus

        outcome = await self._submit_with_drawer(drawer_on_first_check=False)
        self.assertEqual(outcome.status, ApplicationStatus.APPLIED)

    async def test_no_drawer_still_fails_closed(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.core.models import ApplicationStatus
        from naukri_agent.naukri import apply as apply_mod

        btn = self._btn()
        page = self._page(btn)
        engine = self._engine(page)
        engine.artifacts = MagicMock()
        engine.artifacts.screenshot = AsyncMock(return_value="/tmp/x.png")
        engine.artifacts.capture_failure = AsyncMock(return_value="/tmp/y.png")

        async def no_markers(scope, sels, timeout_ms=6000):
            sels = list(sels)
            if sels and sels[0] == "button#apply-button":
                return btn
            return None

        with patch.object(apply_mod, "first_visible", new=AsyncMock(side_effect=no_markers)), \
             patch.object(engine, "_fast_check", new=AsyncMock(return_value=False)), \
             patch.object(apply_mod, "dismiss_overlays", new=AsyncMock()), \
             patch.object(apply_mod, "human_pause", new=AsyncMock()), \
             patch.object(engine, "_fast_check", new=AsyncMock(return_value=False)), \
             patch.object(engine, "_wait_for_post_apply_event", new=AsyncMock(return_value="timeout")), \
             patch.object(engine, "_verify_post_submit", new=AsyncMock(return_value="unknown")):
            outcome = await engine._submit(self._job(), "P", 1, MagicMock())
        self.assertEqual(outcome.status, ApplicationStatus.FAILED)
        self.assertIn("No success confirmation", outcome.detail)

    async def test_handler_maps_unanswered_and_unfit(self):
        from unittest.mock import AsyncMock, MagicMock

        from naukri_agent.core.models import ApplicationStatus

        page = MagicMock()
        page.url = "https://www.naukri.com/job-listings-1"
        artifacts = MagicMock()
        artifacts.screenshot = AsyncMock(return_value="/tmp/x.png")
        artifacts.capture_failure = AsyncMock(return_value="/tmp/y.png")
        engine = self._engine(page)
        engine.artifacts = artifacts

        unfit = MagicMock(unfit="requires 4y", unanswered=[], error=None,
                          completed=False, answered=0)
        out = await engine._handle_chatbot_result(self._job(), "P", 1, unfit)
        self.assertEqual(out.status, ApplicationStatus.SKIPPED)

        unanswered = MagicMock(unfit=None, unanswered=[MagicMock()],
                               error=None, completed=False, answered=1)
        out = await engine._handle_chatbot_result(self._job(), "P", 1, unanswered)
        self.assertEqual(out.status, ApplicationStatus.NEEDS_REVIEW)


class TestAssessmentSkip(unittest.IsolatedAsyncioTestCase):
    """Proctored-test questions must skip the job, never queue review.

    Run 500: an autoproctor link sat in review forever — the agent can never
    take the test, so answering is impossible and reviewing is pointless.
    """

    def test_assessment_markers_skip(self):
        from naukri_agent.naukri.chatbot import assessment_skip_detail

        for q in [
            "Please submit the online assessment : https://www.autoproctor.co/tests/bfIxNc90WC/instructions/",
            "You must complete the HackerRank coding test to proceed",
            "Take the test at the link below within 48 hours",
            "This role requires an aptitude test score",
        ]:
            self.assertIsNotNone(assessment_skip_detail(q), q)

    def test_ordinary_questions_untouched(self):
        from naukri_agent.naukri.chatbot import assessment_skip_detail

        for q in [
            "How many years of experience do you have in Python?",
            "Are you willing to relocate to Bengaluru?",
            "Rate your fit for this role on a scale of 1-10",
            "What is your current CTC?",
            "",
        ]:
            self.assertIsNone(assessment_skip_detail(q), q)

    def test_reason_bucket_exists(self):
        from naukri_agent.core.models import SkipReason

        self.assertEqual(SkipReason.ASSESSMENT_REQUIRED.value, "assessment_required")

    async def test_handler_maps_assessment_reason(self):
        from unittest.mock import AsyncMock, MagicMock

        from naukri_agent.core.models import ApplicationStatus, Job, SkipReason
        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.naukri.apply import ApplyEngine

        page = MagicMock()
        page.url = "https://www.naukri.com/job-listings-1"
        artifacts = MagicMock()
        artifacts.screenshot = AsyncMock(return_value="/tmp/x.png")
        artifacts.capture_failure = AsyncMock(return_value="/tmp/y.png")
        engine = ApplyEngine(page, MagicMock(), artifacts,
                             policy=RunPolicy(dry_run=False, side_effects_enabled=True))
        res = MagicMock(unfit="requires proctored online assessment",
                        unanswered=[], error=None, completed=False, answered=0)
        res.unfit_reason = SkipReason.ASSESSMENT_REQUIRED
        job = Job(job_id="reco-9", title="SDE", company="Acme",
                  url="https://www.naukri.com/job-listings-1")
        out = await engine._handle_chatbot_result(job, "P", 1, res)
        self.assertEqual(out.status, ApplicationStatus.SKIPPED)
        self.assertEqual(out.reason, SkipReason.ASSESSMENT_REQUIRED)


class TestDescriptionSnippetPersistence(unittest.IsolatedAsyncioTestCase):
    """Migration 0019: card/JD snippets persist (truncated) for offline audits."""

    def test_to_row_truncates_snippet(self):
        from naukri_agent.core.models import Job

        job = Job(job_id="j1", title="SDE", company="C", url="u", description="x" * 5000)
        row = job.to_row()
        self.assertEqual(len(row["description_snippet"]), 2000)
        self.assertEqual(Job(job_id="j2", title="T", company="C", url="u").to_row()["description_snippet"], "")

    def test_migration_declared(self):
        from naukri_agent.db.migrations import MIGRATIONS

        versions = [v for v, _ in MIGRATIONS]
        self.assertIn("0019_job_description_snippet", versions)
        sql = dict(MIGRATIONS)["0019_job_description_snippet"]
        self.assertIn("description_snippet", sql)
        self.assertIn("IF NOT EXISTS", sql)

    async def test_upsert_writes_snippet(self):
        from unittest.mock import AsyncMock

        from naukri_agent.core.models import Job
        from naukri_agent.db.repository import Repository

        seen = {}

        async def capture(query, *args):
            seen["n_args"] = len(args)
            seen["snippet"] = args[17]
            return "INSERT 0 1"

        pool = AsyncMock()
        pool.execute = capture
        repo = Repository(pool)
        job = Job(job_id="j1", title="SDE", company="C", url="u", description="hello world")
        self.assertTrue(await repo.upsert_job(job, platform="naukri"))
        self.assertEqual(seen["n_args"], 18)
        self.assertEqual(seen["snippet"], "hello world")


class TestReviewRetryQuery(unittest.IsolatedAsyncioTestCase):
    """needs_review_retries: deterministic re-drive for answered reviews."""

    def _repo(self, fetch_result):
        from unittest.mock import AsyncMock

        from naukri_agent.db.repository import Repository

        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=fetch_result)
        return Repository(pool)

    def _row(self):
        return {
            "job_id": "reco-1", "title": "SDE", "company": "Acme",
            "url": "https://www.naukri.com/job-listings-1", "location": "Bengaluru",
            "experience_text": "2-4 Yrs", "salary_text": "5-9 Lacs PA",
            "posted_text": "2 days ago", "rating": 4.0, "tags": ["python", "react"],
            "min_experience": 2.0, "max_experience": 4.0, "min_salary_lpa": 5.0,
            "posted_days_ago": 2, "is_walkin": False, "source_keyword": "sde",
            "platform": "naukri",
        }

    async def test_maps_row_to_job(self):
        repo = self._repo([self._row()])
        jobs = await repo.needs_review_retries("AI / Python Engineer", "naukri", "primary")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "reco-1")
        self.assertEqual(jobs[0].tags, ["python", "react"])
        self.assertEqual(jobs[0].platform, "naukri")

    async def test_string_tags_split(self):
        row = self._row()
        row["tags"] = "python, react"
        repo = self._repo([row])
        jobs = await repo.needs_review_retries("AI / Python Engineer", "naukri", "primary")
        self.assertEqual(jobs[0].tags, ["python", "react"])

    async def test_db_failure_returns_empty_never_raises(self):
        from unittest.mock import AsyncMock

        from naukri_agent.db.repository import Repository

        pool = AsyncMock()
        pool.fetch = AsyncMock(side_effect=OSError("down"))
        repo = Repository(pool)
        # _fetch_with_retry retries on OSError with sleep; patch sleep fast
        import unittest.mock as mock

        with mock.patch("asyncio.sleep", new=AsyncMock()):
            jobs = await repo.needs_review_retries("P", "naukri", "primary")
        self.assertEqual(jobs, [])


class TestResumeGate(unittest.IsolatedAsyncioTestCase):
    """The Naukri profile must carry the running account's CV.

    A swapped resume poisons every application in the run: confident
    mismatch aborts loudly, flaky reads never block.
    """

    def _orc(self, account):
        from types import SimpleNamespace

        from naukri_agent.core.orchestrator import Orchestrator

        o = Orchestrator.__new__(Orchestrator)
        o.account_key = account
        o.config = SimpleNamespace(profiles=[
            SimpleNamespace(enabled=True, resume_file="resumes/CV_Mahesh_Chitakoti_2026.pdf",
                            account="primary"),
            SimpleNamespace(enabled=True, resume_file="resumes/CV_Mahesh_Chitakoti_2026_1_.pdf",
                            account="secondary"),
        ])
        return o

    def test_longest_stem_disambiguates_nested_names(self):
        from naukri_agent.core.orchestrator import Orchestrator

        expected = {"primary": "CV_Mahesh_Chitakoti_2026",
                    "secondary": "CV_Mahesh_Chitakoti_2026_1_"}
        # Secondary file active: primary stem is a substring too, but the
        # longer (secondary) stem must win.
        self.assertEqual(
            Orchestrator._match_resume_account("CV_Mahesh_Chitakoti_2026_1_.pdf", expected),
            "secondary",
        )
        self.assertEqual(
            Orchestrator._match_resume_account("CV_Mahesh_Chitakoti_2026.pdf", expected),
            "primary",
        )
        self.assertIsNone(Orchestrator._match_resume_account("Random CV.pdf", expected))
        self.assertIsNone(Orchestrator._match_resume_account("", expected))

    async def test_wrong_resume_aborts(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.core.orchestrator import FatalAgentError

        mgr = MagicMock()
        mgr.current_resume_name = AsyncMock(
            return_value="CV_Mahesh_Chitakoti_2026_1_.pdf")
        with patch("naukri_agent.naukri.resume.ResumeManager", return_value=mgr):
            with self.assertRaises(FatalAgentError):
                await self._orc("primary")._verify_active_resume(MagicMock())

    async def test_right_resume_passes(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        mgr = MagicMock()
        mgr.current_resume_name = AsyncMock(
            return_value="CV_Mahesh_Chitakoti_2026.pdf")
        with patch("naukri_agent.naukri.resume.ResumeManager", return_value=mgr):
            await self._orc("primary")._verify_active_resume(MagicMock())

    async def test_unreadable_or_unknown_never_blocks(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        for current in ("", "Some Other CV.pdf"):
            mgr = MagicMock()
            mgr.current_resume_name = AsyncMock(return_value=current)
            with patch("naukri_agent.naukri.resume.ResumeManager", return_value=mgr):
                await self._orc("primary")._verify_active_resume(MagicMock())


class TestLinkedInSuitabilityTokens(unittest.TestCase):
    """Oct 2026 audit: 100 real AI/ML jobs died on 'No candidate-stack
    overlap' because the two most common title tokens (ai/ml) were missing
    from track_keywords. 52 recovered; bare generics must stay out."""

    def _check(self, title, location="Bengaluru, India"):
        from naukri_agent.core.models import Job
        from naukri_agent.platforms.linkedin import LinkedInPlatform

        plat = LinkedInPlatform.__new__(LinkedInPlatform)
        job = Job(job_id="x", title=title, company="C", url="u",
                  location=location, description="", platform="linkedin")
        return LinkedInPlatform.evaluate_job_suitability(plat, job)

    def test_ai_family_recovers(self):
        for title in [
            "AI/ML Engineer",
            "Gen AI Engineer",
            "AI Voice Agent Developer",
            "Applied AI Engineer",
            "AI Engineer",
            "Forward Deployed AI Engineer",
            "Software Engineer in Test",
            "Junior Full-Stack Developer",
        ]:
            suitable, reason, score = self._check(title)
            self.assertTrue(suitable, f"{title!r} should pass: {reason}")

    def test_guards_hold(self):
        for title, why in [
            ("Software Engineer", "bare generic, run-377 lesson"),
            ("Software Developer", "bare generic"),
            ("Java Developer", "blocked stack"),
            ("Data Scientist", "blocked family"),
            ("UI/UX Designer", "non-dev"),
        ]:
            suitable, reason, _ = self._check(title)
            self.assertFalse(suitable, f"{title!r} must stay out ({why}): {reason}")

    def test_legit_blocks_unchanged(self):
        self.assertFalse(self._check("Full Stack Engineer AEM + React", location="India")[0])
        self.assertFalse(self._check(
            "Forward Deployed AI/ML Engineer IV", location="United States")[0])
        # Word boundaries: ai/ml must not fire inside other words
        suitable, _, _ = self._check("Airbnb Experience Designer")
        self.assertFalse(suitable)


class TestLinkedInBlockedStep(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_labels_and_text(self):
        from unittest.mock import AsyncMock, MagicMock

        from naukri_agent.platforms.linkedin import LinkedInPlatform

        el = MagicMock()
        el.get_attribute = AsyncMock(return_value="How many years of experience?")
        locs = MagicMock()
        locs.all = AsyncMock(return_value=[el])
        modal = MagicMock()
        modal.locator = MagicMock(return_value=locs)
        qs = await LinkedInPlatform._extract_blocked_questions(
            modal, "Step 6 Additional Questions How many years...")
        self.assertEqual(len(qs), 1)
        self.assertIn("How many years", qs[0]["text"])
        self.assertEqual(qs[0]["kind"], "text")

    async def test_empty_step_returns_empty(self):
        from unittest.mock import AsyncMock, MagicMock

        from naukri_agent.platforms.linkedin import LinkedInPlatform

        locs = MagicMock()
        locs.all = AsyncMock(return_value=[])
        modal = MagicMock()
        modal.locator = MagicMock(return_value=locs)
        self.assertEqual(await LinkedInPlatform._extract_blocked_questions(modal, ""), [])

    async def test_multipage_blob_splits(self):
        """Review #62 verbatim shape: one blob with face-2-face Yes/No, a
        Hyderabad presence Yes/No, and years text inputs must split into
        separate items — answerable ones train, presence stays review."""
        from unittest.mock import AsyncMock, MagicMock

        from naukri_agent.platforms.linkedin import LinkedInPlatform

        locs = MagicMock()
        locs.all = AsyncMock(return_value=[])
        modal = MagicMock()
        modal.locator = MagicMock(return_value=locs)
        blob = (
            "Apply to SRS Consulting Inc 3/4 pages Additional Questions "
            "Are you comfortable for face 2 face interview?* Yes No "
            "This field is required "
            "Are you currently in Hyderabad?* Yes No "
            "This field is required "
            "How many years of work experience do you have with Python?* "
            "How many years of work experience do you have with Docker?*"
        )
        qs = await LinkedInPlatform._extract_blocked_questions(modal, blob)
        kinds = [q["kind"] for q in qs]
        self.assertIn("radio", kinds)
        self.assertIn("text", kinds)
        self.assertTrue(all(len(q["text"]) >= 20 for q in qs))
        self.assertLessEqual(len(qs), 6)

    async def test_counter_debris_not_strong_text(self):
        """'* 1/20 1 of 20 characters' belongs to the previous box — a Yes/No
        question after one must still read as radio, not text."""
        from unittest.mock import AsyncMock, MagicMock

        from naukri_agent.platforms.linkedin import LinkedInPlatform

        locs = MagicMock()
        locs.all = AsyncMock(return_value=[])
        modal = MagicMock()
        modal.locator = MagicMock(return_value=locs)
        blob = ("* 1/20 1 of 20 characters Can you write complex SQL queries?* "
                "Yes No This field is required")
        qs = await LinkedInPlatform._extract_blocked_questions(modal, blob)
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["kind"], "radio")

    def _btn(self, text, visible=True):
        from unittest.mock import AsyncMock, MagicMock

        btn = MagicMock()
        btn.is_visible = AsyncMock(return_value=visible)
        btn.inner_text = AsyncMock(return_value=text)
        return btn

    def _modal_with_buttons(self, texts):
        from unittest.mock import AsyncMock, MagicMock

        btns = [self._btn(t) for t in texts]
        locs = MagicMock()
        locs.all = AsyncMock(return_value=btns)
        modal = MagicMock()
        modal.locator = MagicMock(return_value=locs)
        return modal

    async def test_fallback_advance_accepts_continue(self):
        from naukri_agent.platforms.linkedin import LinkedInPlatform

        modal = self._modal_with_buttons(["Continue"])
        btn = await LinkedInPlatform._fallback_action_button(modal)
        self.assertIsNotNone(btn)

    async def test_fallback_advance_rejects_back_and_dismiss(self):
        from naukri_agent.platforms.linkedin import LinkedInPlatform

        modal = self._modal_with_buttons(["Back", "Dismiss", "Save and exit"])
        self.assertIsNone(await LinkedInPlatform._fallback_action_button(modal))

    async def test_fallback_advance_rejects_save_and_exit_over_save(self):
        from naukri_agent.platforms.linkedin import LinkedInPlatform

        # "Save and exit" contains "save" but is an abandon path.
        modal = self._modal_with_buttons(["Save and exit"])
        self.assertIsNone(await LinkedInPlatform._fallback_action_button(modal))

    async def test_modal_button_labels_collects_visible(self):
        from naukri_agent.platforms.linkedin import LinkedInPlatform

        modal = self._modal_with_buttons(["Next", "Back"])
        labels = await LinkedInPlatform._modal_button_labels(modal)
        self.assertEqual(labels, ["Next", "Back"])

    async def test_modal_button_labels_never_raises(self):
        from unittest.mock import MagicMock

        from naukri_agent.platforms.linkedin import LinkedInPlatform

        bad = MagicMock()
        bad.locator = MagicMock(side_effect=RuntimeError("detached"))
        self.assertEqual(await LinkedInPlatform._modal_button_labels(bad), [])
        self.assertIsNone(await LinkedInPlatform._fallback_action_button(bad))

    async def test_modal_visible_never_raises(self):
        from unittest.mock import AsyncMock, MagicMock

        from naukri_agent.platforms.linkedin import LinkedInPlatform

        self.assertFalse(await LinkedInPlatform._modal_visible(None))
        bad = MagicMock()
        bad.is_visible = AsyncMock(side_effect=RuntimeError("detached"))
        self.assertFalse(await LinkedInPlatform._modal_visible(bad))
        good = MagicMock()
        good.is_visible = AsyncMock(return_value=True)
        self.assertTrue(await LinkedInPlatform._modal_visible(good))


class TestCheckboxSubmit(unittest.IsolatedAsyncioTestCase):
    """City multi-selects failed silently across runs 268-507: label clicks
    swallowed, Save controls named Done/OK/Confirm. Verify checked-state
    recovery and the drawer-scoped text fallback."""

    def _handler(self):
        from unittest.mock import MagicMock

        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.naukri.chatbot import ChatbotHandler

        return ChatbotHandler(MagicMock(), MagicMock(), RunPolicy(
            dry_run=False, side_effects_enabled=True))

    def _box(self, checked_sequence):
        from unittest.mock import AsyncMock, MagicMock

        box = MagicMock()
        box.count = AsyncMock(return_value=1)
        box.is_checked = AsyncMock(side_effect=list(checked_sequence))
        box.evaluate = AsyncMock()
        return box

    def _label_with(self, box_or_none):
        from unittest.mock import AsyncMock, MagicMock

        label = MagicMock()

        def _locator(sel):
            inner = MagicMock()
            if sel == "input[type='checkbox']" and box_or_none is not None:
                inner.count = AsyncMock(return_value=1)
                inner.first = box_or_none
                return inner
            inner.count = AsyncMock(return_value=0)
            return inner

        label.locator = MagicMock(side_effect=_locator)
        return label

    async def test_already_checked_no_reclick(self):
        handler = self._handler()
        box = self._box([True])
        label = self._label_with(box)
        self.assertTrue(await handler._ensure_checkbox_checked(label))
        box.evaluate.assert_not_called()

    async def test_js_recovery_checks_the_box(self):
        handler = self._handler()
        box = self._box([False, True])
        label = self._label_with(box)
        self.assertTrue(await handler._ensure_checkbox_checked(label))
        box.evaluate.assert_called_once()

    async def test_no_input_preserves_old_behavior(self):
        handler = self._handler()
        label = self._label_with(None)
        self.assertTrue(await handler._ensure_checkbox_checked(label))

    async def test_submit_fallback_done_button(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.naukri import chatbot as cb_mod

        btn = MagicMock()
        btn.is_visible = AsyncMock(return_value=True)
        btn.count = AsyncMock(return_value=1)
        btn.inner_text = AsyncMock(return_value="Done")
        btn.click = AsyncMock()
        scope = MagicMock()
        scope.locator = MagicMock(return_value=MagicMock(
            all=AsyncMock(return_value=[btn])))
        drawer = scope
        page = MagicMock()

        def _locator(sel):
            m = MagicMock()
            m.count = AsyncMock(return_value=0)
            m.first = m
            return m

        page.locator = MagicMock(side_effect=_locator)
        handler = self._handler()
        handler.page = page
        with patch.object(cb_mod, "first_visible", new=AsyncMock(return_value=drawer)):
            self.assertTrue(await handler._submit())
        btn.click.assert_called_once()

    async def test_submit_no_drawer_no_save_is_false(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.naukri import chatbot as cb_mod

        page = MagicMock()

        def _locator(sel):
            m = MagicMock()
            m.count = AsyncMock(return_value=0)
            m.first = m
            return m

        page.locator = MagicMock(side_effect=_locator)
        handler = self._handler()
        handler.page = page
        with patch.object(cb_mod, "first_visible", new=AsyncMock(return_value=None)):
            self.assertFalse(await handler._submit())

    async def test_handler_routes_error_plus_unanswered_to_review(self):
        from unittest.mock import AsyncMock, MagicMock

        from naukri_agent.core.models import ApplicationStatus, Job, SkipReason
        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.naukri.apply import ApplyEngine

        page = MagicMock()
        page.url = "https://www.naukri.com/job-listings-1"
        artifacts = MagicMock()
        artifacts.screenshot = AsyncMock(return_value="/tmp/x.png")
        artifacts.capture_failure = AsyncMock(return_value="/tmp/y.png")
        engine = ApplyEngine(page, MagicMock(), artifacts,
                             policy=RunPolicy(dry_run=False, side_effects_enabled=True))
        res = MagicMock(unanswered=[MagicMock()], error="could not submit answer for: X",
                        unfit=None, completed=False, answered=1)
        job = Job(job_id="reco-9", title="SDE", company="Acme",
                  url="https://www.naukri.com/job-listings-1")
        out = await engine._handle_chatbot_result(job, "P", 1, res)
        self.assertEqual(out.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(out.reason, SkipReason.UNANSWERED_QUESTION)
        self.assertIn("submit also failed", out.detail)
        self.assertEqual(len(out.unanswered_questions), 1)


class TestFreeTextHunter(unittest.IsolatedAsyncioTestCase):
    """Last-resort option hunter: clicks only word-matching short nodes and
    submits only on verified selection (run-268→509 silent streak)."""

    def _handler(self, page):
        from unittest.mock import MagicMock

        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.naukri.chatbot import ChatbotHandler

        return ChatbotHandler(page, MagicMock(), RunPolicy(
            dry_run=False, side_effects_enabled=True))

    def _node(self, text, visible=True):
        from unittest.mock import AsyncMock, MagicMock

        el = MagicMock()
        el.is_visible = AsyncMock(return_value=visible)
        el.count = AsyncMock(return_value=1)
        el.inner_text = AsyncMock(return_value=text)
        el.click = AsyncMock()
        el.evaluate = AsyncMock()
        el.focus = AsyncMock()
        el.press = AsyncMock()
        return el

    def _scope(self, nodes, evidence_on=True):
        from unittest.mock import AsyncMock, MagicMock

        scope = MagicMock()
        scope.locator = MagicMock(side_effect=lambda sel: MagicMock(
            all=AsyncMock(return_value=list(nodes)),
            first=MagicMock(
                count=AsyncMock(return_value=1 if evidence_on else 0),
                is_visible=AsyncMock(return_value=True),
            ),
        ))
        return scope

    def _page(self, scope):
        from unittest.mock import MagicMock

        page = MagicMock()
        page.locator = MagicMock(return_value=scope)
        return page

    async def test_verified_click_submits(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.naukri import chatbot as cb_mod

        node = self._node("Bengaluru")
        scope = self._scope([node])
        handler = self._handler(self._page(scope))
        with patch.object(cb_mod, "first_visible", new=AsyncMock(return_value=scope)):
            with patch.object(cb_mod, "human_pause", new=AsyncMock()):
                # force _submit True via a Save control
                with patch.object(handler, "_submit", new=AsyncMock(return_value=True)):
                    ok = await handler._answer_free_text_option(
                        "Bengaluru", MagicMock(text="Which city?", options=[]))
        self.assertTrue(ok)
        node.click.assert_called_once()

    async def test_no_match_returns_false(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.naukri import chatbot as cb_mod

        scope = self._scope([self._node("Pune"), self._node("Chennai")])
        handler = self._handler(self._page(scope))
        with patch.object(cb_mod, "first_visible", new=AsyncMock(return_value=scope)):
            with patch.object(cb_mod, "human_pause", new=AsyncMock()):
                ok = await handler._answer_free_text_option(
                    "Bengaluru", MagicMock(text="Which city?", options=[]))
        self.assertFalse(ok)

    async def test_click_without_evidence_keeps_hunting(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.naukri import chatbot as cb_mod

        node = self._node("Bengaluru")
        scope = self._scope([node], evidence_on=False)
        handler = self._handler(self._page(scope))
        with patch.object(cb_mod, "first_visible", new=AsyncMock(return_value=scope)):
            with patch.object(cb_mod, "human_pause", new=AsyncMock()):
                ok = await handler._answer_free_text_option(
                    "Bengaluru", MagicMock(text="Which city?", options=[]))
        # Click happened (no crash) but no verified selection -> False,
        # never a blind submit.
        self.assertFalse(ok)
        node.click.assert_called_once()

    async def test_giant_container_unclickable(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.naukri import chatbot as cb_mod

        big = self._node("Bengaluru " + ("lorem ipsum dolor sit amet " * 10))
        scope = self._scope([big])
        handler = self._handler(self._page(scope))
        with patch.object(cb_mod, "first_visible", new=AsyncMock(return_value=scope)):
            with patch.object(cb_mod, "human_pause", new=AsyncMock()):
                ok = await handler._answer_free_text_option(
                    "Bengaluru", MagicMock(text="Which city?", options=[]))
        self.assertFalse(ok)
        big.click.assert_not_called()

    def test_word_boundaries(self):
        from naukri_agent.naukri.chatbot import ChatbotHandler

        self.assertTrue(ChatbotHandler._word_in("bengaluru", "bengaluru, karnataka"))
        self.assertFalse(ChatbotHandler._word_in("ai", "airbnb host"))
        self.assertFalse(ChatbotHandler._word_in("go", "django developer"))
        self.assertFalse(ChatbotHandler._word_in("", "anything"))


class TestPauseRetry(unittest.TestCase):
    """A paused platform must self-heal on cooldown, never skip forever."""

    def test_cooldown_gates_retry(self):
        from naukri_agent.core.orchestrator import _should_retry_paused

        self.assertFalse(_should_retry_paused(None))
        self.assertFalse(_should_retry_paused(0.0))
        self.assertFalse(_should_retry_paused(11.9))
        self.assertTrue(_should_retry_paused(12.0))
        self.assertTrue(_should_retry_paused(72.5))
        self.assertFalse(_should_retry_paused("nonsense"))


class TestPauseAgeQuery(unittest.IsolatedAsyncioTestCase):
    async def test_maps_row_and_failures(self):
        from unittest.mock import AsyncMock

        from naukri_agent.db.repository import Repository

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value={"age_h": 13.5})
        repo = Repository(pool)
        self.assertAlmostEqual(
            await repo.platform_pause_age_hours("primary", "linkedin"), 13.5)

        pool2 = AsyncMock()
        pool2.fetchrow = AsyncMock(return_value=None)
        self.assertIsNone(await Repository(pool2).platform_pause_age_hours("p", "l"))

        pool3 = AsyncMock()
        pool3.fetchrow = AsyncMock(side_effect=OSError("down"))
        import unittest.mock as mock

        with mock.patch("asyncio.sleep", new=AsyncMock()):
            self.assertIsNone(
                await Repository(pool3).platform_pause_age_hours("p", "l"))


class TestVerifiedClick(unittest.IsolatedAsyncioTestCase):
    """Run 512: 8 Apply clicks with zero page reaction (React swallowed them
    on detached nodes). Re-click once only on provably dead clicks."""

    def _engine(self, page):
        from unittest.mock import MagicMock

        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.naukri.apply import ApplyEngine

        artifacts = MagicMock()
        artifacts.screenshot = MagicMock()
        artifacts.capture_failure = MagicMock()
        # capture_failure is awaited in dead paths; make it awaitable
        from unittest.mock import AsyncMock

        artifacts.capture_failure = AsyncMock(return_value="/tmp/y.png")
        return ApplyEngine(page, MagicMock(), artifacts,
                           policy=RunPolicy(dry_run=False, side_effects_enabled=True))

    async def test_effect_cases(self):
        from unittest.mock import MagicMock

        page = MagicMock()
        page.url = "https://www.naukri.com/job-listings-1"
        engine = self._engine(page)
        engine._popup_opened = True
        self.assertTrue(await engine._submit_effect_seen(("u", True, True, "apply")))
        engine._popup_opened = False

        async def fast_false(sels):
            return False

        engine._fast_check = fast_false
        # url changed
        page.url = "https://www.naukri.com/other"
        self.assertTrue(await engine._submit_effect_seen(
            ("https://www.naukri.com/job-listings-1", True, True, "apply")))
        page.url = "https://www.naukri.com/job-listings-1"

    async def test_button_state_change_is_effect(self):
        from unittest.mock import AsyncMock, MagicMock

        async def fast_false(sels):
            return False

        for pre, post_text, post_enabled, post_present in [
            (("u", True, True, "apply"), "applied", True, True),
            (("u", True, True, "apply"), "apply", False, True),
        ]:
            page = MagicMock()
            page.url = "u"
            state = MagicMock()
            state.count = AsyncMock(return_value=1 if post_present else 0)
            state.inner_text = AsyncMock(return_value=post_text)
            state.is_enabled = AsyncMock(return_value=post_enabled)
            first = MagicMock()
            first.count = state.count
            first.inner_text = state.inner_text
            first.is_enabled = state.is_enabled
            page.locator = MagicMock(return_value=MagicMock(first=first))
            engine = self._engine(page)
            engine._fast_check = fast_false
            self.assertTrue(await engine._submit_effect_seen(pre))

    async def test_dead_click_recovers_with_reclick(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.core.models import ApplicationStatus
        from naukri_agent.naukri import apply as apply_mod

        clickbtn = AsyncMock()
        clickbtn.is_visible = AsyncMock(return_value=True)
        clickbtn.scroll_into_view_if_needed = AsyncMock()
        clickbtn.click = AsyncMock()
        clickbtn.evaluate = AsyncMock()
        state = MagicMock()
        state.count = AsyncMock(return_value=1)
        state.inner_text = AsyncMock(return_value="Apply")
        state.is_enabled = AsyncMock(return_value=True)
        listing = MagicMock()
        listing.first = state
        listing.all = AsyncMock(return_value=[clickbtn])
        page = MagicMock()
        page.url = "https://www.naukri.com/job-listings-1"
        page.title = AsyncMock(return_value="Software Engineer")
        page.locator = MagicMock(side_effect=lambda sel: listing if sel == "button#apply-button" else MagicMock(
            first=MagicMock(count=AsyncMock(return_value=0)),
            all=AsyncMock(return_value=[]),
            count=AsyncMock(return_value=0),
        ))
        engine = self._engine(page)
        freshbtn = AsyncMock()
        freshbtn.click = AsyncMock()
        freshbtn.evaluate = AsyncMock()

        async def no_markers(scope, sels, timeout_ms=6000):
            if sels and sels[0] == "button#apply-button":
                return freshbtn
            return None

        async def fast_false(sels):
            return False

        job = MagicMock(job_id="reco-9", title="SDE", company="Acme",
                        url="https://www.naukri.com/job-listings-1")
        with patch.object(apply_mod, "first_visible", new=AsyncMock(side_effect=no_markers)), \
             patch.object(apply_mod, "dismiss_overlays", new=AsyncMock()), \
             patch.object(apply_mod, "human_pause", new=AsyncMock()), \
             patch("asyncio.sleep", new=AsyncMock()), \
             patch.object(engine, "_fast_check", new=AsyncMock(side_effect=fast_false)), \
             patch.object(engine, "_wait_for_post_apply_event", new=AsyncMock(return_value="timeout")), \
             patch.object(engine, "_verify_post_submit", new=AsyncMock(return_value="unknown")):
            outcome = await engine._submit(job, "P", 1, MagicMock())
        # First cascade clicked once + exactly one guarded re-click, then
        # fail-closed (no drawer/markers anywhere in this harness).
        self.assertEqual(clickbtn.click.await_count, 1)
        self.assertEqual(freshbtn.click.await_count, 1)
        self.assertEqual(outcome.status, ApplicationStatus.FAILED)
        self.assertIn("No success confirmation", outcome.detail)


class TestContractionFit(unittest.TestCase):
    """"I'm" vs "I am" silently broke option fitting (Q#42 Gurugram screener
    went unresolved despite an exact-phrase KB key)."""

    def test_expansion_unit(self):
        from naukri_agent.core.answers import _expand_contractions

        self.assertEqual(_expand_contractions("i'm outside ncr"), "i am outside ncr")
        self.assertEqual(_expand_contractions("do not relocate"), "do not relocate")
        self.assertEqual(_expand_contractions("can't relocate"), "cannot relocate")

    def test_q42_resolves_to_relocate_option(self):
        from naukri_agent.core.models import ScreeningQuestion

        cfg = AgentConfig.load()
        from naukri_agent.core.answers import AnswerEngine

        eng = AnswerEngine(kb=[(k, v) for k, v in cfg.answers.items()],
                           profile_answers={}, strict=True)
        q = ScreeningQuestion(
            text="This role is fully in-office at Sector 65, Gurugram. Which describes you?",
            kind="radio",
            options=["I live in Gurugram",
                     "I live elsewhere in Delhi NCR and can commute daily",
                     "I am outside NCR and will relocate before joining",
                     "I canoot relocate / I am looking for remote or hybrid"],
        )
        r = eng.resolve(q)
        self.assertIsNotNone(r)
        self.assertIn("relocate before joining", r.value)
        self.assertNotIn("Gurugram', 'I live", r.value)

    def test_q41_resolves_to_url(self):
        from naukri_agent.core.models import ScreeningQuestion

        cfg = AgentConfig.load()
        from naukri_agent.core.answers import AnswerEngine

        eng = AnswerEngine(kb=[(k, v) for k, v in cfg.answers.items()],
                           profile_answers={}, strict=True)
        q = ScreeningQuestion(
            text="Please submit your portfolio link (if you have a portfolio) or share a link to your best work",
            kind="text", options=[],
        )
        r = eng.resolve(q)
        self.assertIsNotNone(r)
        self.assertIn("github.com/maheshchichkoti", r.value)


class TestRun569Unanswered(unittest.TestCase):
    """Run 569 (#65-79): 15 screening questions queued for review in one
    run. Root causes, one per failure class."""

    def _eng(self, profile_name="AI / Python Engineer"):
        from naukri_agent.core.answers import AnswerEngine

        cfg = AgentConfig.load()
        prof = next(p for p in cfg.profiles if p.name == profile_name)
        return AnswerEngine(
            kb=[(k, v) for k, v in cfg.answers.items()],
            profile_answers=dict(prof.answers),
            strict=True,
            experience=cfg.experience_for(prof),
        )

    def test_role_question_resolves_profile_title(self):
        from naukri_agent.core.models import ScreeningQuestion

        r = self._eng("AI / Python Engineer").resolve(ScreeningQuestion(
            text="What role / roles you are applying for?",
            kind="text", options=[]))
        self.assertIsNotNone(r)
        self.assertIn("AI", r.value)
        r = self._eng("Full Stack / Web Developer").resolve(ScreeningQuestion(
            text="What role / roles you are applying for?",
            kind="text", options=[]))
        self.assertIsNotNone(r)
        self.assertIn("Full Stack", r.value)

    def test_tenure_beats_language_list(self):
        # "How many years ... Python?" must answer years, never the
        # "programming language" skill list.
        from naukri_agent.core.models import ScreeningQuestion

        r = self._eng("Full Stack / Web Developer").resolve(ScreeningQuestion(
            text="Apply to techolution 3/4 pages Additional Questions "
                 "How many years of work experience do you have with "
                 "Python (Programming Language)?",
            kind="text", options=[]))
        self.assertIsNotNone(r)
        self.assertEqual(r.value.strip(), "2")

    def test_more_than_5_years_is_no(self):
        from naukri_agent.core.models import ScreeningQuestion

        r = self._eng("Full Stack / Web Developer").resolve(ScreeningQuestion(
            text="Do you have more than 5 years of exp?",
            kind="text", options=[]))
        self.assertIsNotNone(r)
        self.assertEqual(r.value, "No")

    def test_foreign_presence_is_no(self):
        from naukri_agent.core.models import ScreeningQuestion

        r = self._eng().resolve(ScreeningQuestion(
            text="Are you currently based in Gurgaon?",
            kind="radio", options=["Yes", "No"]))
        self.assertIsNotNone(r)
        self.assertEqual(r.value, "No")

    def test_compensation_below_30l_is_yes(self):
        # Expected 7 LPA <= 30. Guards the "nit"-in-"opportunity"
        # substring false positive that answered No.
        from naukri_agent.core.models import ScreeningQuestion

        r = self._eng().resolve(ScreeningQuestion(
            text="Is your expected annual compensation for this opportunity "
                 "less than 30 lakhs?",
            kind="radio", options=["Yes", "No"]))
        self.assertIsNotNone(r)
        self.assertEqual(r.value, "Yes")

    def test_opportunity_never_matches_nit(self):
        from naukri_agent.core.models import ScreeningQuestion

        r = self._eng().resolve(ScreeningQuestion(
            text="Is this opportunity open to freshers?",
            kind="radio", options=["Yes", "No"]))
        # Must not resolve via the "nit" (NIT college) KB entry.
        if r is not None:
            self.assertNotEqual(r.matched_pattern, "nit")

    def test_self_describe_picks_independent_not_lead(self):
        # Run 571 #94: seniority gradient. OPT2 (independent IC) is the
        # truthful pick at 3y; OPT3 (guide architecture + review others)
        # claims lead-level work and must never win.
        from naukri_agent.core.models import ScreeningQuestion

        opts = [
            "I need detailed implementation instructions and regular technical guidance.",
            "I own individual tasks but need help planning changes across the stack.",
            "I independently plan, build, test, and deploy features across the stack.",
            "I independently deliver features and also guide architecture and review other engineers work.",
        ]
        eng = self._eng("Full Stack / Web Developer")
        r = eng.resolve(ScreeningQuestion(
            text="Which best describes your experience independently delivering full stack features?",
            kind="radio", options=opts))
        self.assertIsNotNone(r)
        self.assertEqual(r.value, opts[2])

    def test_self_describe_unknown_skill_stays_review(self):
        from naukri_agent.core.models import ScreeningQuestion

        opts = ["Beginner.", "I independently deliver and guide architecture."]
        r = self._eng("Full Stack / Web Developer").resolve(ScreeningQuestion(
            text="Which best describes your experience with COBOL mainframes?",
            kind="radio", options=opts))
        self.assertIsNone(r)

    def test_self_describe_lead_only_stays_review(self):
        # Every option claims lead work: answering any of them is a lie.
        from naukri_agent.core.models import ScreeningQuestion

        r = self._eng("Full Stack / Web Developer").resolve(ScreeningQuestion(
            text="Which best describes your experience leading enterprise architecture?",
            kind="radio", options=[
                "I set org-wide architecture and mentor principal engineers.",
                "I guide architecture and review other engineers work."]))
        self.assertIsNone(r)

    def test_run572_kb_patterns(self):
        """Run 572 #95-118: all new KB patterns resolve correctly."""
        from naukri_agent.core.models import ScreeningQuestion

        eng = self._eng("AI / Python Engineer")
        cases = [
            ("Which best describes your experience independently delivering full stack features?",
             "radio",
             ["I need detailed implementation instructions and regular technical guidance.",
              "I own individual tasks but need help planning changes across the stack.",
              "I independently plan, build, test, and deploy features across the stack.",
              "I independently deliver features and also guide architecture and review other engineers work."],
             "I independently plan, build, test, and deploy features across the stack."),
            ("How many years of work experience do you have with FastAPI?", "text", [], "2"),
            ("How many years of experience do you have shipping LLM or RAG features to production?", "text", [], "2"),
            ("Have you built retrieval using embeddings and vector search (pgvector, Pinecone, Weaviate, Qdrant, etc.) in a production system?",
             "radio", ["Yes", "No"], "Yes"),
            ("Have you designed an offline evaluation set or LLM-as-judge scoring to measure chat or retrieval quality?",
             "radio", ["Yes", "No"], "Yes"),
            ("Have you implemented tool/function calling and streaming responses (SSE or WebSockets) with an LLM API?",
             "radio", ["Yes", "No"], "Yes"),
            ("Are you comfortable with a salary range of 5-8 LPA?", "radio", ["Yes", "No"], "Yes"),
            ("Do you have at least 5.5 years of professional, hands-on Python software development experience, with Python as a language you have personally used to build and maintain software?",
             "text", [], "No"),
            ("Have you personally designed, coded, tested and deployed production-grade Python applications, backend services or APIs—not just notebooks, scripts for analysis, or proof-of-concept AI models?",
             "radio", ["Yes", "No"], "Yes"),
            ("Have you personally investigated a production incident, identified its root cause and implemented a permanent technical fix?",
             "radio", ["Yes", "No"], "Yes"),
            ("Have you used Git-based version control, code reviews and automated unit/integration testing as part of a professional software delivery process?",
             "radio", ["Yes", "No"], "Yes"),
            ("Have you personally worked with CI/CD pipelines to test, package, deploy or release software into production?",
             "radio", ["Yes", "No"], "Yes"),
            ("Have you used Infrastructure as Code (for example, Terraform, AWS CloudFormation or AWS CDK) to provision or manage infrastructure in a real project?",
             "radio", ["Yes", "No"], "No"),
        ]
        for text, kind, opts, expected in cases:
            r = eng.resolve(ScreeningQuestion(text=text, kind=kind, options=opts))
            self.assertIsNotNone(r, f"unanswered: {text[:80]}")
            self.assertEqual(r.value, expected, f"wrong answer for: {text[:80]}")

    def test_linkedin_field_length_truncates(self):
        """LinkedIn overlong answers must truncate, not return empty."""
        from naukri_agent.platforms.linkedin import _fit_field_length

        self.assertEqual(_fit_field_length("short", 10), "short")
        self.assertEqual(_fit_field_length("this is too long", 10), "this is to")
        self.assertEqual(_fit_field_length("exact10chr", 10), "exact10chr")
        self.assertEqual(_fit_field_length("no limit", None), "no limit")


class TestLinkedInFieldGuards(unittest.TestCase):
    """Q#40: typo'd/singular numeric labels fell through to the 140-char
    generic bio and died on maxlength validation."""

    def test_numeric_cue_variants(self):
        from naukri_agent.platforms.linkedin import _numeric_label_cue

        for label in ["How many years of exp in RAG?",
                      "What is your overal year of exp?",
                      "Yoe in building Production AI Agent applications?",
                      "YOE in MLops",
                      "Total yr of experience", "Notice period (days)",
                      "Current CTC (LPA)"]:
            self.assertTrue(_numeric_label_cue(label.lower()), label)
        for label in ["Area of expertise", "Expected joining bonus",
                      "Describe yourself", "Primary skill"]:
            self.assertFalse(_numeric_label_cue(label.lower()), label)

    def test_field_length_fit(self):
        from naukri_agent.platforms.linkedin import _fit_field_length

        self.assertEqual(_fit_field_length("2", 20), "2")
        self.assertEqual(_fit_field_length("Bengaluru", None), "Bengaluru")
        self.assertEqual(_fit_field_length("x" * 140, 20), "x" * 20)
        self.assertEqual(_fit_field_length("x" * 20, 20), "x" * 20)


class TestLinkedInVerifySubmit(unittest.IsolatedAsyncioTestCase):
    """Post-submit sweep: any confirmation signal counts; silence doesn't."""

    def _plat(self):
        from unittest.mock import MagicMock

        from naukri_agent.platforms.linkedin import LinkedInPlatform

        plat = LinkedInPlatform.__new__(LinkedInPlatform)
        plat.page = MagicMock()
        return plat

    async def _verify(self, first_visible_map):
        from unittest.mock import AsyncMock, patch

        from naukri_agent.platforms import linkedin as li_mod

        plat = self._plat()

        async def fake_first_visible(scope, sels, timeout_ms=6000):
            key = tuple(sels)
            return first_visible_map.get(key)

        job = MagicMock(job_id="li-1")
        with patch.object(li_mod, "first_visible", new=AsyncMock(side_effect=fake_first_visible)), \
             patch.object(li_mod, "click_if_present", new=AsyncMock(return_value=False)), \
             patch.object(li_mod, "human_pause", new=AsyncMock()):
            return await plat._verify_submit(MagicMock(), job)

    def _sentinel(self, name):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.__str__ = lambda s: name
        return m

    async def test_confirmation_counts(self):
        from unittest.mock import MagicMock

        marker = MagicMock()
        m = await self._verify({
            ("div[data-test-modal]:has-text('Application sent')",
             "div[role='dialog']:has-text('Application sent')",
             "h3:has-text('Application sent')",
             "h2:has-text('Application sent')",
             "span:has-text('Application sent')",
             "div:has-text('Your application was sent')",
             "button:has-text('Done')",
             "button[aria-label='Done']"): marker,
        })
        self.assertTrue(m)

    async def test_closed_modal_counts(self):
        # No confirmation, no badge, modal gone == submitted.
        m = await self._verify({})
        self.assertTrue(m)

    async def test_open_modal_no_markers_fails(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.platforms import linkedin as li_mod

        modal = MagicMock()
        plat = self._plat()
        job = MagicMock(job_id="li-1")

        async def fake_first_visible(scope, sels, timeout_ms=6000):
            joined = " ".join(sels)
            if "Application sent" in joined and "dialog" in joined:
                return None
            if "Application submitted" in joined or "'Applied'" in joined:
                return None
            return modal  # modal container still present

        with patch.object(li_mod, "first_visible", new=AsyncMock(side_effect=fake_first_visible)), \
             patch.object(li_mod, "click_if_present", new=AsyncMock(return_value=False)), \
             patch.object(li_mod, "human_pause", new=AsyncMock()):
            self.assertFalse(await plat._verify_submit(modal, job))


class TestWalkinByTitle(unittest.IsolatedAsyncioTestCase):
    """Run 526: 'Walk-in || Python Software Developer' failed classification
    because the tab title lacked the marker. The posting title carries it
    unambiguously — trust it before failing."""

    def _engine(self, page):
        from unittest.mock import AsyncMock, MagicMock

        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.naukri.apply import ApplyEngine

        artifacts = MagicMock()
        artifacts.screenshot = AsyncMock(return_value="/tmp/x.png")
        artifacts.capture_failure = AsyncMock(return_value="/tmp/y.png")
        return ApplyEngine(page, MagicMock(), artifacts,
                           policy=RunPolicy(dry_run=False, side_effects_enabled=True))

    async def _classify(self, title):
        from unittest.mock import AsyncMock, MagicMock, patch

        from naukri_agent.core.models import Job
        from naukri_agent.naukri import apply as apply_mod

        page = MagicMock()
        page.goto = AsyncMock()
        page.url = "https://www.naukri.com/job-listings-1"
        engine = self._engine(page)
        job = Job(job_id="reco-w", title=title, company="C",
                  url="https://www.naukri.com/job-listings-1")
        with patch.object(apply_mod, "first_visible", new=AsyncMock(return_value=None)), \
             patch.object(apply_mod, "dismiss_overlays", new=AsyncMock()), \
             patch.object(apply_mod, "human_pause", new=AsyncMock()), \
             patch.object(engine, "_state", new=AsyncMock(return_value="unknown")):
            return await engine.apply(job, "P")

    async def test_walkin_title_skips_clean(self):
        from naukri_agent.core.models import ApplicationStatus, SkipReason

        out = await self._classify("Walk-in || Python Software Developer")
        self.assertEqual(out.status, ApplicationStatus.SKIPPED)
        self.assertEqual(out.reason, SkipReason.WALKIN)

    async def test_plain_title_still_fails_closed(self):
        from naukri_agent.core.models import ApplicationStatus

        out = await self._classify("Software Engineer")
        self.assertEqual(out.status, ApplicationStatus.FAILED)
        self.assertIn("not resolvable", out.detail)


class TestCityAndBasedQuestions(unittest.TestCase):
    """Q#39/45/46/47: city boxes get cities, based-in questions get Yes —
    never a bare Yes typed into a city field."""

    def _eng(self):
        from naukri_agent.core.answers import AnswerEngine

        cfg = AgentConfig.load()
        return AnswerEngine(kb=[(k, v) for k, v in cfg.answers.items()],
                            profile_answers={}, strict=True)

    def test_city_select_resolves_bengaluru(self):
        from naukri_agent.core.models import ScreeningQuestion

        eng = self._eng()
        for opts in ([], ["Bengaluru", "Pune", "Mumbai"]):
            r = eng.resolve(ScreeningQuestion(
                text="Please select the city you are currently residing or willing to relocate to",
                kind="text", options=list(opts)))
            self.assertIsNotNone(r, opts)
            self.assertEqual(r.value, "Bengaluru", opts)
        # Home city absent: willingness fallback picks first non-negative
        # option (documented behavior — user is relocate-willing broadly).
        r = eng.resolve(ScreeningQuestion(
            text="Please select the city you are currently residing or willing to relocate to",
            kind="text", options=["Pune", "Mumbai"]))
        self.assertIsNotNone(r)
        self.assertEqual(r.value, "Pune")

    def test_based_in_bengaluru_yes_mumbai_never(self):
        from naukri_agent.core.models import ScreeningQuestion

        eng = self._eng()
        r = eng.resolve(ScreeningQuestion(
            text="Are you based in Bengaluru?", kind="radio",
            options=["Yes", "No", "Skip this question"]))
        self.assertIsNotNone(r)
        self.assertEqual(r.value, "Yes")
        # Mumbai must NOT resolve Yes (would be a lie). Since run 569 #73 it
        # resolves No — the truthful Yes/No answer for a known-foreign hub —
        # instead of burning the application in review.
        r2 = eng.resolve(ScreeningQuestion(
            text="Are you based in Mumbai?", kind="radio",
            options=["Yes", "No"]))
        self.assertIsNotNone(r2)
        self.assertEqual(r2.value, "No")


class TestCutshortTextareaAttribution(unittest.IsolatedAsyncioTestCase):
    """Run 539: a compensation label from a NEIGHBORING question leaked into
    a demo-testing box, pasting CTC boilerplate as its answer."""

    def _bot(self):
        from unittest.mock import MagicMock

        from naukri_agent.core.answers import AnswerEngine
        from naukri_agent.platforms.cutshort import CutshortChatbot

        answers = AnswerEngine(
            kb=[],
            profile_answers={"current ctc": "4", "expected ctc": "7",
                             "notice period": "0 days"},
            strict=True,
        )
        return CutshortChatbot(MagicMock(), answers, max_questions=1)

    def _form_page(self):
        from unittest.mock import AsyncMock, MagicMock

        ta = MagicMock()
        ta.is_visible = AsyncMock(return_value=True)
        ta.input_value = AsyncMock(return_value="")
        ta.fill = AsyncMock()
        ta.dispatch_event = AsyncMock()
        ta.scroll_into_view_if_needed = AsyncMock()

        label_first = MagicMock()
        label_first.count = AsyncMock(return_value=1)
        li_inner = MagicMock()
        li_inner.count = AsyncMock(return_value=1)
        li_inner.count = AsyncMock(return_value=1)

        def _ta_locator(sel):
            if sel == "xpath=ancestor::li[1]":
                return MagicMock(first=li_inner)
            if "label" in sel:
                return MagicMock(first=label_first)
            empty = MagicMock()
            empty.count = AsyncMock(return_value=0)
            empty.all = AsyncMock(return_value=[])
            empty.first = MagicMock()
            return empty

        ta.locator = MagicMock(side_effect=_ta_locator)

        form = MagicMock()
        form.count = AsyncMock(return_value=1)
        form.is_visible = AsyncMock(return_value=True)
        form.first = form
        form.locator = MagicMock(side_effect=lambda sel: MagicMock(
            all=AsyncMock(return_value=[ta]),
            count=AsyncMock(return_value=1),
            first=ta,
        ) if "textarea" in sel else MagicMock(
            all=AsyncMock(return_value=[]),
            count=AsyncMock(return_value=0),
            first=MagicMock(),
        ))

        page = MagicMock()
        page.locator = MagicMock(side_effect=lambda sel: form if sel.startswith("form:has") else MagicMock(
            all=AsyncMock(return_value=[]),
            count=AsyncMock(return_value=0),
            first=MagicMock(),
        ))
        return page, ta, label_first, li_inner

    async def _run_with_texts(self, label_text, li_text):
        from unittest.mock import AsyncMock, patch

        from naukri_agent.platforms import cutshort as cs_mod

        page, ta, label_first, li_inner = self._form_page()

        async def fake_safe_text(loc):
            if loc is label_first:
                return label_text
            if loc is li_inner:
                return li_text
            return ""

        bot = self._bot()
        bot.page = page
        with patch.object(cs_mod, "safe_text", new=AsyncMock(side_effect=fake_safe_text)), \
             patch.object(cs_mod, "human_pause", new=AsyncMock()), \
             patch.object(cs_mod, "first_visible", new=AsyncMock(return_value=None)):
            await bot.run()
        return bot, ta

    async def test_neighbor_label_discarded_and_queued(self):
        bot, ta = await self._run_with_texts(
            "What compensation do you expect, with currency and pay period?",
            "What can break in your demo? How did you test loading states?",
        )
        ta.fill.assert_not_called()
        self.assertEqual(len(bot.unanswered), 1)
        self.assertIn("demo", bot.unanswered[0]["text"].lower())
        self.assertEqual(bot.unanswered[0]["kind"], "text")

    async def test_own_label_still_fills(self):
        bot, ta = await self._run_with_texts(
            "What compensation do you expect, with currency and pay period?",
            "What compensation do you expect, with currency and pay period? CTC range here.",
        )
        ta.fill.assert_called_once()
        filled = ta.fill.await_args.args[0]
        self.assertIn("Current CTC: 4", filled)
        self.assertEqual(bot.unanswered, [])

    def test_primary_topic_window(self):
        from naukri_agent.platforms.cutshort import _primary_topic

        self.assertTrue(_primary_topic("What compensation do you expect?", ["compensation"]))
        self.assertTrue(_primary_topic("Phone number for contact", ["phone"]))
        # Buried past the window: attribution noise, not a pay question.
        self.assertFalse(_primary_topic("x" * 200 + " compensation", ["compensation"]))
        self.assertFalse(_primary_topic("Describe your demo", ["ctc", "salary"]))


class TestWalkinDetection(unittest.IsolatedAsyncioTestCase):
    """Run 498: a walk-in page recorded FAILED (no apply machinery found).
    Walk-ins must classify as clean SKIPPED under skip_walkin policy."""

    def _engine(self, title, body):
        from unittest.mock import AsyncMock, MagicMock

        from naukri_agent.core.run_policy import RunPolicy
        from naukri_agent.naukri.apply import ApplyEngine

        node = MagicMock()
        node.inner_text = AsyncMock(return_value=body)
        loc = MagicMock()
        loc.first = node
        page = MagicMock()
        page.title = AsyncMock(return_value=title)
        page.locator = MagicMock(return_value=loc)
        return ApplyEngine(page, MagicMock(), MagicMock(),
                           policy=RunPolicy(dry_run=True, side_effects_enabled=False))

    async def test_walkin_title_detected(self):
        engine = self._engine("Walk-in || Urgent Hiring | Azure Data Engineer", "")
        self.assertTrue(await engine._is_walkin_page())

    async def test_walkin_body_with_venue_detected(self):
        engine = self._engine(
            "Azure Data Engineer",
            "Walk in interview on Saturday. Venue: Whitefield office. Bring along resume.",
        )
        self.assertTrue(await engine._is_walkin_page())

    async def test_prose_mention_not_walkin(self):
        engine = self._engine(
            "Software Engineer",
            "You should be willing to walk in with new ideas every day. Remote role.",
        )
        self.assertFalse(await engine._is_walkin_page())

    async def test_plain_page_not_walkin(self):
        engine = self._engine("Software Engineer", "Apply now for this great role.")
        self.assertFalse(await engine._is_walkin_page())


class TestExperienceGates(unittest.TestCase):
    def _engine(self, profile_name="AI / Python Engineer", years=2.5):
        from naukri_agent.core.ranking import CandidateProfile

        cfg = AgentConfig.load()
        prof = next(p for p in cfg.profiles if profile_name in p.name)
        cand = CandidateProfile(
            title_keywords=[], core_skills=[], secondary_skills=[],
            target_experience_years=years,
        )
        return FilterEngine(prof.filters_for("recommended"), candidate=cand)

    def _job(self, title, company="C", min_exp=None, max_exp=None):
        return Job(
            job_id="t-%s" % title[:10], title=title, company=company,
            location="Bengaluru", url="http://t", platform="naukri",
            min_experience=min_exp, max_experience=max_exp,
        )

    def test_fresher_only_band_rejected(self):
        """0-0 fresher requisitions reject at 2y+ tenure (run 424)."""
        engine = self._engine()
        d = engine.evaluate_card(self._job("AI Engineer", min_exp=0.0, max_exp=0.0))
        self.assertFalse(d.passed)
        self.assertEqual(d.reason.value, "filter_experience")

    def test_narrow_bands_stay_eligible(self):
        """0-1/0-2 bands stay eligible — only pure 0-0 is cut."""
        engine = self._engine()
        self.assertTrue(engine.evaluate_card(self._job("AI Engineer", min_exp=0.0, max_exp=1.0)).passed)
        self.assertTrue(engine.evaluate_card(self._job("AI Engineer", min_exp=0.0, max_exp=2.0)).passed)

    def test_bare_net_blocked_tech_adjacent(self):
        """'TypeScript NET' is .NET (run 391 Neu Edge miss)."""
        engine = self._engine("Full Stack / Web Developer", years=3.0)
        d = engine.evaluate_card(self._job("Full Stack Engineer React Angular TypeScript NET"))
        self.assertFalse(d.passed)

    def test_net_company_not_blocked(self):
        """A company literally named 'Net Solutions' must not trip the
        dotnet company gate — the NET rule is tech-adjacent only."""
        engine = self._engine("Full Stack / Web Developer", years=3.0)
        d = engine.evaluate_card(self._job("Software Engineer", company="Net Solutions"))
        self.assertTrue(d.passed, f"wrongly blocked: {d.detail}")

    def test_impute_seniority(self):
        """Missing exp fills from title markers for every platform (run 424:
        SDE-4/Sr Lead applied with no stated range)."""
        from naukri_agent.core.filters import impute_seniority

        j = self._job("SDE 4 - Backend")
        self.assertTrue(impute_seniority(j))
        self.assertEqual((j.min_experience, j.max_experience), (5.0, 10.0))
        self.assertTrue(j.experience_imputed)


class TestProfileShadowing(unittest.TestCase):
    """Config promises profile answers win over global ones. Without
    shadowing, longer global phrasings outscored corrected profile entries
    (AI profile answered '2 years machine learning' with 0 ML background)."""

    def _engine(self, profile_name):
        from naukri_agent.core.answers import AnswerEngine

        cfg = AgentConfig.load()
        prof = next(p for p in cfg.profiles if profile_name in p.name)
        return AnswerEngine(
            kb=[(k, v) for k, v in cfg.answers.items()],
            profile_answers=dict(prof.answers), strict=True)

    def test_ml_answers_honest_on_ai_profile(self):
        from naukri_agent.core.models import ScreeningQuestion

        eng = self._engine("AI / Python Engineer")
        res = eng.resolve(ScreeningQuestion(
            text="Do you have experience in machine learning?",
            kind="radio", options=["Yes", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")

    def test_ml_numeric_without_honest_option_reviews(self):
        """A '0' that fits no numeric option must route to review — never
        fall back to the shadowed global '2'."""
        from naukri_agent.core.models import ScreeningQuestion

        eng = self._engine("AI / Python Engineer")
        res = eng.resolve(ScreeningQuestion(
            text="How many years of experience do you have in machine learning?",
            kind="radio", options=["2", "3"]))
        self.assertIsNone(res)

    def test_relevant_experience_uses_profile_value(self):
        from naukri_agent.core.models import ScreeningQuestion

        eng = self._engine("AI / Python Engineer")
        res = eng.resolve(ScreeningQuestion(
            text="What is your relevant experience?",
            kind="radio", options=["2", "3"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "2")

    def test_unrelated_globals_survive(self):
        from naukri_agent.core.models import ScreeningQuestion

        eng = self._engine("AI / Python Engineer")
        res = eng.resolve(ScreeningQuestion(
            text="What is your notice period?", kind="text"))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "0 days")


class TestRunSummaryFormat(unittest.TestCase):
    def test_no_job_lists_all_errors_shown(self):
        from naukri_agent.core.models import RunStats
        from naukri_agent.notify.notifier import format_run_summary

        stats = RunStats()
        stats.applied = 3
        stats.errors = ["boom x", "boom x", "other y"]
        stats.applied_jobs = [{"platform": "Naukri", "title": "T", "company": "C"}]
        stats.external_jobs = [{"platform": "Naukri", "title": "E", "company": "C"}]
        body = format_run_summary(stats, duration_s=65, run_id=1)
        self.assertNotIn("APPLIED POSITIONS", body)
        self.assertNotIn("ACTION REQUIRED", body)
        self.assertIn("boom x (×2)", body)
        self.assertIn("other y", body)


class TestCompletenessDiscount(unittest.TestCase):
    """Unknown fields sink below stated fits (ordering only, never a
    reject): stated-good > stated-close > unknown > stated-bad."""

    def _ranker(self):
        from naukri_agent.core.ranking import CandidateProfile, RankingEngine

        cand = CandidateProfile(
            title_keywords=[], core_skills=["python"],
            secondary_skills=[], target_experience_years=3.0,
            min_acceptable_salary_lpa=5.0,
        )
        return RankingEngine(cand)

    def _job(self, **kw):
        base = dict(job_id="t", title="Python Developer", company="C",
                    location="Bengaluru", url="http://t", platform="naukri")
        base.update(kw)
        return Job(**base)

    def test_unknown_exp_below_close_fit(self):
        eng = self._ranker()
        stated, _ = eng._score_experience(self._job(min_experience=2.0, max_experience=4.0))
        unknown, reason = eng._score_experience(self._job())
        self.assertLess(unknown, stated)
        self.assertIn("undisclosed", reason)

    def test_unknown_date_below_dated(self):
        eng = self._ranker()
        fresh, _ = eng._score_freshness(self._job(posted_days_ago=0))
        unknown, _ = eng._score_freshness(self._job())
        self.assertLess(unknown, fresh)

    def test_unknown_salary_and_rating_below_stated(self):
        eng = self._ranker()
        sal_open, _ = eng._score_salary(self._job())
        sal_met, _ = eng._score_salary(self._job(min_salary_lpa=7.0))
        self.assertLess(sal_open, sal_met)
        rate_open, _ = eng._score_rating(self._job())
        rate_good, _ = eng._score_rating(self._job(rating=4.2))
        self.assertLess(rate_open, rate_good)

class TestImputationEdges(unittest.TestCase):
    """Tail of the imputation suite: stated numbers skip filling, levels
    behave, and imputed seniors then fail the ceiling gate."""

    def _engine(self, profile_name="AI / Python Engineer", years=2.5):
        from naukri_agent.core.ranking import CandidateProfile

        cfg = AgentConfig.load()
        prof = next(p for p in cfg.profiles if profile_name in p.name)
        cand = CandidateProfile(
            title_keywords=[], core_skills=[], secondary_skills=[],
            target_experience_years=years,
        )
        return FilterEngine(prof.filters_for("recommended"), candidate=cand)

    def _job(self, title, company="C", min_exp=None, max_exp=None):
        return Job(
            job_id="t-%s" % title[:10], title=title, company=company,
            location="Bengaluru", url="http://t", platform="naukri",
            min_experience=min_exp, max_experience=max_exp,
        )

    def test_stated_numbers_skip_imputation(self):
        from naukri_agent.core.filters import impute_seniority

        stated = self._job("SDE 4 - Backend", min_exp=2.0, max_exp=7.0)
        self.assertFalse(impute_seniority(stated))

        junior_band = self._job("SDE 2 - Backend")
        self.assertFalse(impute_seniority(junior_band))

        roman = self._job("SDE - III @ Arintra")
        self.assertTrue(impute_seniority(roman))

        plain = self._job("Software Engineer")
        self.assertFalse(impute_seniority(plain))
        self.assertIsNone(plain.min_experience)

    def test_imputed_senior_rejected_by_ceiling(self):
        """Imputed 5-10y seniority must then fail the 3.5y ceiling gate."""
        from naukri_agent.core.filters import impute_seniority

        engine = self._engine()
        j = self._job("Sr. Lead AI Engineer")
        self.assertTrue(impute_seniority(j))
        d = engine.evaluate_card(j)
        self.assertFalse(d.passed)


class TestCtcRangePick(unittest.TestCase):
    def test_parse_bands(self):
        from naukri_agent.platforms.linkedin import _parse_lpa_range

        self.assertEqual(_parse_lpa_range("4-6 LPA"), (4.0, 6.0))
        self.assertEqual(_parse_lpa_range("3 to 5"), (3.0, 5.0))
        self.assertEqual(_parse_lpa_range("Rs 4,00,000 - 7,00,000"), (4.0, 7.0))
        self.assertEqual(_parse_lpa_range("Yes"), None)
        self.assertEqual(_parse_lpa_range("Competitive"), None)

    def test_containment_only(self):
        """Only the band containing OUR number is picked (run 441: Current
        CTC ranges). Nearest-above/below would misstate pay."""
        from naukri_agent.platforms.linkedin import _pick_ctc_option

        opts = ["0-3 LPA", "3-6 LPA", "6-10 LPA"]
        self.assertEqual(_pick_ctc_option(opts, 3.9), "3-6 LPA")
        self.assertEqual(_pick_ctc_option(opts, 7.5), "6-10 LPA")
        self.assertIsNone(_pick_ctc_option(opts, 25.0))
        self.assertIsNone(_pick_ctc_option(["Yes", "No"], 4.0))
        self.assertIsNone(_pick_ctc_option(opts, None))


class TestSpacedTitleVariants(unittest.TestCase):
    """Spaced 'Front End'/'Back End' must pass like 'Frontend'/'Backend'.

    Oct 2026 corpus audit (4,502 Naukri rows): 14 good titles died on the
    missing spaced variants; hyphenated forms already passed via tech-norm.
    """

    def setUp(self):
        self.cfg = AgentConfig.load()
        self.fs_profile = next(p for p in self.cfg.profiles if "Full Stack" in p.name)
        self.ai_profile = next(p for p in self.cfg.profiles if "AI" in p.name)

    def _job(self, title):
        return Job(
            job_id=f"test-spaced-{title}",
            title=title,
            company="Test Corp",
            location="Bengaluru",
            url="http://test.com",
            platform="naukri",
        )

    def test_spaced_variants_pass_both_profiles(self):
        for prof in (self.fs_profile, self.ai_profile):
            engine = FilterEngine(prof.filters_for("recommended"))
            for title in [
                "Front End Developer",
                "Back End Developer",
                "Application Developer",
                "Database Engineer",
            ]:
                decision = engine.evaluate_card(self._job(title))
                self.assertTrue(
                    decision.passed,
                    f"{prof.name}: {title!r} should pass, got {decision.detail}",
                )


class TestPhantomSalaryGuard(unittest.TestCase):
    """Sub-1.0 LPA figures are parser hallucinations, not offers.

    Oct 2026 audit: 28 Wellfound rows (incl. 'ML / RAG / Inference Engineer')
    died on 0.00-0.30 LPA mined from experience bands ('0-1 Yrs') or equity
    ('0.01%') with empty salary text. Unknown never rejects; the 5 LPA floor
    still holds for real numbers.
    """

    def setUp(self):
        self.cfg = AgentConfig.load()
        self.fs_profile = next(p for p in self.cfg.profiles if "Full Stack" in p.name)

    def _job(self, salary):
        return Job(
            job_id="test-phantom-sal",
            title="Full Stack Developer",
            company="Test Corp",
            location="Bengaluru",
            url="http://test.com",
            platform="wellfound",
            min_salary_lpa=salary,
            max_salary_lpa=None,
        )

    def test_phantom_salaries_pass(self):
        engine = FilterEngine(self.fs_profile.filters_for("recommended"))
        for phantom in (0.0, 0.01, 0.2, 0.3, 0.99):
            decision = engine.evaluate_card(self._job(phantom))
            self.assertTrue(
                decision.passed,
                f"{phantom} LPA should read as undisclosed, got {decision.detail}",
            )

    def test_real_low_salary_still_rejected(self):
        engine = FilterEngine(self.fs_profile.filters_for("recommended"))
        decision = engine.evaluate_card(self._job(4.0))
        self.assertFalse(decision.passed)
        self.assertIn("LPA", decision.detail)


class TestWellfoundPaySignal(unittest.TestCase):
    def test_no_signal_no_numbers(self):
        from naukri_agent.platforms.wellfound import _has_pay_signal

        self.assertFalse(_has_pay_signal("Full Stack Developer (React / Node.js) - 0-1 Yrs"))
        self.assertFalse(_has_pay_signal("AI Backend Engineer 0.01% equity Remote"))
        self.assertFalse(_has_pay_signal(""))
        self.assertTrue(_has_pay_signal("₹12-18 Lacs PA Bangalore"))
        self.assertTrue(_has_pay_signal("Salary: 10-15 LPA"))
        self.assertTrue(_has_pay_signal("CTC 8L"))


class TestQuarantineGeneralization(unittest.TestCase):
    def _eng(self):
        from naukri_agent.core.answers import AnswerEngine

        cfg = AgentConfig.load()
        return AnswerEngine(kb=[(k, v) for k, v in cfg.answers.items()],
                            profile_answers={}, strict=True)

    def test_non_cutshort_phrasing_quarantined(self):
        """'Are you currently in Pune?' quarantines the here-claim and the
        apply proceeds via the truthful relocate option — interview kept."""
        from naukri_agent.core.answers import _job_place_is_home

        self.assertFalse(_job_place_is_home("are you currently in pune?"))
        self.assertTrue(_job_place_is_home("are you currently in bengaluru?"))
        self.assertTrue(_job_place_is_home("are you willing to relocate?"))

    def test_pune_question_resolves_relocate(self):
        from naukri_agent.core.models import ScreeningQuestion

        eng = self._eng()
        res = eng.resolve(ScreeningQuestion(
            text="Are you currently in Pune? Are you okay with this?",
            kind="radio",
            options=["I am currently in this location and okay with it",
                     "I am not in this location but can relocate", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("relocate", res.value.lower())


class TestDegreeGuard(unittest.TestCase):
    def _eng(self):
        from naukri_agent.core.answers import AnswerEngine

        cfg = AgentConfig.load()
        return AnswerEngine(kb=[(k, v) for k, v in cfg.answers.items()],
                            profile_answers={}, strict=True)

    def test_master_level_resolves_no(self):
        from naukri_agent.core.models import ScreeningQuestion

        eng = self._eng()
        for text in ["Have you completed your MBA?",
                     "Have you completed your Masters degree?"]:
            res = eng.resolve(ScreeningQuestion(
                text=text, kind="radio", options=["Yes", "No"]))
            self.assertIsNotNone(res)
            assert res is not None
            self.assertEqual(res.value, "No")

    def test_bachelor_and_generic_resolve_yes(self):
        from naukri_agent.core.models import ScreeningQuestion

        eng = self._eng()
        for text in ["Have you completed your B.Tech?",
                     "Have you completed your degree?"]:
            res = eng.resolve(ScreeningQuestion(
                text=text, kind="radio", options=["Yes", "No"]))
            self.assertIsNotNone(res)
            assert res is not None
            self.assertEqual(res.value, "Yes")


class TestWordBoundaryMatching(unittest.TestCase):
    def test_no_never_matches_noida(self):
        from naukri_agent.naukri.chatbot import ChatbotHandler

        m = ChatbotHandler._match_option_text
        self.assertFalse(m("Noida", "No"))
        self.assertFalse(m("Notice period 0 days", "not applicable"))
        self.assertTrue(m("Yes, willing to relocate", "Yes"))
        self.assertTrue(m("Bengaluru", "bengaluru"))
        self.assertTrue(m("3 years experience", "3 years"))


class TestGigAndStackDisjoint(unittest.TestCase):
    def _decision(self, title):
        cfg = AgentConfig.load()
        ai_profile = next(p for p in cfg.profiles if "AI" in p.name)
        engine = FilterEngine(ai_profile.filters_for("recommended"))
        return engine.evaluate_card(Job(
            job_id="t", title=title, company="C", location="Bengaluru",
            url="http://test.com", platform="naukri"))

    def test_gig_postings_blocked(self):
        for title in ["AI Engineer | $50/task", "Python Developer per task",
                      "React Developer per gig"]:
            self.assertFalse(self._decision(title).passed, title)

    def test_php_ruby_stacks_blocked(self):
        for title in ["Laravel Developer/Full Stack Developer",
                      "Ruby on Rails Developer",
                      "Senior Symfony Developer"]:
            self.assertFalse(self._decision(title).passed, title)

    def test_legit_titles_untouched(self):
        for title in ["AI Engineer", "Python Developer",
                      "Full Stack Developer", "Giganet Developer"]:
            decision = self._decision(title)
            if "Giganet" in title:
                self.assertFalse(decision.passed)
            else:
                self.assertTrue(decision.passed, f"{title}: {decision.reason}")


class TestBlockedQuestionStatic(unittest.IsolatedAsyncioTestCase):
    async def test_instance_call_contract(self):
        """Production calls self._extract_blocked_questions (bound self); the
        TypeError in runs 554/556 came from a lost staticmethod. This pins
        the call contract, not just the logic."""
        from unittest.mock import AsyncMock, MagicMock

        from naukri_agent.platforms.linkedin import LinkedInPlatform

        el = MagicMock()
        el.get_attribute = AsyncMock(return_value="How many years of experience?")
        locs = MagicMock()
        locs.all = AsyncMock(return_value=[el])
        modal = MagicMock()
        modal.locator = MagicMock(return_value=locs)
        plat = LinkedInPlatform.__new__(LinkedInPlatform)
        qs = await plat._extract_blocked_questions(modal, "Step 5 blocked")
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["kind"], "unknown")


class TestNonHiringEmailExclusion(unittest.TestCase):
    def test_compliance_inboxes_dropped(self):
        """2026-10-12: an application went to Aon's reasonable-accommodation
        inbox. Compliance/accessibility/no-reply boxes are never recruiters."""
        from naukri_agent.core.models import extract_description_metadata

        _, emails = extract_description_metadata(
            "Apply at reasonableaccommodations@aon.com or nandinib@intellicorecg.com "
            "or legal@company.com or noreply@company.com or hr@startup.io"
        )
        self.assertNotIn("reasonableaccommodations@aon.com", emails)
        self.assertNotIn("legal@company.com", emails)
        self.assertNotIn("noreply@company.com", emails)
        self.assertIn("nandinib@intellicorecg.com", emails)
        self.assertIn("hr@startup.io", emails)

    def test_extractors_share_one_list(self):
        """The two extractors must not drift apart again."""
        from naukri_agent.core import models as models_mod
        from naukri_agent.naukri import apply as apply_mod

        self.assertIs(apply_mod.NON_HIRING_EMAIL_PREFIXES, models_mod.NON_HIRING_EMAIL_PREFIXES)
        self.assertIn("reasonableaccommodation@", models_mod.NON_HIRING_EMAIL_PREFIXES)


class TestParseExperienceSingles(unittest.TestCase):
    def test_bare_singles_fail_open(self):
        """Headcount figures are not tenure: unknown flows to ranking."""
        from naukri_agent.naukri.parser import parse_experience

        self.assertEqual(parse_experience("25 applicants"), (None, None))
        self.assertEqual(parse_experience("3 openings"), (None, None))

    def test_marked_singles_still_parse(self):
        from naukri_agent.naukri.parser import parse_experience

        self.assertEqual(parse_experience("3 Years"), (3.0, 3.0))
        self.assertEqual(parse_experience("Exp: 5"), (5.0, 5.0))
        self.assertEqual(parse_experience("Fresher"), (0.0, 0.0))
        self.assertEqual(parse_experience("2-4 Yrs"), (2.0, 4.0))
        self.assertEqual(parse_experience("5+ years"), (5.0, None))


class TestHiringCafeCleanContext(unittest.TestCase):
    def test_experiment_flag_defaults_off(self):
        """The clean-room context must never activate unasked: shared-path
        behavior is byte-identical unless explicitly enabled."""
        from naukri_agent.config import BrowserConfig

        self.assertFalse(BrowserConfig().hiringcafe_clean_context)


class TestHiringCafeCleanPageContract(unittest.IsolatedAsyncioTestCase):
    async def test_requires_started_browser(self):
        from naukri_agent.browser.manager import BrowserManager
        from naukri_agent.config import BrowserConfig

        manager = BrowserManager(BrowserConfig(hiringcafe_clean_context=True))
        with self.assertRaises(RuntimeError):
            await manager.new_hiringcafe_page()


if __name__ == "__main__":
    unittest.main()



