import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_agent.config import AgentConfig
from naukri_agent.core.filters import FilterEngine
from naukri_agent.core.models import Job, SkipReason
from naukri_agent.platforms import instahyre


def target():
    return Job('instahyre-123', 'Full Stack Engineer', 'Example',
               'https://www.instahyre.com/candidate/opportunities/')


class TestInstahyreAppliedFeed(unittest.IsolatedAsyncioTestCase):
    def platform(self):
        platform = object.__new__(instahyre.InstahyrePlatform)
        platform.page = MagicMock()
        platform.artifacts = MagicMock(dump_html=AsyncMock(), capture_failure=AsyncMock())
        return platform

    async def test_experience_slider_requires_matching_label_and_successful_search(self):
        platform = self.platform()
        slider = MagicMock(evaluate=AsyncMock(return_value={"min": 0, "max": 26, "value": 26}))
        label = MagicMock(count=AsyncMock(return_value=1), inner_text=AsyncMock(return_value="2"))
        handle = MagicMock(scroll_into_view_if_needed=AsyncMock(), bounding_box=AsyncMock(
            return_value={"x": 350, "y": 292, "width": 24, "height": 24},
        ))
        track = MagicMock(bounding_box=AsyncMock(return_value={"x": 172, "width": 202}))
        slider.locator.side_effect = {".slider-value": label, ".slider-handle": handle,
                                     ".slider-track": track}.__getitem__
        response = MagicMock(ok=True, body=AsyncMock())
        async def get_response():
            return response
        event = MagicMock()
        event.__aenter__.side_effect = lambda: MagicMock(value=get_response())
        platform.page.expect_response.return_value = event
        platform.page.mouse = MagicMock(move=AsyncMock(), down=AsyncMock(), up=AsyncMock())

        self.assertTrue(await platform._set_experience_slider(slider, 2))
        matches_search = platform.page.expect_response.call_args.args[0]
        endpoint = "https://www.instahyre.com/api/v1/job_search"
        self.assertTrue(matches_search(MagicMock(url=f"{endpoint}?years=2")))
        self.assertFalse(matches_search(MagicMock(url=f"{endpoint}?years=3")))
        self.assertFalse(matches_search(MagicMock(url=endpoint)))
        label.inner_text.return_value = "All"
        self.assertFalse(await platform._set_experience_slider(slider, 2))
        label.inner_text.return_value = "2"
        response.ok = False
        self.assertFalse(await platform._set_experience_slider(slider, 2))
        self.assertEqual(platform.page.mouse.up.await_count, 3)

    async def test_exact_card_marker_can_confirm_without_second_lookup(self):
        platform = self.platform()
        card = MagicMock()
        platform._find_target_card = AsyncMock(return_value=card)
        with patch.object(instahyre, 'first_visible', new=AsyncMock(return_value=MagicMock())) as find:
            self.assertTrue(await platform._verify_target_applied(target()))
        self.assertIs(find.await_args.args[0], card)
        self.assertIn('.application-status-box .status-label:text-is(\'Applied\')', find.await_args.args[1])
        platform.page.context.new_page.assert_not_called()

    def verification(self, platform):
        locator = MagicMock()
        locator.filter.return_value = locator
        locator.first = locator
        locator.count = AsyncMock(return_value=1)
        locator.wait_for = AsyncMock()
        locator.click = AsyncMock()
        locator.fill = AsyncMock()
        locator.press_sequentially = AsyncMock()
        page = MagicMock(goto=AsyncMock(), close=AsyncMock())
        page.locator.return_value = locator
        platform.page.context.new_page = AsyncMock(return_value=page)
        return page, locator

    async def test_fresh_applied_feed_confirms_only_exact_target(self):
        platform = self.platform()
        page, locator = self.verification(platform)
        card = MagicMock()
        platform._find_target_card = AsyncMock(side_effect=[None, card])
        with patch.object(instahyre, 'first_visible', new=AsyncMock(return_value=MagicMock())):
            self.assertTrue(await platform._verify_target_applied(target()))
        self.assertEqual(platform._find_target_card.await_args.args[0].job_id, target().job_id)
        self.assertIs(platform._find_target_card.await_args.args[1], page)
        locator.press_sequentially.assert_awaited_with('Example', delay=30)
        page.close.assert_awaited_once()
        platform.page.goto.assert_not_called()
        card.click.assert_not_called()

    async def test_missing_target_cannot_use_company_popup_or_global_count(self):
        platform = self.platform()
        page, _ = self.verification(platform)
        platform._find_target_card = AsyncMock(return_value=None)
        with patch.object(instahyre, 'first_visible', new=AsyncMock(return_value=MagicMock())) as find:
            self.assertFalse(await platform._verify_target_applied(target()))
        find.assert_not_awaited()
        page.close.assert_awaited_once()

    async def test_missing_exact_status_is_unconfirmed(self):
        platform = self.platform()
        page, _ = self.verification(platform)
        platform._find_target_card = AsyncMock(side_effect=[None, MagicMock()])
        with patch.object(instahyre, 'first_visible', new=AsyncMock(return_value=None)):
            self.assertFalse(await platform._verify_target_applied(target()))
        page.close.assert_awaited_once()

    async def test_failed_lookup_closes_verification_tab_and_captures_evidence(self):
        platform = self.platform()
        page, _ = self.verification(platform)
        page.goto.side_effect = RuntimeError('network unavailable')
        platform._find_target_card = AsyncMock(return_value=None)
        self.assertFalse(await platform._verify_target_applied(target()))
        page.close.assert_awaited_once()
        platform.artifacts.capture_failure.assert_awaited_once()

    async def test_native_clear_handles_collapsed_skill_tags(self):
        platform = self.platform()
        input_box = MagicMock(fill=AsyncMock(), focus=AsyncMock(), press=AsyncMock())
        platform.page.locator.return_value.all = AsyncMock(return_value=[])
        platform.page.locator.return_value.all_inner_texts = AsyncMock(return_value=[])
        with (patch.object(instahyre, 'first_visible', new=AsyncMock(return_value=input_box)),
              patch.object(instahyre, 'human_pause', new=AsyncMock())):
            self.assertEqual(await platform._clear_skill_tags(), 0)
        self.assertEqual([call.args[0] for call in input_box.press.await_args_list], ['Control+A', 'Backspace'])
        self.assertIn('[data-value]', instahyre._SKILL_TAGS)


