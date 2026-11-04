# Instahyre

1. Confirm the candidate session and open **Opportunities**. Use the configured search filters and pagination. Deduplicate the cards and preserve the search view used to find each opportunity.
2. Open the exact opportunity card and inspect its modal. Check the full details and configured eligibility before applying. If the opportunity is stale or has disappeared, skip it.
3. Route an external company-site action to the handoff. If the native flow already shows Applied, record it as already applied. Otherwise click the Apply control in the opportunity modal once.
4. Require **Applied** on the target card, or a newly displayed application-sent panel naming the exact employer. Capture the evidence before closing the modal. Record an uncertain click or missing receipt as `submission_unconfirmed`.
5. Respect the run’s failure limit. Diagnose repeated misses from the card, modal and receipt evidence before resuming.

**Verify** the target card, an external action, a new employer-specific success panel and a missing or pre-existing panel. See the [Instahyre adapter](../../naukri_agent/platforms/instahyre.py).
