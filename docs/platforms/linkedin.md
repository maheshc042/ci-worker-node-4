# LinkedIn Platform Playbook

Workflow and implementation reference for LinkedIn Easy Apply and recruiter-post email outreach. Reviewed against live runs 713–715 and an SMTP send on 6 November 2026 IST. This guide describes behavior and evidence, not a guarantee for future website layouts.

---

## 1. Authentication & Safety Guardrails
- **Target URL**: `https://www.linkedin.com/feed/`
- **Session Verification ([`linkedin_signed_in`](../../naukri_agent/platforms/linkedin.py))**:
  - Validates active profile markers: `img.global-nav__me-photo`, `.feed-identity-module`, `button.global-nav__me-menu-trigger`, or verified global nav elements.
  - Observes the initial silent sign-in transition for up to 25 checks. The current navigation link is `/mynetwork`, without a required trailing slash. A temporary login URL alone is not a failed session.
  - A manual run explicitly selecting LinkedIn can recheck an authentication-only pause after the login is repaired. Successful authentication is still mandatory; challenge and rate-limit pauses are separate.
- **Perimeter & Checkpoint Policy**:
  - Detects security challenges and checkpoints (`checkpoint`, `challenge`, `security-check`, `uas`, `authwall`).
  - **Headless Mode**: Immediately logs a warning and exits gracefully without hanging the run.
  - **Headed Mode**: Pauses and allows up to 120 seconds for the user to complete interactive human verification.
  - **Never** attempts automated CAPTCHA or PIN challenge solving.
- **Daily Budget**: Strictly adheres to configured apply limits (per-profile or per-run platform limits plus the configured account daily cap). A search target or platform limit is not an independent daily budget.

---

## 2. Job Discovery & Search Ingestion ([`fetch_jobs`](../../naukri_agent/platforms/linkedin.py))

### 1. High-Precision Track Boolean Queries
Search queries are generated per profile track using boolean clauses with hard technical skills:
- **AI & Python Track**:
  ```text
  ("AI Engineer" OR "GenAI Engineer" OR "AI Developer" OR "LLM Engineer" OR "RAG Engineer" OR "Forward Deployed Engineer" OR "Python Developer" OR "Backend Developer") AND ("Python" OR "AI" OR "LLM" OR "RAG" OR "FastAPI")
  ```
- **Full Stack & QA Track**:
  ```text
  ("Full Stack Developer" OR "Software Engineer" OR "Node.js Developer" OR "Backend Developer" OR "SDET" OR "Cloud Engineer" OR "Quality Analyst") AND ("React" OR "Node" OR "TypeScript" OR "MERN" OR "AWS" OR "QA")
  ```

### 2. URL Parameters & Pagination
Constructs the search URL via `_get_search_url`:
- `f_AL=true`: Easy Apply filter only.
- `f_TPR=r{tpr_seconds}`: Recency filter (model default 3 days = 259,200s; current configured `linkedin.days` is 1; falls back to 7 days if $<10$ jobs found).
- `f_E=2%2C3`: Filters strictly for Entry Level (`2`) and Associate (`3`).
- `sortBy=DD`: Orders listings chronologically by date posted.
- **Pagination**: Iterates through up to 6 pages per pass using in-page buttons (`button[aria-label='Page {N}']`) or URL offsets (`&start=0, 25, 50, ...`).

### 3. Card Scraping & Metadata Extraction ([`_scroll_and_extract_cards`](../../naukri_agent/platforms/linkedin.py))
- **Layout Support**: Handles both modern `[componentkey^='job-card-component-ref-']` cards and legacy search results.
- **Title Normalization**: Strips noise badges (*"with verification"*, *"Be an early applicant"*) and deduplicates doubled title strings (e.g. *"Software Engineer Software Engineer"*).
- **Fast Card-Level Filtering**:
  - `easy_apply`: Checks footer text. If "Easy Apply" is missing, marks `easy_apply = False` to discard company-site jobs in 0s instead of burning 20s at apply time.
  - `applicant_count`: Parsed or imputed from card text (*"57 applicants"*, *"Over 100 applicants"* $\rightarrow$ 101, *"Be an early applicant"* $\rightarrow$ 5).
  - `posted_days_ago`: Parses relative timestamps (*"just now"*, *"today"*, *"yesterday"*, *"3 days ago"*).
  - `is_walkin`: Tags drive/walk-in ads (*"walk-in"*, *"f2f"*, *"face to face"*).
  - Experience extraction: Extracts explicit years or infers bounds for senior (5–10y) and fresher (0y).

