import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_agent.core.models import Job
from naukri_agent.naukri.apply import ApplyEngine
from naukri_agent.naukri.parser import extract_jd_experience


class TestJdExperienceExtractorHardening(unittest.TestCase):
    def test_framework_versions_not_treated_as_tenure(self):
        """Framework versions like Angular 18+, React 18/19, Node 20 must NOT be extracted as tenure."""
        # Comviva case
        self.assertEqual(
            extract_jd_experience("Hands-on experience in React 18/19 and Next.js"),
            (None, None),
        )
        # Dansk Scanning case
        self.assertEqual(
            extract_jd_experience("Expert in Angular 18+ and TypeScript experience"),
            (None, None),
        )
        # Node & Python versions
        self.assertEqual(
            extract_jd_experience("Experience with Node.js 20 and Python 3.12"),
            (None, None),
        )

    def test_shift_markers_not_tenure(self):
        """24/7 support shift markers must NOT be extracted as 24y tenure."""
        self.assertEqual(
            extract_jd_experience("Must have experience working in 24/7 production support"),
            (None, None),
        )

    def test_recruiter_typo_handling(self):
        """Recruiter typo '24 years of experience' for junior QA role must be mapped to (2.0, 4.0)."""
        self.assertEqual(
            extract_jd_experience("Looking for QA Engineer with 24 years of experience in automation"),
            (2.0, 4.0),
        )

    def test_valid_experience_bands(self):
        """Standard experience requirements must continue to be extracted accurately."""
        self.assertEqual(
            extract_jd_experience("3-7 years experience with Python development, Django, SaltStack"),
            (3.0, 7.0),
        )
        self.assertEqual(
            extract_jd_experience("Minimum 2 years of experience in AI/ML"),
            (2.0, 2.0),
        )
        self.assertEqual(
            extract_jd_experience("5+ years experience in Devops"),
            (5.0, None),
        )


class TestApplyEngineOutlierGuard(unittest.IsolatedAsyncioTestCase):
    async def test_enrich_ignores_absurd_jd_experience_outlier(self):
        """If card had max_experience=3.0, an absurd JD min extracted (e.g. 18.0) must NOT override card metadata."""
        engine = ApplyEngine.__new__(ApplyEngine)
        engine.page = MagicMock()

        job = Job(
            job_id="test-123",
            title="Junior QA Engineer",
            company="TestCo",
            url="https://test.com/job/123",
            min_experience=1.0,
            max_experience=3.0,
        )

        # Mock the rendered JD text containing a framework version.
        description = MagicMock(
            inner_text=AsyncMock(return_value="Angular 18+ and TypeScript experience required")
        )
        with patch("naukri_agent.naukri.apply.first_visible", new=AsyncMock(return_value=description)):
            await engine.enrich(job)

        # Card metadata must be preserved
        self.assertEqual(job.min_experience, 1.0)
        self.assertEqual(job.max_experience, 3.0)

