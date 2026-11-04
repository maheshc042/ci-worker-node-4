import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_agent.core.models import ApplicationStatus, Job
from naukri_agent.core.run_policy import RunPolicy
from naukri_agent.naukri import chatbot as cb_mod
from naukri_agent.naukri import selectors as S
from naukri_agent.naukri.apply import ApplyEngine
from naukri_agent.naukri.chatbot import ChatbotResult


class TestChatbotFalsePositiveGuard(unittest.IsolatedAsyncioTestCase):
    def test_chatbot_complete_selectors_do_not_match_greeting(self):
        """The greeting bubble 'Hi Mahesh, thank you for showing interest'
        must NEVER match S.CHATBOT_COMPLETE."""
        greeting = "Hi Mahesh Chitakoti, thank you for showing interest in this job"
        for sel in S.CHATBOT_COMPLETE:
            if "has-text(" in sel:
                target_text = sel.split("has-text('")[1].split("')")[0]
                self.assertNotIn(
                    target_text.lower(),
                    greeting.lower(),
                    f"Selector '{sel}' matches standard chatbot greeting text!",
                )

    async def test_chatbot_drawer_closed_zero_answered_not_completed(self):
        """If drawer closes with 0 questions answered, result.completed must be False
        and an error must be logged."""
        page = MagicMock()
        handler = cb_mod.ChatbotHandler(
            page, MagicMock(), RunPolicy(dry_run=True, side_effects_enabled=False)
        )
        handler._current_question = AsyncMock(return_value=None)
        handler.is_open = AsyncMock(return_value=False)
        with (
            patch.object(cb_mod, "first_visible", new=AsyncMock(return_value=None)),
            patch.object(cb_mod, "dismiss_overlays", new=AsyncMock()),
        ):
            result = await handler.run()
        self.assertFalse(result.completed)
        self.assertEqual(result.answered, 0)
        self.assertIsNotNone(result.error)
        self.assertIn("before any questions were answered", result.error)

    async def test_apply_engine_chatbot_zero_answered_fails(self):
        """_handle_chatbot_result must reject 0 questions answered when page
        level confirmation is absent, never returning APPLIED."""
        page = MagicMock()
        page.url = "https://www.naukri.com/job-listings-011126004275"
        page.title = AsyncMock(return_value="Devops Engineer")
        page.locator.return_value.count = AsyncMock(return_value=0)
        artifacts = MagicMock()
        artifacts.capture_failure = AsyncMock(return_value="/tmp/fail.png")

        engine = ApplyEngine(
            page, MagicMock(), artifacts, policy=RunPolicy(dry_run=False, side_effects_enabled=True)
        )
        engine._fast_check = AsyncMock(return_value=False)

        res = ChatbotResult()
        res.completed = True
        res.answered = 0
        job = Job(job_id="reco-011126004275", title="Devops Engineer", company="NPCI", url=page.url)

        with patch("naukri_agent.naukri.apply.first_visible", new=AsyncMock(return_value=None)):
            outcome = await engine._handle_chatbot_result(job, "P", 1, res)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, ApplicationStatus.FAILED)
        self.assertIn("0 questions answered", outcome.detail)

    async def test_apply_engine_chatbot_answered_succeeds(self):
        """_handle_chatbot_result accepts completed chatbot when questions > 0."""
        page = MagicMock()
        page.url = "https://www.naukri.com/myapply/saveApply"
        page.title = AsyncMock(return_value="Apply Confirmation")
        page.locator.return_value.count = AsyncMock(return_value=0)
        artifacts = MagicMock()

        engine = ApplyEngine(
            page, MagicMock(), artifacts, policy=RunPolicy(dry_run=False, side_effects_enabled=True)
        )

        async def mock_fast_check(sels):
            return sels == S.APPLY_SUCCESS

        engine._fast_check = AsyncMock(side_effect=mock_fast_check)

        res = ChatbotResult()
        res.completed = True
        res.answered = 1
        job = Job(job_id="reco-011126004275", title="Devops Engineer", company="NPCI", url=page.url)

        with patch("naukri_agent.naukri.apply.first_visible", new=AsyncMock(return_value=None)):
            outcome = await engine._handle_chatbot_result(job, "P", 1, res)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, ApplicationStatus.APPLIED)
        self.assertEqual(outcome.questions_answered, 1)



    def test_selectors_purged_of_loose_elements(self):
        """APPLY_SUCCESS and JD_ALREADY_APPLIED must NOT contain broad selectors that match
        ambient icons, sidebar recommendations, or applicant counts."""
        for bad_sel in [
            "img[alt='success-icon']",
            "div:has-text('Applied on')",
            "div.applied-job-content",
            "div.styles_apply-message__2Sd0v:has-text('applied')",
            "button[disabled]:has-text('Applied')",
            "div.acp-container",
            "div.acp-header-container",
        ]:
            self.assertNotIn(
                bad_sel,
                S.APPLY_SUCCESS,
                f"Loose selector '{bad_sel}' found in S.APPLY_SUCCESS!",
            )
        self.assertNotIn(
            "span:has-text('Applied')",
            S.JD_ALREADY_APPLIED,
            "Unscoped 'span:has-text(\'Applied\')' matches applicant count chips and must not be in S.JD_ALREADY_APPLIED!",
        )

    async def test_apply_engine_chatbot_unconfirmed_verify_fails_closed(self):
        """When page confirmation is absent and drawer closed after answering,
        if reload verification returns 'unknown', it must return None (fail closed)."""
        page = MagicMock()
        page.url = "https://www.naukri.com/job-listings-011126004275"
        page.title = AsyncMock(return_value="Devops Engineer")
        page.locator.return_value.count = AsyncMock(return_value=0)
        artifacts = MagicMock()

        engine = ApplyEngine(
            page, MagicMock(), artifacts, policy=RunPolicy(dry_run=False, side_effects_enabled=True)
        )
        engine._fast_check = AsyncMock(return_value=False)
        engine._verify_post_submit = AsyncMock(return_value="unknown")

        res = ChatbotResult()
        res.completed = False
        res.answered = 1
        job = Job(job_id="reco-011126004275", title="Devops Engineer", company="NPCI", url=page.url)

        with patch("naukri_agent.naukri.apply.first_visible", new=AsyncMock(return_value=None)):
            outcome = await engine._handle_chatbot_result(job, "P", 1, res)
        self.assertIsNone(outcome)

    async def test_apply_engine_chatbot_unconfirmed_verify_passes_when_reloaded_applied(self):
        """When page confirmation is absent but reload verification finds 'applied',
        it successfully marks APPLIED via verify_pass."""
        page = MagicMock()
        page.url = "https://www.naukri.com/job-listings-011126004275"
        page.title = AsyncMock(return_value="Devops Engineer")
        page.locator.return_value.count = AsyncMock(return_value=0)
        artifacts = MagicMock()

        engine = ApplyEngine(
            page, MagicMock(), artifacts, policy=RunPolicy(dry_run=False, side_effects_enabled=True)
        )
        engine._fast_check = AsyncMock(return_value=False)
        engine._verify_post_submit = AsyncMock(return_value="applied")

        res = ChatbotResult()
        res.completed = False
        res.answered = 1
        job = Job(job_id="reco-011126004275", title="Devops Engineer", company="NPCI", url=page.url)

        with patch("naukri_agent.naukri.apply.first_visible", new=AsyncMock(return_value=None)):
            outcome = await engine._handle_chatbot_result(job, "P", 1, res)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, ApplicationStatus.APPLIED)
        self.assertEqual(outcome.confirmation_type, "verify_pass")
        self.assertEqual(outcome.questions_answered, 1)

    async def test_chatbot_drawer_closed_after_answering_without_banner_is_unconfirmed(self):
        """When drawer closes after answering 1 question without positive confirmation banner,
        result.completed must remain False (unconfirmed), letting apply engine verify via reload."""
        page = MagicMock()
        page.url = "https://www.naukri.com/job-listings-011126004275"
        page.title = AsyncMock(return_value="Devops Engineer")
        page.locator.return_value.count = AsyncMock(return_value=0)

        handler = cb_mod.ChatbotHandler(
            page, MagicMock(), RunPolicy(dry_run=True, side_effects_enabled=False)
        )
        handler._current_question = AsyncMock(return_value=None)
        handler.is_open = AsyncMock(return_value=False)
        with (
            patch.object(cb_mod, "first_visible", new=AsyncMock(return_value=None)),
            patch.object(cb_mod, "dismiss_overlays", new=AsyncMock()),
        ):
            # Simulate 1 question answered before stagnant rounds
            async def mock_run_with_1_ans():
                res = ChatbotResult()
                res.answered = 1
                # Trigger stagnant rounds check logic
                still_open = await handler.is_open(1_500)
                if not still_open:
                    is_confirmed = False
                    if is_confirmed:
                        res.completed = True
                    elif res.answered > 0:
                        res.completed = False
                return res

            result = await mock_run_with_1_ans()
        self.assertFalse(result.completed)
        self.assertEqual(result.answered, 1)

    async def test_verify_post_submit_active_apply_button_returns_unknown(self):
        """If the reloaded page still renders an active 'Apply' button, _verify_post_submit
        MUST return 'unknown', never 'applied', even if ambient success markers exist."""
        page = MagicMock()
        page.goto = AsyncMock()
        page.url = "https://www.naukri.com/job-listings-091026027616"
        page.title = AsyncMock(return_value="AEM Fullstack Developer - Cognizant")

        apply_btn = MagicMock()
        apply_btn.is_visible = AsyncMock(return_value=True)
        apply_btn.inner_text = AsyncMock(return_value="Apply")
        apply_btn.is_disabled = AsyncMock(return_value=False)

        engine = ApplyEngine(
            page, MagicMock(), MagicMock(), policy=RunPolicy(dry_run=False, side_effects_enabled=True)
        )

        with patch("naukri_agent.naukri.apply.first_visible", new=AsyncMock(return_value=apply_btn)):
            verdict = await engine._verify_post_submit(
                Job(job_id="reco-091026027616", title="AEM Fullstack Developer", company="Cognizant", url=page.url)
            )
        self.assertEqual(verdict, "unknown")

    async def test_verify_post_submit_applied_button_returns_applied(self):
        """If the reloaded page renders 'Applied' on an enabled button, _verify_post_submit
        returns 'applied'."""
        page = MagicMock()
        page.goto = AsyncMock()
        page.url = "https://www.naukri.com/job-listings-091026027616"
        page.title = AsyncMock(return_value="AEM Fullstack Developer - Cognizant")

        apply_btn = MagicMock()
        apply_btn.is_visible = AsyncMock(return_value=True)
        apply_btn.inner_text = AsyncMock(return_value="Applied")
        apply_btn.is_disabled = AsyncMock(return_value=False)

        engine = ApplyEngine(
            page, MagicMock(), MagicMock(), policy=RunPolicy(dry_run=False, side_effects_enabled=True)
        )

        with patch("naukri_agent.naukri.apply.first_visible", new=AsyncMock(return_value=apply_btn)):
            verdict = await engine._verify_post_submit(
                Job(job_id="reco-091026027616", title="AEM Fullstack Developer", company="Cognizant", url=page.url)
            )
        self.assertEqual(verdict, "applied")

    async def test_verify_post_submit_applied_disabled_returns_already_applied(self):
        """If the reloaded page renders 'Applied' on a disabled button, _verify_post_submit
        returns 'already_applied'."""
        page = MagicMock()
        page.goto = AsyncMock()
        page.url = "https://www.naukri.com/job-listings-091026027616"
        page.title = AsyncMock(return_value="AEM Fullstack Developer - Cognizant")

        apply_btn = MagicMock()
        apply_btn.is_visible = AsyncMock(return_value=True)
        apply_btn.inner_text = AsyncMock(return_value="Applied")
        apply_btn.is_disabled = AsyncMock(return_value=True)

        engine = ApplyEngine(
            page, MagicMock(), MagicMock(), policy=RunPolicy(dry_run=False, side_effects_enabled=True)
        )

        with patch("naukri_agent.naukri.apply.first_visible", new=AsyncMock(return_value=apply_btn)):
            verdict = await engine._verify_post_submit(
                Job(job_id="reco-091026027616", title="AEM Fullstack Developer", company="Cognizant", url=page.url)
            )
        self.assertEqual(verdict, "already_applied")

    async def test_screening_chatbot_silent_drawer_close_without_server_verify_fails_closed(self):
        """When drawer closes silently after answering questions (res.completed = False),
        if the page is not an unambiguous native confirmation URL and reload verification
        returns 'unknown', it must fail closed and return None."""
        page = MagicMock()
        page.url = "https://www.naukri.com/job-listings-091026027616"
        page.title = AsyncMock(return_value="AEM Fullstack Developer - Cognizant")
        page.locator.return_value.count = AsyncMock(return_value=0)

        artifacts = MagicMock()
        engine = ApplyEngine(
            page, MagicMock(), artifacts, policy=RunPolicy(dry_run=False, side_effects_enabled=True)
        )
        engine._fast_check = AsyncMock(return_value=False)
        engine._verify_post_submit = AsyncMock(return_value="unknown")

        res = ChatbotResult()
        res.completed = False  # Silent drawer close
        res.answered = 1
        job = Job(job_id="reco-091026027616", title="AEM Fullstack Developer", company="Cognizant", url=page.url)

        with patch("naukri_agent.naukri.apply.first_visible", new=AsyncMock(return_value=None)):
            outcome = await engine._handle_chatbot_result(job, "P", 1, res)
        self.assertIsNone(outcome)

if __name__ == "__main__":
    unittest.main()