---

## 3. Screening & Multi-Tier Filter Pipeline

### 1. Card Filter ([`FilterEngine.evaluate_card`](../../naukri_agent/core/filters.py))
- Title keyword match against `title_must_include_any`.
- Checks experience bounds against profile limits (e.g. 0 to 3.5y).
- Rejects non-technical roles and blocked company names.

### 2. Deep Suitability & Scoring Engine ([`evaluate_job_suitability`](../../naukri_agent/platforms/linkedin.py))
Uses an adapter-specific suitability score with a threshold of 60. The implementation labels it out of 100 but does not clamp its additions to 100.
- **Base Score**: 75.
- **Title Blocklist**: Uses the adapter's explicit whole-word blocklist for technologies and job families. Do not infer additional exclusions from broad labels such as support. *"Lead"* is deliberately permitted for experienced candidate matching.
- **Target Role Alignment**: Matches against 30 target roles (+20 points).
- **Unpaid / Spam Skipping**: Immediately rejects unpaid, fellowship, volunteer, or survey scam postings (`pollfish`, etc.).
- **Seniority Shield**: A high-tenure pattern combined with a senior title can reject the listing. The shared detail gate separately evaluates extracted experience.
- **Geographic Gate**: Rejects foreign on-site positions (US, UK, Germany, Canada, Australia) while approving India-based or remote roles (+5 points).
- **Stack Overlap Gate**: Evaluates whole-word regex matches (`_word_hit`) across 40+ engineering tokens. If zero stack overlap is detected, the job is disqualified immediately (`"No candidate-stack overlap in title/JD"`).

### 3. Detail Pre-Submit Filter & Re-Gating
- Navigates directly to canonical `https://www.linkedin.com/jobs/view/{id}/`.
- Extracts full JD description, recruiter emails, and external form links.
- Extracts JD experience bounds via `extract_jd_experience`.
- Re-evaluates profile rules with JD-enriched experience.
- Runs `pre_submit_check(job)`.

### 4. External ATS Routing ([`_capture_company_site_url`](../../naukri_agent/platforms/linkedin.py))
- If the listing requires applying on the employer website (no Easy Apply button):
  - Clicks the external apply button.
  - Captures popup URLs or advances through the *"Leaving LinkedIn"* interstitial screen (`_INTERSTITIAL_CONTINUE_SELECTORS`: *"Continue"*, *"Proceed"*, *"Yes, apply"*).
  - Normalizes external employer ATS URL (discarding `linkedin.com` links).
  - Returns `status = EXTERNAL`, `reason = SkipReason.EXTERNAL_APPLY`, and routes the URL to Sidekick for external submission.

---

## 4. Multi-Step Easy Apply Traversal ([`apply_to_job`](../../naukri_agent/platforms/linkedin.py))

### 1. Initiate Apply
- Clicks `button.jobs-apply-button` (Easy Apply).
- Automatically acknowledges safety reminders (*"Continue applying"*, *"Acknowledge"*, *"Got it"*, *"I understand"*).
- Resolves dialog container (`_resolve_modal`), handling CSS module containers, dialog tags, and slide-out drawers. Retries click once on React hydration races.

### 2. Multi-Step Form Execution (Up to 11 Steps)
Iterates through modal steps, executing strict element resolution inside `modal`:

