# Naukri

1. Confirm the session for the selected account and check the active résumé for that account. If Naukri asks for OTP or CAPTCHA, stop and restore the session manually.
2. Collect jobs from **Recommended jobs**. This adapter is intentionally recommended-only. Preserve the source when diagnosing a thin or empty feed.
3. Open the exact job and read its description. Reject hard mismatches and classify already-applied, stale and walk-in jobs separately. Route company-site applications to the external handoff.
4. Click the native Apply control. Naukri can finish directly or open a screening questionnaire in the application flow. Answer from configured facts, check required fields and route unresolved questions to review. A verified eligibility conflict can produce a skip.
5. Require Naukri’s explicit application evidence. The engine can verify again on the target listing when needed. An already-applied marker is a separate outcome. If the answers were sent but the submission could not be verified, record `submission_unconfirmed`.

**Verify** a direct apply, a screening flow with a receipt, an unresolved question, and a screening flow with no receipt. Keep primary and secondary sessions distinct. See the [platform adapter](../../naukri_agent/platforms/naukri_platform.py), [application engine](../../naukri_agent/naukri/apply.py) and [authentication flow](../../naukri_agent/naukri/auth.py).
