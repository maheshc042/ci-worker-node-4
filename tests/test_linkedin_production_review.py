import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_agent.config import AgentConfig
from naukri_agent.core.answers import AnswerEngine
from naukri_agent.core.mailer import ColdEmailer
from naukri_agent.core.models import Job, ScreeningQuestion
from naukri_agent.linkedin.analyzer import campaign_profile_mismatch, is_experience_match, is_abroad_onsite, post_content, advertised_role
from naukri_agent.linkedin.campaign import _get_resume_path
from naukri_agent.platforms import linkedin


class TestLinkedInAttachmentAndFacts(unittest.TestCase):
    def test_validation_counter_does_not_change_screening_question(self):
        platform = object.__new__(linkedin.LinkedInPlatform)
        platform.answers = AnswerEngine([], profile_answers={
            "re:^are you currently based out of bangalore\\??$": "Yes",
        })
        question = ScreeningQuestion(
            "* Invalid input 0/20 0 of 20 characters Are you currently based out of Bangalore?", "text",
        )
        self.assertEqual(platform._resolve_screening(question).value, "Yes")

    def test_explicit_opening_title_is_distinct_from_resume_track(self):
        self.assertEqual(advertised_role('Position: Python Developer\nExperience: 2+ Years','AI / Python Engineer'),'Python Developer')
        self.assertEqual(advertised_role('Build FastAPI APIs','AI / Python Engineer'),'AI / Python Engineer')
    def test_ai_filename_does_not_match_fullstack_filename(self):
        self.assertTrue(linkedin.LinkedInPlatform._resume_name_matches('CV_2026.pdf', 'CV_2026'))
        self.assertFalse(linkedin.LinkedInPlatform._resume_name_matches('CV_2026_1_.pdf', 'CV_2026'))

    def test_explicit_python_role_uses_ai_resume_from_fullstack_search(self):
        platform = object.__new__(linkedin.LinkedInPlatform)
        platform.config = AgentConfig.load()
        platform.account = MagicMock(key='primary')
        _, path = platform._resume_for_role('Full Stack / Web Developer', 'Python Developer')
        self.assertEqual(Path(path).name, 'CV_Mahesh_Chitakoti_2026.pdf')
        _, path = platform._resume_for_role('Full Stack / Web Developer', 'Retail Software Engineer')
        self.assertEqual(Path(path).name, 'CV_Mahesh_Chitakoti_2026_1_.pdf')

    def test_campaign_attachment_uses_configured_track(self):
        self.assertEqual(_get_resume_path('AI / Python Engineer', Path('resumes')).name, 'CV_Mahesh_Chitakoti_2026.pdf')
        self.assertEqual(_get_resume_path('Full Stack Engineer', Path('resumes')).name, 'CV_Mahesh_Chitakoti_2026_1_.pdf')

    def test_unknown_screening_fact_is_held_for_review(self):
        platform = object.__new__(linkedin.LinkedInPlatform)
        platform.answers = AnswerEngine([])
        platform._screening_review = []
        self.assertIsNone(platform._resolve_screening(ScreeningQuestion('Years of WebdriverIO experience?', 'text')))
        self.assertEqual(len(platform._screening_review), 1)

    def test_confirmed_freelance_rate_only_answers_the_intended_job(self):
        config = AgentConfig.load()
        profile = config.active_profiles(account="primary")[0]
        platform = object.__new__(linkedin.LinkedInPlatform)
        platform.answers = AnswerEngine([], job_screening_answers=profile.job_screening_answers)
        platform._screening_review = []
        question = ScreeningQuestion("Are you comfortable with an hourly compensation of $8 USD?",
                                     "radio", ["Yes", "No"])
        self.assertEqual(platform._resolve_screening(question, job_id="linkedin-4483862172").value, "Yes")
        self.assertIsNone(platform._resolve_screening(question, job_id="linkedin-999"))

    def test_stale_location_fragment_cannot_answer_another_listing(self):
        config = AgentConfig.load()
        profile = config.active_profiles(account="primary")[0]
        engine = AnswerEngine([("chennai/coimbatore", "Yes")],
                              job_screening_answers=profile.job_screening_answers)
        text = "(Chennai/Coimbatore)* Yes No This field is required Back Next 0 notifications"
        question = ScreeningQuestion(text, "radio", ["Yes", "No"])
        self.assertEqual(engine.resolve(question, job_id="linkedin-4484934350").value, "Yes")
        self.assertIsNone(engine.resolve(question, job_id="linkedin-999"))

    def test_required_domain_years_cannot_use_total_years(self):
        config = AgentConfig.load()
        self.assertIsNotNone(campaign_profile_mismatch('Requires 3 years of experience in generative AI.', config))
        self.assertIsNone(campaign_profile_mismatch('Requires 2 years of experience in generative AI.', config))
        alternative_config = config.model_copy(deep=True)
        alternative_config.experience.skills['python'] = 3
        self.assertIsNotNone(campaign_profile_mismatch('Requires 3 years of experience in Python and generative AI.', alternative_config))
        self.assertIsNone(campaign_profile_mismatch('Requires 3 years of experience in Python or generative AI.', alternative_config))
        self.assertIsNotNone(campaign_profile_mismatch('Requires 1 year of experience in UnknownTool.', config))
        self.assertIsNone(campaign_profile_mismatch('Build Python services. Preferred Qualifications: 5 years of experience in generative AI.', config))

    def test_plain_high_tenure_is_rejected(self):
        self.assertFalse(is_experience_match('Hiring Python developers with 5 years of experience.'))

    def test_job_seeker_and_explicit_java_role_do_not_become_email_leads(self):
        config = AgentConfig.load()
        self.assertIsNotNone(campaign_profile_mismatch("I'm actively looking for a job in Python. My email is me@example.test", config))
        self.assertIsNotNone(campaign_profile_mismatch('Hiring Java Developer. Python exposure is a bonus.', config))

    def test_us_scoped_remote_is_distinct_from_worldwide_remote(self):
        self.assertIsNotNone(campaign_profile_mismatch('Hiring Gen AI Engineer\nUS Remote\nLong term', AgentConfig.load()))
        self.assertTrue(is_abroad_onsite('Location: U.S. (Remote)'))
        self.assertTrue(is_abroad_onsite('Remote - US'))
        self.assertFalse(is_abroad_onsite('Join us remotely from anywhere in India.'))
        self.assertTrue(is_abroad_onsite('Hiring an AI engineer for a remote position in US.'))
        self.assertTrue(is_abroad_onsite('United States (Remote)'))
        self.assertFalse(is_abroad_onsite('Remote India or worldwide remote.'))
        self.assertTrue(is_abroad_onsite('Job Location: Toronto, ON (required 3 days per week)'))
        self.assertTrue(is_abroad_onsite('Toronto, ON (Hybrid)'))

    def test_immediate_start_uses_verified_notice_without_generic_consent(self):
        platform = object.__new__(linkedin.LinkedInPlatform)
        platform._screening_review = []
        platform.answers = AnswerEngine([], profile_answers={"notice period": "0 days"})
        for text in ("Can you join immediately or within 1 month?",
                     "We must fill this position urgently. Can you start immediately?"):
            result = platform._resolve_screening(ScreeningQuestion(text, "radio", ["Yes", "No"]))
            self.assertEqual(result.value, "Yes")
            self.assertEqual(result.matched_pattern, "notice:immediate_start")
        other_terms = "Can you start immediately and work overnight?"
        self.assertIsNone(platform._resolve_screening(ScreeningQuestion(other_terms, "radio", ["Yes", "No"])))
        platform.answers = AnswerEngine([], profile_answers={"notice period": "60 days"})
        self.assertIsNone(platform._resolve_screening(ScreeningQuestion("Can you start immediately?", "radio", ["Yes", "No"])))

    def test_recruiter_seniority_does_not_become_job_seniority(self):
        headline = 'Senior Talent Acquisition Specialist'
        body = post_content(f'Feed post\nRecruiter Name\n{headline}\n2h\nHiring Python Developer with 2 years of experience.', headline)
        self.assertTrue(is_experience_match(body))
        self.assertFalse(is_experience_match(post_content(f'{headline}\nHiring Senior Python Developer with 5 years of experience.', headline)))

    def test_email_fallback_separates_total_from_domain_experience(self):
        mailer = ColdEmailer('candidate@example.test', 'test')
        mailer.gemini_writer.generate_email_body = MagicMock(return_value=None)
        body = mailer._generate_body('AI / Python Engineer', 'Python RAG role', 'Example')
        self.assertIn('of software engineering experience', body)
        self.assertNotIn('Over the past', body)
        self.assertIn('(Software Engineering)', body)

    def test_reviewed_email_body_is_sent_once_and_has_smtp_receipt_id(self):
        mailer = ColdEmailer('candidate@example.test', 'test')
        mailer._generate_body = MagicMock(side_effect=AssertionError('must not regenerate reviewed copy'))
        server = MagicMock()
        server.__enter__.return_value = server
        server.send_message.return_value = {}
        with tempfile.TemporaryDirectory() as folder:
            resume = Path(folder) / 'resume.pdf'
            resume.write_bytes(b'%PDF-1.4 test')
            with patch('naukri_agent.core.mailer.smtplib.SMTP_SSL', return_value=server):
                self.assertTrue(mailer.send_application('hr@example.test', 'Python Engineer', resume,
                                                       prepared_body='Reviewed application body.'))
        self.assertEqual(server.send_message.call_count, 1)
        self.assertEqual(server.send_message.call_args.args[0].get_body(preferencelist=('plain',)).get_content().strip(), 'Reviewed application body.')
        self.assertEqual(mailer.last_send_status, 'smtp_accepted')
        self.assertTrue(mailer.last_message_id)