- **State-Aware Resume Selection (`_resume_for_role`)**:
  - Matches resume stem corresponding to the track (`want_ai` vs `want_fullstack`).
  - Audits document cards (`div[data-test-document-item]`). If target resume is present and unselected, clicks it.
  - If missing from LinkedIn storage, uploads the exact PDF from the local `resumes/` folder via `input[type='file']`.
  - Supports the current native PDF radio selector as well as legacy cards. Selects the exact filename and verifies checked state; the AI filename must not prefix-match the Full Stack filename. These radios are document controls, not a question called “PDF”.
  - Explicit AI/Python roles use the enabled AI profile's résumé even when discovered through the Full Stack search. Unverified selection blocks submission.
- **Dropdowns (`<select>`)**:
  - Ignores Month/Year option dumps (`_looks_like_option_dump`).
  - Auto-selects India (+91) for phone country codes.
  - Matches candidate email for email selectors.
  - Uses pay-band containment (`_pick_ctc_option`) for salary ranges.
  - Resolves custom dropdown questions via `AnswerEngine`.
- **Radio Fieldsets & Groups**:
  - Handles modern split DOM structure where native radio input has an empty label and option text resides in `wrapper.nextElementSibling?.querySelector('p')`.
  - Resolves questions via `_resolve_screening`.
  - Selects pay bands strictly via range containment.
- **Checkboxes**:
  - Answers screening questions.
  - Automatically unchecks company marketing follow checkboxes (*"Follow company"*).
- **Text, Numeric & Combobox Inputs**:
  - Full name and unambiguous two-part first/last names come from the configured candidate name. An optional middle name can remain empty. Ambiguous identity fields still require review.
  - Reconciles existing screening values and preselected radio answers against approved facts; a platform default is not candidate evidence. Each track receives its own answer engine.
  - A standalone immediate-start question can use a configured zero-day notice period. Shift, contract, work authorization and other consent questions still require their own verified answers.
  - Listing-specific answers in `job_screening_answers` require an exact job ID, question, and displayed choice. Use this scope for an accepted freelance rate or a location decision tied to one listing.
  - Discovers detached question labels by traversing nearest preceding sibling headers.
  - **Phone Inputs**: Fills `applicant_phone` with digit validation and sequential typing fallback. Never submits hardcoded fake numbers.
  - **Email Inputs**: Fills `applicant_email`.
  - **Location Comboboxes**: Types configured city and clicks typeahead suggestion.
  - **Numeric Inputs**: Existing-value handling and resolved numeric formatting are separate. Resolved values can retain decimals. Salary units can be scaled to annual INR or thousands from field text and bounds. This is not a general 0–99 constraint.
  - **Date Inputs**: Converts configured dates to ISO `YYYY-MM-DD` or custom Indian formats (`dd-mm-yyyy`). Never fabricates today's date if unconfigured.
  - **Exam Percentiles / Marks**: Leaves unconfigured grades blank to avoid fraud; escalates to review.
  - **Field-Length Guard (`_fit_field_length`)**: Automatically truncates answers to input's `maxlength` attribute, preventing *"Invalid input"* validation stalls.
  - **Synthetic Events**: Dispatches native `input`, `change`, and `blur` events on all modified inputs.

### 3. Screen Review Escalation Guard
If any answer relied on generic tenure inferences (`experience:unlisted_default`, `willingness_affirmative`, etc.):
- Captures screenshot of the form.
- Safely closes modal.
- Returns `NEEDS_REVIEW / UNANSWERED_QUESTION` with question items for orchestrator persistence. Repository review alerts currently reload the global notification configuration; an in-memory per-run notification override does not consistently suppress them.

### 4. Stuck Step Recovery
If the step signature (`inputs:{count}::{form_text}`) remains unchanged for 3 consecutive attempts:
- Captures forensic screenshot and HTML dump.
- Inspects `.artdeco-inline-feedback--error`.
- Gathers empty required controls via `_empty_required_fields`.
- Splits blocked questions into per-question review items via `_extract_blocked_questions`.
- Dismisses modal safely and returns `NEEDS_REVIEW`.