class TestInstahyreEligibility(unittest.TestCase):
    def test_explicit_ai_years_use_domain_tenure_instead_of_total_ceiling(self):
        config = AgentConfig.load()
        description = 'We seek an LLM Engineer with 3-5 years of relevant experience in generative AI.'
        decision = instahyre._ai_tenure_shortfall(description, config)
        self.assertEqual(decision.reason, SkipReason.FILTER_EXPERIENCE)
        self.assertIn('configured experience is 2', decision.detail)
        config.experience.skills['generative ai'] = 3
        self.assertIsNone(instahyre._ai_tenure_shortfall(description, config))

    def test_optional_or_unverified_ai_tenure_is_not_invented(self):
        config = AgentConfig.load()
        self.assertIsNone(instahyre._ai_tenure_shortfall(
            'Build RAG APIs. Preferred Qualifications: 3 years of experience in generative AI.', config))
        config.experience.skills.pop('generative ai')
        self.assertIsNone(instahyre._ai_tenure_shortfall(
            '3 years of experience in generative AI.', config))

    def test_required_foreign_backend_and_ml_libraries_block_broad_titles(self):
        config = AgentConfig.load()
        profile = config.get_unified_instahyre_profile()
        filters = FilterEngine(profile.filters)
        for description in (
            'Design distributed services using Java or Kotlin and Kafka.',
            'Proficient in ML libraries: scikit-learn, TensorFlow or PyTorch, XGBoost.',
        ):
            candidate = target()
            candidate.description = description
            self.assertFalse(filters.evaluate_detail(candidate).passed)
        candidate = target()
        candidate.description = 'Backend: Python/Java or either of them. Frontend: React and TypeScript.'
        self.assertTrue(filters.evaluate_detail(candidate).passed)

    def test_skill_controls_are_scoped_away_from_company_and_location_facets(self):
        self.assertEqual(instahyre._SKILL_INPUTS[0], '#skills-drop-select-job-search-selectized')
        self.assertNotIn('div.selectize-input input', instahyre._SKILL_INPUTS)
        self.assertNotIn('#companies-selectized', instahyre._SKILL_TAGS)
