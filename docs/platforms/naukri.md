# Naukri Platform Playbook

Comprehensive architecture and operational specification for the Naukri apply adapter. This document mirrors the runtime logic in [`naukri_platform.py`](../../naukri_agent/platforms/naukri_platform.py), [`apply.py`](../../naukri_agent/naukri/apply.py), [`chatbot.py`](../../naukri_agent/naukri/chatbot.py), and [`filters.py`](../../naukri_agent/core/filters.py).

---

## 1. Authentication & Session Management
- **Flow**: Managed via [`naukri_agent/naukri/auth.py`](../../naukri_agent/naukri/auth.py).
- **Session Persistence**: Cookies and local storage are saved encrypted in PostgreSQL (`account_cookies`).
- **Account Separation**: Dual-account support (`primary` and `secondary`). Active resumes and daily application caps are tracked independently.
- **Challenge Gate**: If Naukri demands OTP or Cloudflare/perimeter challenges, the platform marks the run as paused/needs review. **Never** attempt to bypass or solve challenges automatically.

---

## 2. Job Discovery & Feed Ingestion
- **Feed URL**: `https://www.naukri.com/mnjuser/recommendedjobs` (Recommended feed only).
- **Extracted Attributes**:
  - `job_id`: Extracted from card URL (e.g., `reco-<numeric_id>`).
  - `title`, `company`, `location`, `min_experience`, `max_experience`, `salary_text`, `posted_days_ago`, `tags`, `is_walkin`.
- **Deduplication Pre-Filter**:
  - `repo.known_job_ids()`: Checks database for existing terminal outcomes (`applied`, `already_applied`, `external`, `needs_review`) within `dedupe_window_days` (default 30 days).
  - Failed applications are kept in the dedupe window for `failed_retry_cooldown_hours` (default 12 hours) to avoid hammering broken listings on the same day.
  - `_company_rejection()`: Enforces daily per-company application limits and collapses same-run duplicate reposts.

---

## 3. Screening & Multi-Tier Filter Pipeline
Every discovered listing is evaluated across two distinct filter gates before an application is attempted:

### Gate A: Card-Level Filter ([`FilterEngine.evaluate_card`](../../naukri_agent/core/filters.py))
1. **Title Inclusion**: Must match at least one keyword in `title_must_include_any`. If the title is generic (e.g., "Software Engineer"), listing tags must contain matching tech skills.
2. **Title Exclusion & Disjoint Specialization**: Rejects titles matching `title_must_not_include_any` or disjoint specializations (e.g., Salesforce, SAP, Mainframe, Embedded, Mobile/iOS/Android, DevOps when looking for Dev, etc.).
3. **Experience Bounds**:
   - `job.min_experience > exp.max_years` (rejects requisitions requiring more experience than candidate ceiling).
   - `job.max_experience < exp.min_years` (rejects fresher/internship positions below candidate floor).
   - Dedicated AI/ML gate: If the role is dedicated AI/ML, enforces `dedicated_ai_max_years` (e.g., 3.0y).
   - Seniority Imputation: Titles with Senior, Lead, Staff, Principal, Architect impute seniority bounds even when raw numeric years are missing.
4. **Compensation**: Rejects jobs whose maximum salary is strictly below `min_salary_lpa`.
5. **Freshness**: Rejects jobs posted more than `max_posted_days` ago (default 3 days).
6. **Walk-in Drives**: Rejects walk-in interviews outside the candidate's home location (Bengaluru).
7. **Company Blacklist**: Rejects companies listed in `blocked_companies`.
8. **Location**: Rejects locations in `blocked_locations` or outside `allowed_locations`.

### Gate B: JD-Level Filter ([`FilterEngine.evaluate_detail`](../../naukri_agent/core/filters.py) & `pre_submit_check`)
- Opens the exact job listing URL.
- Reads the full job description text.
- Rejects listings containing negative phrases (e.g., rotational shifts, bonds, mandatory female diversity, non-negotiable tech stacks not possessed by the candidate).
- Rejects foreign external ATS redirects if `require_easy_apply=True` (or routes to `EXTERNAL` for Sidekick processing).

---

## 4. Application Execution ([`ApplyEngine._submit`](../../naukri_agent/naukri/apply.py))
1. **Pre-Click Button State**:
   - Inspects the live button state (`_submit_button_state()`).
   - If the button is enabled and has text `"apply"`, sets `started_unapplied = True`.
2. **Click Action**:
   - Clicks `button#apply-button` or selectors from `S.JD_APPLY_BUTTON`.
3. **Modal & Drawer Handling**:
   - **1-Click Apply**: Direct submission without screening questions.
   - **Late-Drawer Recovery**: If the chatbot drawer hydrates after a delay, it is intercepted before any page reload.
   - **Screening Chatbot (`ChatbotHandler`)**:
     - Resolves questions through [`AnswerEngine`](../../naukri_agent/core/answers.py) (strict option matching, tenure comparisons, salary confirmations, location checks).
     - If candidate lacks eligibility (e.g., hard tenure shortfall) -> returns `SKIPPED` (`unfit`).
     - If an unconfigured question is encountered -> captures screenshot and returns `NEEDS_REVIEW`.

---

## 5. Compulsory Post-Submit Server Reload Verification
> [!IMPORTANT]
> **Zero False-Positive Rule**: The agent NEVER assumes an application succeeded based on button clicks, modal dismissals, or client-side meta tags.

1. **Mandatory Reload**:
   - Calls `_verify_post_submit(job)` (`page.goto(job.url)`).
2. **Reload Evaluation**:
   - **Primary Button State**: If the primary button still reads `"Apply"` (active, unapplied), verification returns `"unknown"`.
   - **Applied Marker**: If the primary button displays `"Applied"` or page shows `/apply/confirmation`, verification returns `"applied"` or `"already_applied"`.
3. **Outcome Resolution**:
   - If `verdict == "applied"` OR (`verdict == "already_applied"` and `started_unapplied == True`):
     - Marks `status = APPLIED`, `confirmation_type = "verify_pass"`.
   - If `verdict == "already_applied"` and `started_unapplied == False`:
     - Marks `status = ALREADY_APPLIED`.
   - If reloaded page still displays `"Apply"`:
     - Captures screenshot `apply-unconfirmed-reload`.
     - Marks `status = FAILED`, `reason = SkipReason.SUBMISSION_UNCONFIRMED`, `detail = "Naukri application state could not be confirmed after reopening the job"`.

---

## 6. Invariants & Anti-Patterns (Do NOT Break)
1. **DO NOT rely on `meta[name='atdlayout']` or title substring `"confirmation"`**: Naukri SPA pages include these tags even before an application is submitted.
2. **DO NOT skip `_verify_post_submit(job)`**: Server reload is the only source of truth.
3. **DO NOT treat answering screening questions as proof of application**: Answers can be rejected by the server after the drawer closes.
4. **DO NOT guess tenure or skill questions**: Send unlisted questions to `question_review` so the human can train the knowledge base.