### 5. Advancing Steps
- Clicks `button[aria-label*='Review your application']` or `button:has-text('Next')`.
- If missing, tests fallback advance allowlist (`_fallback_action_button`: *"continue"*, *"next"*, *"review"*), strictly excluding navigation dismissals (*"dismiss"*, *"back"*, *"save and exit"*).

---

## 5. Submission Readiness & Compulsory Post-Submit Verification

### 1. Submission Readiness Audit ([`_submission_ready`](../../naukri_agent/platforms/linkedin.py))
Before clicking submit, runs DOM validity script across all controls:
- Asserts Submit application button is visible and enabled.
- Checks that no controls have `aria-invalid="true"` or `validity.valid === false`.
- Verifies that all required radio groups have a checked option and required inputs have values.
- In dry-run mode (`policy.dry_run`), halts before submit and skips with `SkipReason.DRY_RUN`.

### 2. Single Submit Action
- Records mutation `submit_application` and adds job ID to `_submission_attempts`.
- Resolves the active dialog's exact accessible Submit application button name and clicks once.

### 3. Compulsory Verification ([`_verify_submit`](../../naukri_agent/platforms/linkedin.py))
> [!IMPORTANT]
> **Modal Closure Does NOT Imply Application**: Modal closure on network drops or backdrop unmounts will fail closed unless authoritative server-side confirmation is proven.

1. **In-Dialog Receipt Detection**:
   - Inspects the active dialog for:
     - `[role='dialog'] :text-is('Application sent')`
     - `[role='dialog'] :text-is('Your application was sent')`
     - `[data-test-modal] :text-is('Application sent')`
     - `[data-test-modal] :text-is('Your application was sent')`
     - `dialog[open] :text-is('Application sent')`
   - Attempts the supported Done control after observing the receipt. Failure to dismiss it is separate from application confirmation.
2. **Dedicated Job Page Status**:
   - Inspects `/jobs/view/{job_id}`:
     - `main button:text-is('Applied')`
     - `main span:text-is('Application submitted')`
     - `main p:text-is('Application submitted')` (observed in the current live layout)
   - Requires the exact target `/jobs/view/{id}/` URL. If no supported receipt is visible, reopens that exact target and checks again. Never clicks Submit a second time to obtain a receipt.
3. **Outcome Resolution**:
   - **Confirmed**: Returns `status = APPLIED`, `confirmation_type = "dom_marker"`, `confirmation_evidence = "LinkedIn explicit submission receipt or target listing applied state"`.
   - **Unconfirmed**: Captures inline error feedback, dismisses modal cleanly with Discard confirmation (`_dismiss_modal`), and returns `status = FAILED`, `reason = SkipReason.SUBMISSION_UNCONFIRMED`.

---

## 6. Invariants & Anti-Patterns (Do NOT Break)
1. **DO NOT assume modal closing means applied**: `test_closed_modal_without_confirmation_fails` guarantees that an unconfirmed dismissal fails closed.
2. **DO NOT click through unverified form errors**: If LinkedIn highlights a required field in red, capture diagnostics and escalate to review.
3. **DO NOT guess numeric question fields**: Use verified facts and preserve unresolved questions for review.
4. **DO NOT use broad unified queries**: Execute sequential, track-specific queries per profile to preserve search relevance.
5. **DO NOT apply without Easy Apply verification**: If the button says "Apply on company site", route to `EXTERNAL` with the resolved ATS link.

## 7. Recruiter-post email workflow

This is a separate route from Easy Apply. It sends email to an address explicitly published in a recruiter post; it does not send LinkedIn messages or infer email addresses.

