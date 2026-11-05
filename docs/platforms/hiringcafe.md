# HiringCafe Platform Playbook

Workflow and implementation reference for the HiringCafe adapter. This guide describes the current workflow and flags verification limits in the implementation in [`naukri_agent/platforms/hiringcafe.py`](../../naukri_agent/platforms/hiringcafe.py), [`filters.py`](../../naukri_agent/core/filters.py), and [`SIDEKICK_PICKUP.md`](../../SIDEKICK_PICKUP.md).

---

## 1. Overview & Handoff Role
- **Model**: HiringCafe serves as a pure **discovery and external handoff adapter**. It does not perform in-platform form automation; instead, it extracts clean, direct-to-ATS application links.
- **Sidekick Integration**: Eligible ATS links are forwarded as `ApplicationStatus.EXTERNAL` with `external_url = ats_url` and offered to the orchestrator for conditional Sidekick dispatch. Side effects and integration must be enabled, and dispatch validation must pass.

---

## 2. Ingestion & Server-Side Rendered (SSR) Feed ([`fetch_jobs`](../../naukri_agent/platforms/hiringcafe.py))

### 1. SSR Navigation vs API Blockade
- Cloudflare aggressively 403s automated calls to `/api/search-jobs`. The adapter navigates directly to the SSR listing URL:
  ```text
  https://hiring.cafe/classic?searchState={...}&page=N
  ```
- Search parameters are dynamically encoded into JSON `searchState`:
  - `searchQuery`: Role keywords derived from `profile.title_keywords`.
  - `dateFetchedPastNDays`: Configured freshness window (e.g. 3 days).
  - `roleYoeRange`: `[0, max_exp]`.
  - `departments`: Engineering, Software Development, IT, Data, QA.
  - `workplaceTypes`: Remote, Hybrid, Onsite.

### 2. Perimeter Clearance & Turnstile Cooldown ([`_settle_challenge`](../../naukri_agent/platforms/hiringcafe.py))
- Detects Cloudflare interstitial challenges (`just a moment`, `verifying you are human`).
- **Cooldown-Guarded Checkbox Click**: Enforces a minimum 10-second interval (`_last_cf_click`) before clicking Turnstile checkboxes. Mid-flight clicks or premature reloads trigger solve-reload challenge loops.
- **Clearance Verification**: Both gates must pass: clean page title/content (`_has_clearance`) AND visible job cards in the DOM (`_probe_cards`).
- **Poisoned Cookie Purge**: Drops `hiringcafe.com` and `hiring.cafe` domain cookies if challenged repeatedly, reloading against a clean cookie jar without disturbing other platform sessions.
- **Headed vs Headless**:
  - Headed: Initial clearance can wait roughly 120 seconds for manual completion. The separate `_await_manual_solve` helper polls for a call-specific bounded interval.
  - Headless: Logs a diagnostic screenshot (`hiringcafe-challenge.png`) and returns an access failure after bounded recovery attempts. Run 700 ended partial at this gate.

### 3. Card Scraping & Substantive Container Ascent
- **Substantive Text Recovery**: The anchor text (`a[href*='/job/']`) on HiringCafe is often micro-copy (*"Job Posting | View all"*, ~20 chars). The scraper climbs up to 6 parent levels until finding substantive text ($>120$ characters) to ensure downstream filters receive rich descriptions (run 561 fix).
- **Fast Card Pre-Filter (`_card_prefilter_pass`)**: Checks card text against `title_must_include_any` before opening detail pages, preventing costly navigations for obvious out-of-scope roles.

---

## 3. Detail Resolution & ATS Link Extraction ([`_resolve_detail`](../../naukri_agent/platforms/hiringcafe.py))

### 1. Fast Expired-Listing Probe (`_is_expired_body`)
- Probes the first 2,000 characters of the detail page body for expired markers:
  ```regex
  \bexpired\b|no longer accepting|position\s+filled|opening\s+closed|no longer available
  ```
- Bypasses Cloudflare challenge handshakes for dead postings, transforming a 30-second stall into a 3-second check.
- **Rotten-Feed Breaker**: Halts term ingestion after 25 consecutive dead listings (`dead_streak >= 25`) to prevent wasting quota on stale feeds.

### 2. Authoritative ATS Link Resolution ([`_select_apply_url`](../../naukri_agent/platforms/hiringcafe.py))
Inspects candidate anchors on the detail page:
- **Action Regex**: Prioritizes anchors matching `apply now`, `apply on company site`, `view job posting`.
- **Recognized ATS Fallback**: Matches deep links on recognized ATS domains:
  - `greenhouse.io`, `lever.co`, `myworkdayjobs.com`, `ashbyhq.com`, `bamboohr.com`, `smartrecruiters.com`, `workable.com`, `icims.com`.
- **Excluded Domains**: Discards internal domains (`hiringcafe.com`), Google Forms, Typeform, and social networks (`linkedin.com`, `reddit.com`, `twitter.com`).

---

## 4. Screening & Pre-Submit Filter Pipeline ([`apply_to_job`](../../naukri_agent/platforms/hiringcafe.py))
1. **Metadata Validation**: Must have non-empty `title`, non-empty `body`, and valid `apply_url`. Missing fields are dropped immediately.
2. **Detail Eligibility Check ([`pre_submit_check`](../../naukri_agent/platforms/hiringcafe.py))**:
   - Enforces the same strict candidate experience bounds and forbidden phrase filters as native platforms.
   - If rejected, skips cleanly with `decision.reason` without queueing into Sidekick.
3. **External Handoff**:
   - Returns:
     ```python
     ApplyOutcome(
         status=ApplicationStatus.EXTERNAL,
         reason=SkipReason.EXTERNAL_APPLY,
         detail="HiringCafe direct company ATS link; forwarded to Sidekick",
         external_url=job.url,
     )
     ```
   - The orchestrator can enqueue the role when mutations and Sidekick integration are enabled. Returning EXTERNAL does not itself establish queueing, delivery or ATS submission.

---

## 5. Invariants & Anti-Patterns (Do NOT Break)
1. **DO NOT bypass `pre_submit_check`**: External jobs must satisfy the exact same quality and tech filters as native jobs before entering the outbox.
2. **DO NOT accept linkless or titleless listings**: Corrupt listings must be rejected at the boundary.
3. **DO NOT mark HiringCafe jobs as `APPLIED`**: HiringCafe is an external discovery gateway; only the downstream applicant or ATS submitter can record final delivery.
4. **DO NOT hammer Turnstile checkboxes without cooldowns**: Repeated clicks reset active validation tokens and produce permanent challenge walls.

## Evidence and implementation limits

Headless dry run 700 stopped at the Cloudflare clearance gate. Local checks verified the missing-check failure, detail rejection, accepted ATS route and invalid-board-link rejection. Live discovery and downstream delivery remain unverified.

The current code attempts bounded Turnstile checkbox interactions. A guide's statement about cooldowns does not establish that this is compatible with the intended manual-challenge policy. Review the policy before extending that behavior. An unavailable feed must not be reported as zero eligible jobs.

The parser requires non-empty heading and body, extracts tenure bounds and validates link candidates. It still combines card text with a page-body slice, uses a placeholder for an unknown employer, and substitutes the configured maximum age when a timestamp is missing. These are metadata limitations. Explicit action links can use arbitrary company domains, so recognizing an Apply label does not independently establish employer ownership of a destination.
