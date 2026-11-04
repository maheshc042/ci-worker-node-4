# HiringCafe

1. Open the public search feed and check that it is usable. If repeated challenges block discovery, capture the diagnostic state and stop the platform.
2. Search with the configured terms, freshness, experience and page limits. Prefilter obvious title mismatches before visiting detail pages. Skip expired or linkless listings.
3. Resolve the company’s ATS link from the job detail. Deduplicate the resolved jobs and run the configured detail filter.
4. Return the eligible ATS link as an external handoff. When Sidekick integration is enabled, the orchestrator can place it in the external outbox. An outbox entry or delivery acknowledgment records a handoff. The downstream applicant or service owns proof of any eventual ATS submission.

**Verify** an eligible ATS link, a stale or linkless listing, a detail rejection and the outbox status when enabled. See the [HiringCafe adapter](../../naukri_agent/platforms/hiringcafe.py) and [Sidekick pickup guide](../../SIDEKICK_PICKUP.md).