1. Check the global mutation setting, Gmail credentials and remaining daily email budget. The per-run limit can only reduce the daily allowance. Restore LinkedIn cookies from the primary account session before falling back to the campaign browser profile.
2. Search recent hiring posts using the configured keyword list. Enforce the hunt deadline even when zero eligible leads are found. Observe normal login transitions; stop for blocked access or rate limits.
3. Expand posts, extract the author's metadata, then evaluate the hiring body separately. A recruiter's “Senior” headline does not make the advertised job senior. Reject job-seeking posts, explicit experience conflicts, incompatible stacks and foreign-location restrictions. US-only remote and Toronto hybrid roles are incompatible with this India-based profile; worldwide remote is distinct.
4. Require a published recipient and a verifiable post permalink. Current layouts can omit a permalink anchor: use the exact post's native More → Copy link to post control, capture only that copy action, and resolve a native `lnkd.in/p/...` redirect to an actual LinkedIn post. Do not invent activity IDs. A missing/ambiguous link blocks sending.
5. Apply the final candidate-fact gate, normal 60-day recipient deduplication and résumé availability check. An explicit required skill tenure cannot borrow total-career tenure. Combined requirements must satisfy each named mapped skill; explicit alternatives permit either. General arbitrary JD interpretation remains limited.
6. Use an explicit Position/Role/Job title from the post when available. Keep the classified track for résumé routing. Do not attribute a hiring client from the recruiter's headline employer. If the client is unverified, omit its name.
7. Generate the pitch with total software engineering experience separate from domain specialties. Attach the configured track PDF. Send once through SMTP, with Date and Message-ID; record the contact after acceptance. The audit harness additionally held the exact draft for inspection before sending; normal unattended campaigns do not have that manual inspection step.
8. Report SMTP acceptance separately from inbox delivery or recruiter response. A network loss during SMTP DATA can leave delivery uncertain; durable reconciliation for that case is not implemented. Do not describe this as guaranteed delivery.

## 8. Answer-learning boundary

Unlisted quantitative skill tenure stays in review. During run 715, the review queue incorrectly fuzzy-matched Appium to MongoDB. The answer engine blocked submission, but the queue promoted a wrong answer. Queue reuse and human-answer cascade now require identical normalized question wording. Historical fuzzy/cascade resolutions are excluded as reuse sources. The erroneous Appium KB entry was backed up and removed; review #476 is pending again. This does not establish that every historical learned answer has been audited.

## Verification evidence and limits

Run 688 collected 125 Full Stack and QA listings and 77 AI and Python listings. The planner selected 33 and 22 respectively. Run 695 resolved mapped React and JavaScript tenure and held a testing-project preference for review. Run 698 reached the enabled final Submit application button with submission disabled.

Runs 713–714 exposed a detached-element scroll failure and the native résumé-radio parser gap. Saved-form replay now verifies the exact AI and Full Stack files with no spurious PDF question.

Run 715 submitted Aditi Consulting Cloud Engineer – L2 Support (`4487717910`) and Oracle Automation Engineer (`4485817160`). Its original verifier reported both unconfirmed because it missed the current paragraph receipt. Read-only reopening confirmed exact “Application submitted” evidence for both; the repaired production verifier returned true for both. Application records and aggregate run counts were reconciled with audit events; original reports remain available. No second submission was made. The run also held Appium, background-check consent and model-evaluation tenure questions instead of guessing.

The email campaign discovered a Python Developer post by Aiswarya V. A final live source check verified its published email, 2+ years requirement, Trivandrum location and salary up to 8 LPA. One reviewed email was accepted by Gmail on 6 November IST, with the AI/Python résumé and a Message-ID. This is live SMTP evidence, not verified inbox placement. See the [audit](../../analysis/linkedin_production_review.md).

Remaining limits include title-based tenure estimates during early planning, incomplete arbitrary mandatory-skill/qualification interpretation, some post-menu variants that still fail closed on missing source links, and feed-oriented “considered” metrics that exclude direct review retries. Both live-submitted jobs used the Full Stack track; the AI form and résumé path were exercised, but the AI job remained blocked by genuine unanswered questions. A title pass does not establish every named skill or an overall 10/10 production result.
