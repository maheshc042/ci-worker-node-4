"""needs_review retries must not depend on the feed (run 595).

On 2026-10-21 the AI/Python LinkedIn feed returned 8 cards, all cross-platform
duplicates, so ``_run_profile`` returned at ``search.jobs_empty_after_dedupe``
before the step-6.5 retry pass. Four jobs stayed stranded on needs_review even
though every blocking question had already been answered.

These tests pin the retry helper's accounting and assert it is reachable from
the empty-feed early return. No browser or DB is used.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from naukri_agent.core.models import (
    ApplicationStatus,
    ApplyOutcome,
    Job,
    RunStats,
    SkipReason,
)
from naukri_agent.core.orchestrator import Orchestrator, StopRun


def _job(job_id: str, company: str = "Acme", title: str = "Python Engineer") -> Job:
    return Job(job_id=job_id, title=title, company=company, url=f"https://x/{job_id}")


class _Repo:
    def __init__(self, retries: list[Job] | None = None) -> None:
        self.retries = retries or []
        self.retry_calls = 0
        self.paused: list[tuple] = []

    async def needs_review_retries(self, *args, **kwargs):
        self.retry_calls += 1
        return list(self.retries)

    async def pause_platform(self, *args):
        self.paused.append(args)

    async def known_job_ids(self, *args, **kwargs):
        return set()


def _orch(
    *,
    retries: list[Job] | None = None,
    applied: int = 0,
    applied_today: int = 0,
    may_mutate: bool = True,
    status: ApplicationStatus = ApplicationStatus.APPLIED,
    reason: SkipReason | None = None,
) -> Orchestrator:
    """Build an Orchestrator with only the collaborator surface retry needs."""
    orch = object.__new__(Orchestrator)
    orch.config = SimpleNamespace(
        run=SimpleNamespace(
            daily_application_cap=80,
            dedupe_window_days=90,
            max_applications_per_company_per_day=3,
        )
    )
    orch.repo = _Repo(retries)
    orch.policy = SimpleNamespace(may_mutate=may_mutate)
    orch.account_key = "primary"
    orch.run_id = 1
    orch.applied_today = applied_today
    orch._profile_applied = {"AI / Python Engineer": applied}
    orch._company_applied = {}
    orch._seen_company_title = set()
    orch.stats = RunStats()
    orch.processed = []
    orch.pace_calls = 0

    def _no_limits(*args, **kwargs):
        return None

    async def _pace():
        orch.pace_calls += 1

    async def _process_job(job, *args, **kwargs):
        orch.processed.append(job.job_id)
        return ApplyOutcome(status=status, reason=reason)

    orch._check_global_limits = _no_limits
    orch._check_platform_limits = _no_limits
    orch._pace = _pace
    orch._process_job = _process_job
    return orch


_PROFILE = SimpleNamespace(
    name="AI / Python Engineer",
    account="primary",
    platform_limits={"linkedin": 5},
    max_applications_per_run=5,
)
_PLATFORM = SimpleNamespace(platform_name="linkedin")


class TestRetryHelper:
    def test_applies_and_updates_counters(self):
        orch = _orch(retries=[_job("a"), _job("b", company="Beta")])
        applied: list[tuple[Job, ApplyOutcome]] = []
        failed: list[tuple[Job, ApplyOutcome]] = []

        count = asyncio.run(
            orch._retry_needs_review(
                _PROFILE, _PLATFORM, object(), None, None, applied, failed
            )
        )

        assert count == 2
        assert orch.processed == ["a", "b"]
        assert orch.applied_today == 2
        assert orch._profile_applied["AI / Python Engineer"] == 2
        assert orch.pace_calls == 2
        assert [j.job_id for j, _ in applied] == ["a", "b"]
        assert failed == []

    def test_failures_are_not_counted(self):
        orch = _orch(
            retries=[_job("a")],
            status=ApplicationStatus.FAILED,
        )
        applied: list[tuple[Job, ApplyOutcome]] = []
        failed: list[tuple[Job, ApplyOutcome]] = []

        count = asyncio.run(
            orch._retry_needs_review(
                _PROFILE, _PLATFORM, object(), None, None, applied, failed
            )
        )

        assert count == 0
        assert orch.applied_today == 0
        assert applied == []
        assert [j.job_id for j, _ in failed] == ["a"]

    def test_stops_when_budget_is_spent(self):
        orch = _orch(retries=[_job("a")], applied=5)

        count = asyncio.run(
            orch._retry_needs_review(
                _PROFILE, _PLATFORM, object(), None, None, [], []
            )
        )

        assert count == 0
        assert orch.repo.retry_calls == 0
        assert orch.processed == []

    def test_read_only_mode_skips_retries(self):
        orch = _orch(retries=[_job("a")], may_mutate=False)

        count = asyncio.run(
            orch._retry_needs_review(
                _PROFILE, _PLATFORM, object(), None, None, [], []
            )
        )

        assert count == 0
        assert orch.repo.retry_calls == 0

    def test_cloudflare_pauses_platform(self):
        orch = _orch(
            retries=[_job("a")],
            status=ApplicationStatus.SKIPPED,
            reason=SkipReason.CLOUDFLARE_CHALLENGE,
        )

        try:
            asyncio.run(
                orch._retry_needs_review(
                    _PROFILE, _PLATFORM, object(), None, None, [], []
                )
            )
        except StopRun:
            pass
        else:  # pragma: no cover - the raise is the contract
            raise AssertionError("cloudflare challenge must stop the run")

        assert orch.repo.paused == [
            ("primary", "linkedin", "Cloudflare challenge wall")
        ]


class TestEmptyFeedStillRetries:
    def test_run_profile_retries_when_feed_is_empty(self):
        """Regression: the old early return skipped step 6.5 entirely."""
        orch = _orch()
        orch.config.recommended = SimpleNamespace(enabled=True)
        orch.config.run = SimpleNamespace(
            daily_application_cap=80,
            dedupe_window_days=90,
            max_applications_per_company_per_day=3,
        )
        orch.retry_args = []

        async def _fetch_jobs(*args, **kwargs):
            return []

        async def _retry(*args):
            orch.retry_args.append(args)
            return 0

        platform = SimpleNamespace(
            platform_name="linkedin", fetch_jobs=_fetch_jobs
        )
        profile = SimpleNamespace(
            name="AI / Python Engineer",
            account="primary",
            use_recommended=True,
            platform_limits={"linkedin": 5},
            max_applications_per_run=5,
        )
        orch._retry_needs_review = _retry

        asyncio.run(orch._run_profile(platform, profile, None, None))

        assert len(orch.retry_args) == 1, "empty feed must still run the retry pass"
        assert orch.retry_args[0][0] is profile
        assert orch.retry_args[0][1] is platform
        assert orch.stats.scraped == 0
