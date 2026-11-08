import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_agent.config import AgentConfig
from naukri_agent.core.filters import FilterEngine
from naukri_agent.core.models import Job, SkipReason
from naukri_agent.platforms import wellfound


def job():
    return Job('wellfound-123', 'AI Engineer', 'Example', 'https://wellfound.com/jobs/123-example')


class TestWellfoundQueuedDelivery(unittest.IsolatedAsyncioTestCase):
    def platform(self):
        platform = object.__new__(wellfound.WellfoundPlatform)
        platform.page = MagicMock(url=job().url, goto=AsyncMock())
        return platform

    async def test_persisted_undo_is_pending_not_applied(self):
        platform = self.platform()
        detail = MagicMock()
        undo = MagicMock(inner_text=AsyncMock(return_value='Undo · 4:50'))
        with patch.object(wellfound, 'first_visible', new=AsyncMock(side_effect=[detail, undo])):
            self.assertEqual(await platform._application_state(job()), ('pending', 290))
        undo.click.assert_not_called()

    async def test_wrong_job_cannot_establish_receipt(self):
        platform = self.platform()
        platform.page.url = 'https://wellfound.com/jobs/1234-other'
        with patch.object(wellfound, 'first_visible', new=AsyncMock()) as find:
            self.assertEqual(await platform._application_state(job()), ('unknown', None))
        find.assert_not_awaited()

    async def test_navigation_applied_without_target_detail_is_not_receipt(self):
        platform = self.platform()
        with patch.object(wellfound, 'first_visible', new=AsyncMock(return_value=None)):
            self.assertEqual(await platform._application_state(job()), ('unknown', None))

    async def test_active_apply_conflicts_with_applied_elsewhere(self):
        platform = self.platform()
        with patch.object(wellfound, 'first_visible', new=AsyncMock(side_effect=[MagicMock(), None, MagicMock()])) as find:
            self.assertEqual(await platform._application_state(job()), ('unknown', None))
        self.assertEqual(find.await_count, 3)

    async def test_queued_send_requires_later_reloaded_applied_state(self):
        platform = self.platform()
        platform._application_state = AsyncMock(side_effect=[('pending', 290), ('pending', 2), ('applied', None)])
        with patch.object(wellfound.asyncio, 'sleep', new=AsyncMock()) as sleep:
            self.assertEqual(await platform._verify_application(job()), 'applied')
        self.assertEqual(platform.page.goto.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertLessEqual(sleep.await_args_list[0].args[0], 30)
        platform.page.click.assert_not_called()

    async def test_cancelled_or_unavailable_send_never_becomes_applied(self):
        platform = self.platform()
        platform._application_state = AsyncMock(side_effect=[('pending', 2), ('unknown', None)])
        with patch.object(wellfound.asyncio, 'sleep', new=AsyncMock()):
            self.assertEqual(await platform._verify_application(job()), 'unknown')

    async def test_stuck_countdown_has_bounded_wait(self):
        platform = self.platform()
        platform._application_state = AsyncMock(return_value=('pending', 290))
        clock = MagicMock(time=MagicMock(side_effect=[0, 0, 331]))
        with (patch.object(wellfound.asyncio, 'get_running_loop', return_value=clock),
              patch.object(wellfound.asyncio, 'sleep', new=AsyncMock()) as sleep):
            self.assertEqual(await platform._verify_application(job()), 'pending')
        self.assertEqual(sleep.await_count, 1)
        self.assertEqual(platform.page.goto.await_count, 2)

    async def test_existing_pending_send_cannot_reach_apply_button(self):
        platform = self.platform()
        platform.policy = wellfound.RunPolicy(dry_run=False)
        platform._metrics = None
        platform._application_state = AsyncMock(return_value=('pending', 290))
        platform._verify_application = AsyncMock(return_value='pending')
        platform._read_description = AsyncMock()
        with patch.object(wellfound, 'human_pause', new=AsyncMock()):
            outcome = await platform.apply_to_job(job(), 'Example')
        self.assertEqual(outcome.reason, SkipReason.SUBMISSION_UNCONFIRMED)
        platform._read_description.assert_not_awaited()
        platform.page.click.assert_not_called()


class TestWellfoundMandatoryTraining(unittest.TestCase):
    def test_small_absolute_rupee_range_needs_pay_period(self):
        candidate = job()
        candidate.salary_text = '₹20,000 – ₹25,000'
        candidate.min_salary_lpa, candidate.max_salary_lpa = .2, .25
        self.assertTrue(wellfound._ambiguous_pay_period(candidate))
        for text in ('₹20,000 – ₹25,000 per month', '₹20,000 – ₹25,000 per annum', '₹4L – ₹12L'):
            candidate.salary_text = text
            self.assertFalse(wellfound._ambiguous_pay_period(candidate))

    def test_training_ownership_blocks_but_optional_exposure_does_not(self):
        profile = AgentConfig.load().get_unified_wellfound_profile()
        filters = FilterEngine(profile.filters)
        candidate = job()
        candidate.description = 'Own the full MLOps lifecycle: model training, evaluation, deployment and retraining.'
        self.assertFalse(filters.evaluate_detail(candidate).passed)
        candidate.description = 'Build Python APIs and RAG workflows. Optional MLOps exposure is a bonus.'
        self.assertTrue(filters.evaluate_detail(candidate).passed)
        self.assertNotIn('mlops', profile.core_skills)


class TestWellfoundStructuredExperience(unittest.IsolatedAsyncioTestCase):
    async def test_structured_metadata_experience_overrides_imputed_senior(self):
        platform = object.__new__(wellfound.WellfoundPlatform)
        platform.page = MagicMock()

        desc_node = MagicMock(inner_text=AsyncMock(return_value="Design modular POM architecture using Playwright."))
        exp_node = MagicMock(inner_text=AsyncMock(return_value="3+ years"))

        target_job = Job(
            job_id="wellfound-4953796",
            title="Senior Automation Engineer",
            company="Workong",
            url="https://wellfound.com/jobs/4953796-senior-automation-engineer",
            min_experience=5.0,
            max_experience=10.0,
            experience_imputed=True,
        )

        with patch.object(wellfound, "first_visible", new=AsyncMock(side_effect=[desc_node, exp_node])):
            self.assertTrue(await platform._read_description(target_job))

        self.assertEqual(target_job.min_experience, 3.0)
        self.assertIsNone(target_job.max_experience)
        self.assertFalse(target_job.experience_imputed)

    async def test_page_body_regex_fallback_overrides_imputed_senior(self):
        platform = object.__new__(wellfound.WellfoundPlatform)
        desc_node = MagicMock(inner_text=AsyncMock(return_value="Design modular POM architecture using Playwright."))

        main_elem = MagicMock(
            count=AsyncMock(return_value=1),
            inner_text=AsyncMock(return_value="Location\nBengaluru\nExperience\n3+ years\nSkills\nPlaywright"),
        )
        platform.page = MagicMock()
        platform.page.locator = MagicMock(return_value=MagicMock(first=main_elem))

        target_job = Job(
            job_id="wellfound-4953796",
            title="Senior Automation Engineer",
            company="Workong",
            url="https://wellfound.com/jobs/4953796-senior-automation-engineer",
            min_experience=5.0,
            max_experience=10.0,
            experience_imputed=True,
        )

        with patch.object(wellfound, "first_visible", new=AsyncMock(side_effect=[desc_node, None])):
            self.assertTrue(await platform._read_description(target_job))

        self.assertEqual(target_job.min_experience, 3.0)
        self.assertIsNone(target_job.max_experience)
        self.assertFalse(target_job.experience_imputed)

