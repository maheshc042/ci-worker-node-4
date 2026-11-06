# Wellfound Platform Playbook

Workflow and implementation reference for the Wellfound (AngelList Talent) apply adapter. This guide describes the workflow and flags verification limits in the implementation in [`naukri_agent/platforms/wellfound.py`](../../naukri_agent/platforms/wellfound.py), [`filters.py`](../../naukri_agent/core/filters.py), and [`gemini_writer.py`](../../naukri_agent/core/gemini_writer.py).

---

## 1. Authentication & Session Management
- **Target URL**: `https://wellfound.com/jobs`
- **Session Verification**: Inspects candidate profile elements, navigation links, and absence of login/sign-up banners.
- **Challenge Gate**: If Wellfound prompts for account registration (*"Sign up to apply"*, *"Log in with your account to apply"*) or presents a perimeter check, authentication can fail and the orchestrator handles the failure. A registration prompt encountered inside Apply returns a job failure rather than necessarily pausing the platform.

---

## 2. Job Discovery & Infinite Scroll Ingestion ([`fetch_jobs`](../../naukri_agent/platforms/wellfound.py))
- **Discovery Feed**: `https://wellfound.com/jobs` (Candidate Feed).
- **Stabilized Infinite Scroll**:
  - Executes dual window and element scrolling: `window.scrollTo(0, document.body.scrollHeight)`.
  - Automatically clicks dynamic pagination controls (*"Load more"*, *"Show more"*, *"View more"*).
  - Emulates keyboard `End` and mouse wheel actions (`mouse.wheel(0, 1500)`) to trigger virtualized card hydration.
  - Allows up to 40 scroll cycles, requiring 5 consecutive stagnant cycles of unique job IDs before terminating.
- **Extracted Attributes**:
  - `job_id`: Extracted via regex `r"/jobs/(\d+)-"` and normalized as `wellfound-<numeric_id>`.
  - `title`, `company`, `location`, `salary_text`, `tags`, `posted_days_ago`.
- **Deduplication Pre-Filter**:
  - `repo.known_job_ids()` database window check.
  - Collapses same-company duplicate postings.

---

## 3. Screening & Multi-Tier Filter Pipeline
Every job card selected for application is evaluated through two comprehensive filter layers:

### Gate A: Card & Initial URL Screen
- Matches role keywords against `title_must_include_any`.
- Checks experience bounds against profile limits (e.g. 0 to 3.5 years).
- Rejects blacklisted company profiles and unallowed locations.

### Gate B: Detail Listing Screen ([`_read_description`](../../naukri_agent/platforms/wellfound.py))
- Navigates to `job.url` and verifies the target job with `_wellfound_job_page`. Supported URL variants are defined in that helper.
- **Pre-Existing Check**: Checks `_APPLIED_JOB_MARKERS` immediately upon page load. If present, returns `ALREADY_APPLIED`.
- **Full Description Extraction**: Scrapes the entire job description text via `_DESCRIPTION_SELECTORS` (`data-test='JobDescription'`, etc.).
- **Freshness Re-Check**: Parses posted date from detail header. If older than `MAX_POSTED_DAYS` (7 days), skips as `FILTER_FRESHNESS`.
- **Suitability Screen (`_wellfound_suitable`)**: Rejects non-technical titles, unpaid equity-only arrangements, and high-tenure foreign on-site roles.
- **JD Skill Match (`_profile_must_skills`)**: Requires at least one core skill from the candidate's profile stack to appear in the combined title and JD text (`_stack_hit`).
- **Orchestrator Pre-Submit Check (`pre_submit_check`)**: Evaluates global candidate filters before opening the application dialog.

---

## 4. Application Flow & Dialog Traversal ([`apply_to_job`](../../naukri_agent/platforms/wellfound.py))

### 1. Apply Button & Prerequisite Validation
- Locates primary button: `button:text-matches('^Apply( now| on Wellfound)?$', 'i')`.
- **Disabled Button Guard**: If the button is disabled (due to an unselected profile location or missing prerequisite), fails fast as `NEEDS_REVIEW` (`STUCK_FLOW`) with actionable text rather than hanging until timeout.
- **External ATS Handoff**: If clicking navigates away from `wellfound.com`, routes immediately to `EXTERNAL`.

### 2. Modal-Level In-Flight Gates
- **Experience Requirement Gate**: Reads `experience: X+ years` from the dialog. A stronger requirement or an explicit value replacing an estimate is evaluated against the same configured detail gate before Send.
- **Location-Gating**: Detects *"not accepting applications from your location"* and skips with `BLOCKED_LOCATION`.
- **Relocation Prompt**: If Wellfound asks the candidate to confirm relocation, captures the prompt for job-specific review and stops before Send.

### 3. Dual-Strategy Pitch Generation
The supported Wellfound application path looks for a pitch note such as *"What interests you about working for this company?"*.
- **Strategy 1: AI Customization (Gemini)**:
  - Invokes `gemini.generate_wellfound_pitch(role_name, job_description, company_name)`.
  - **Stub Rejection**: If Gemini generates a brief stub (< 150 characters), it is discarded to prevent unprofessional one-liners.