class TestLinkedInReceiptScope(unittest.IsolatedAsyncioTestCase):
    async def test_exact_listing_reload_can_confirm_modern_paragraph_receipt(self):
        platform = object.__new__(linkedin.LinkedInPlatform)
        platform.page = MagicMock(url='https://www.linkedin.com/jobs/view/123/')
        platform.page.goto = AsyncMock()
        job = Job('linkedin-123', 'Developer', 'Example', 'https://www.linkedin.com/jobs/view/123/')
        with patch.object(linkedin, 'first_visible', new=AsyncMock(side_effect=[None, None, MagicMock()])) as find:
            self.assertTrue(await platform._verify_submit(None, job))
        platform.page.goto.assert_awaited_once_with(job.url,wait_until='domcontentloaded',timeout=25000)
        self.assertIn("main p:text-is('Application submitted')",find.await_args.args[1])

    async def test_similar_skill_wording_cannot_resolve_unknown_tenure(self):
        from naukri_agent.db.repository import Repository
        pool = AsyncMock()
        pool.fetch.return_value = [{'id':1,'question':'How many years of work experience do you have with MongoDB?', 'answer':'3','profile':'Example'}]
        pool.fetchrow.return_value = None
        repo = Repository(pool)
        with tempfile.TemporaryDirectory() as folder, patch('naukri_agent.config.PROJECT_ROOT', Path(folder)):
            await repo.queue_question_for_review('Example', 'How many years of work experience do you have with Appium?', 'text', [], job_id='linkedin-123')
        pool.execute.assert_not_awaited()
        self.assertIn('RETURNING id, times_seen', pool.fetchrow.await_args.args[0])

    async def test_human_answer_does_not_cascade_to_different_skill(self):
        from naukri_agent.db.repository import Repository
        pool = AsyncMock()
        pool.fetchrow.side_effect = [{'profile':'Example','question':'Years of experience with MongoDB?'}, None]
        pool.fetch.side_effect = [[{'id':2,'question':'Years of experience with Appium?','profile':'Example'}], []]
        await Repository(pool).resolve_review(1,'3')
        self.assertFalse(any("resolved_by = 'auto-cascade'" in call.args[0] for call in pool.execute.await_args_list))

    async def test_native_shortened_post_link_is_resolved_before_acceptance(self):
        from naukri_agent.linkedin.scraper import LinkedInHunter
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=[True, 'https://lnkd.in/p/example', None])
        page.wait_for_function = AsyncMock()
        page.keyboard.press = AsyncMock()
        copy = MagicMock()
        copy.wait_for = AsyncMock()
        copy.count = AsyncMock(return_value=1)
        copy.click = AsyncMock()
        page.get_by_text.return_value = copy
        canonical = 'https://www.linkedin.com/posts/recruiter_activity-123-example/'
        page.context.request.get = AsyncMock(return_value=MagicMock(url=canonical))
        card = MagicMock()
        menu = card.locator.return_value.first
        menu.count = AsyncMock(return_value=1)
        menu.click = AsyncMock()
        self.assertEqual(await LinkedInHunter()._copy_post_permalink(page, card), canonical)
        page.context.request.get.assert_awaited_once_with('https://lnkd.in/p/example', timeout=15000)
        page.evaluate.assert_awaited()

    async def test_post_short_link_redirecting_off_linkedin_is_rejected(self):
        from naukri_agent.linkedin.scraper import LinkedInHunter
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=[True, 'https://lnkd.in/p/example', None])
        page.wait_for_function = AsyncMock()
        page.keyboard.press = AsyncMock()
        copy = page.get_by_text.return_value
        copy.wait_for = AsyncMock()
        copy.count = AsyncMock(return_value=1)
        copy.click = AsyncMock()
        page.context.request.get = AsyncMock(return_value=MagicMock(url='https://example.test/'))
        card = MagicMock()
        card.locator.return_value.first.count = AsyncMock(return_value=1)
        card.locator.return_value.first.click = AsyncMock()
        self.assertEqual(await LinkedInHunter()._copy_post_permalink(page, card), '')

    async def test_receipt_on_other_job_is_not_accepted(self):
        platform = object.__new__(linkedin.LinkedInPlatform)
        platform.page = MagicMock(url='https://www.linkedin.com/jobs/view/999/')
        job = Job('linkedin-123', 'Developer', 'Example', 'https://www.linkedin.com/jobs/view/123/')
        with patch.object(linkedin, 'first_visible', new=AsyncMock(return_value=MagicMock())) as find:
            self.assertFalse(await platform._verify_submit(MagicMock(), job))
        find.assert_not_awaited()
