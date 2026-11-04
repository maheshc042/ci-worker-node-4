# LinkedIn

1. Confirm that the browser has an authenticated LinkedIn session. If authentication fails or a checkpoint blocks access, restore the session before processing jobs.
2. Collect jobs from LinkedIn search, deduplicate by job ID, then open the exact listing. Check its full description and run the configured matching and eligibility checks.
3. Use **Easy Apply** for native applications. Treat a company-site-only Apply action as an external handoff. Skip a listing LinkedIn already marks as applied.
4. Work through the Easy Apply dialog in order. Handle the safety reminder if shown, use the résumé for the role, and fill each step with configured answers. Check required fields and validation before advancing. An unresolved question or a stuck step goes to review.
5. Click **Submit application** once. Require a dialog that explicitly says the application was sent, or an **Applied** or **Application submitted** marker on the exact job page. Dialog closure alone leaves the attempt unconfirmed.

**Verify** a multi-step flow, a blocked required field, an external Apply and a post-submit receipt tied to the job. See the [LinkedIn adapter](../../naukri_agent/platforms/linkedin.py).
