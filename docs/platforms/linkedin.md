# LinkedIn Platform Playbook

Comprehensive architecture and operational specification for the LinkedIn Easy Apply adapter. This document strictly mirrors the runtime logic in [`naukri_agent/platforms/linkedin.py`](../../naukri_agent/platforms/linkedin.py), [`filters.py`](../../naukri_agent/core/filters.py), and [`answers.py`](../../naukri_agent/core/answers.py).

---

## 1. Authentication & Safety Guardrails
- **Target URL**: `https://www.linkedin.com/feed/`
- **Session Verification ([`linkedin_signed_in`](../../naukri_agent/platforms/linkedin.py))**:
  - Validates active profile markers: `img.global-nav__me-photo`, `.feed-identity-module`, `button.global-nav__me-menu-trigger`, or verified global nav elements.
  - Allows up to 15 seconds for initial client-side authentication handshakes.
- **Perimeter & Checkpoint Policy**:
  - Detects security challenges and checkpoints (`checkpoint`, `challenge`, `security-check`, `uas`, `authwall`).
  - **Headless Mode**: Immediately logs a warning and exits gracefully without hanging the run.
  - **Headed Mode**: Pauses and allows up to 120 seconds for the user to complete interactive human verification.
  - **Never** attempts automated CAPTCHA or PIN challenge solving.
- **Daily Budget**: Strictly adheres to configured apply limits (typically 35 applies per day) to maintain account trust scores.

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
- `f_TPR=r{tpr_seconds}`: Recency filter (default 3 days = 259,200s; falls back to 7 days if $<10$ jobs found).
- `f_E=2%2C3`: Filters strictly for Entry Level (`2`) and Associate (`3`).
- `sortBy=DD`: Orders listings chronologically by date posted.
- **Pagination**: Iterates through up to 6 pages per pass using in-page buttons (`button[aria-label='Page {N}']`) or URL offsets (`&start=0, 25, 50, ...`).

### 3. Card Scraping & Metadata Extraction ([`_scroll_and_extract_cards`](../../naukri_agent/platforms/linkedin.py))
- **Layout Support**: Handles both modern `[componentkey^='job-card-component-ref-']` cards and legacy search results.
- **Title Normalization**: Strips noise badges (*"with verification"*, *"Be an early applicant"*) and deduplicates doubled title strings (e.g. *"Software Engineer Software Engineer"*).
- **Fast Card-Level Filtering**:
  - `easy_apply`: Checks footer text. If "Easy Apply" is missing, marks `easy_apply = False` to discard company-site jobs in 0s instead of burning 20s at apply time.
  - `applicant_count`: Extracted directly from card text (*"57 applicants"*, *"Over 100 applicants"* $\rightarrow$ 101, *"Be an early applicant"* $\rightarrow$ 5).
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
Scores listings on a 100-point rubric (minimum threshold: $\ge 60/100$):
- **Base Score**: 75.
- **Title Blocklist**: Blocks out-of-scope technologies (Java, Spring Boot, .NET, C#, PHP, SAP, Salesforce, Mainframe) and non-dev roles (Sales, BPO, BA, SEO, Tech Support). *"Lead"* is deliberately permitted for experienced candidate matching.
- **Target Role Alignment**: Matches against 30 target roles (+20 points).
- **Unpaid / Spam Skipping**: Immediately rejects unpaid, fellowship, volunteer, or survey scam postings (`pollfish`, etc.).
- **Seniority Shield**: Skips listings requiring 6+, 7+, 8+, 10+, 15+ years of experience.
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
  - Discovers detached question labels by traversing nearest preceding sibling headers.
  - **Phone Inputs**: Fills `applicant_phone` with digit validation and sequential typing fallback. Never submits hardcoded fake numbers.
  - **Email Inputs**: Fills `applicant_email`.
  - **Location Comboboxes**: Types configured city and clicks typeahead suggestion.
  - **Numeric Inputs**: Validates whole integers (0–99). Adjusts salary multipliers (LPA to annual INR) based on input `min` attributes.
  - **Date Inputs**: Converts configured dates to ISO `YYYY-MM-DD` or custom Indian formats (`dd-mm-yyyy`). Never fabricates today's date if unconfigured.
  - **Exam Percentiles / Marks**: Leaves unconfigured grades blank to avoid fraud; escalates to review.
  - **Field-Length Guard (`_fit_field_length`)**: Automatically truncates answers to input's `maxlength` attribute, preventing *"Invalid input"* validation stalls.
  - **Synthetic Events**: Dispatches native `input`, `change`, and `blur` events on all modified inputs.

### 3. Screen Review Escalation Guard
If any answer relied on generic tenure inferences (`experience:unlisted_default`, `willingness_affirmative`, etc.):
- Captures screenshot of the form.
- Safely closes modal.
- Returns `status = NEEDS_REVIEW`, `reason = SkipReason.UNANSWERED_QUESTION`, and alerts via Telegram.

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
- Clicks `button:has-text('Submit application')` once.

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
   - Clicks `[role='dialog'] button:text-is('Done')` to dismiss receipt cleanly.
2. **Dedicated Job Page Status**:
   - Inspects `/jobs/view/{job_id}`:
     - `main button:text-is('Applied')`
     - `main span:text-is('Application submitted')`
3. **Outcome Resolution**:
   - **Confirmed**: Returns `status = APPLIED`, `confirmation_type = "dom_marker"`, `confirmation_evidence = "LinkedIn explicit submission receipt or target listing applied state"`.
   - **Unconfirmed**: Captures inline error feedback, dismisses modal cleanly with Discard confirmation (`_dismiss_modal`), and returns `status = FAILED`, `reason = SkipReason.SUBMISSION_UNCONFIRMED`.

---

## 6. Invariants & Anti-Patterns (Do NOT Break)
1. **DO NOT assume modal closing means applied**: `test_closed_modal_without_confirmation_fails` guarantees that an unconfirmed dismissal fails closed.
2. **DO NOT click through unverified form errors**: If LinkedIn highlights a required field in red, capture diagnostics and escalate to review.
3. **DO NOT guess numeric question fields**: Fabricating answers triggers recruiter spam reports and damages candidate trust score.
4. **DO NOT use broad unified queries**: Execute sequential, track-specific queries per profile to preserve search relevance.
5. **DO NOT apply without Easy Apply verification**: If the button says "Apply on company site", route to `EXTERNAL` with the resolved ATS link.