- **Strategy 2: Deterministic Fallback**:
  - Uses `candidate.wellfound_pitch` from [`config.yaml`](../../config/config.yaml) when configured. Otherwise uses a neutral role and company introduction, the configured name and available profile links.
- **React Input Dispatch**: Injects pitch via `textarea.fill(pitch)` and dispatches synthetic `input` and `change` events so React state synchronizes cleanly.
- **Pitch Retention Audit**: Verifies `await textarea.input_value()`. If the text was wiped by a React re-render, fails closed.

### 4. Form Submission
- Resolves submit button within the active form container: `button:text-matches('^(Send application|Submit application|Apply)$', 'i')`.
- Clicks the form's submit control once, then verifies the listing. A missing control or pitch-retention failure stops the job.

---

## 5. Compulsory Post-Submit Listing Verification
> [!IMPORTANT]
> **Persisted State Required**: Wellfound modal dismissals can be triggered by unhandled errors. The adapter reopens the exact listing URL to confirm that Wellfound persisted the application on the server.

1. **Reopen Exact Listing**:
   - Executes `page.goto(job.url, wait_until="domcontentloaded", timeout=25000)`.
   - Validates that the page is still the target listing via `_wellfound_job_page(self.page.url, job.job_id)`.
2. **Authoritative Applied Markers (`_APPLIED_JOB_MARKERS`)**:
   - Inspects the reloaded DOM for:
     - `button[data-test='Button']:not(nav button):text-matches('^[✓✔  ]*Applied$', 'i')`
     - `button:not(nav button):text-is('Applied')`
     - `main :text-matches('^You applied (on|.{0,20}ago)', 'i')`
3. **Outcome Resolution**:
   - **Confirmed**: Returns `status = APPLIED`, `confirmation_type = "dom_marker"`, `confirmation_evidence = "Wellfound target listing shows Applied after reopening"`.
   - **Unconfirmed**: Captures failure screenshot `wellfound-submit-unconfirmed`, returns `status = FAILED`, `reason = SkipReason.SUBMISSION_UNCONFIRMED`, and distinguishes an unresolved queued send from a missing persisted receipt.

### Queued-send window (run 706)

Wellfound can show `button[data-test='JobApplicationUndoButton']` with `Undo · 4:50` after submission and a listing reload. Its tooltip explicitly says cancellation is possible before sending. This is a persisted pending send, not a final Applied receipt and not proof that submission failed.

`_application_state(job)` validates the exact listing URL and reads controls within `[data-test='JobDetail']`. A queued application is never submitted again. `_verify_application(job)` reloads in bounded intervals for up to 330 seconds while Undo remains, then requires settled Applied evidence. An active Apply control, lost target, or unresolved countdown cannot establish success. The orchestrator allows Wellfound 420 seconds per job to cover form work and the five-minute window; other platforms retain 300 seconds. The overall run budget still applies. A timeout remains submission-unconfirmed to prevent an ordinary failure retry from sending again.

Read-only inspection of run 706's four target URLs found all four settled Applied receipts. Local replay checked all four archived pending pages, all four current pages, and four pending-to-settled reload transitions. No new application was sent during this audit and historical run outcomes were not rewritten. See the [run 706 audit](../../analysis/run706_wellfound_audit.md).

---

## 6. Invariants & Anti-Patterns (Do NOT Break)
1. **DO NOT rely on modal disappearance**: Modals close on click-aways and network errors. The current adapter verifies the exact listing after navigation.
2. **DO NOT use loose selectors like `button:has-text('Applied')`**: These match peripheral tags, sidebars, or recommended jobs. Use `_APPLIED_JOB_MARKERS`.
3. **DO NOT submit one-liner pitch stubs**: Maintain high founder appeal; pitches must be structured, technical, and complete.
4. **DO NOT hang on disabled Apply buttons**: Fail fast to `NEEDS_REVIEW` so profile location or screening blockers can be resolved.


## Evidence and implementation limits

No fresh Wellfound submission was performed during this audit. Current receipts and saved-page replay establish the observed failure mechanism, not comprehensive future reliability.

- Run 706 exposed a mandatory model-training/MLOps ownership role passing a broad AI title/skill match. The unified profile now excludes the observed mandatory phrases and no longer lists MLOps as a core candidate skill. Optional MLOps exposure remains eligible. These narrow rules are not a general mandatory-skill parser.
- Low absolute rupee compensation without a pay period now returns NEEDS_REVIEW before applying, rather than passing through the generic sub-1-LPA phantom-salary bypass. Do not infer monthly versus annual pay. This is a compensation blocker, not a candidate screening question for the answer-training queue.

- Run 708 rejected all seven listings at planning, so that run did not exercise the form or receipt path. The configured experience recheck has regression coverage; the updated form flow has not sent a fresh live application.
- This adapter has a pitch workflow, not a general questionnaire solver. Unsupported custom questions need review handling rather than an assumption that the pitch is the only field.
- The adapter can read an existing receipt without a detail callback. Any new application or external route requires that callback to pass first.
