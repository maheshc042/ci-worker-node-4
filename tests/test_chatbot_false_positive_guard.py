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


if __name__ == "__main__":
    unittest.main()
