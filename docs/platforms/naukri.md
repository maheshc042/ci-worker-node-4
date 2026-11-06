# Naukri Platform Playbook

Workflow and implementation reference for the Naukri apply adapter. This guide describes the workflow and flags verification limits in the implementation in [`naukri_platform.py`](../../naukri_agent/platforms/naukri_platform.py), [`apply.py`](../../naukri_agent/naukri/apply.py), [`chatbot.py`](../../naukri_agent/naukri/chatbot.py), and [`filters.py`](../../naukri_agent/core/filters.py).

---

## 1. Authentication & Session Management
- **Flow**: Managed via [`naukri_agent/naukri/auth.py`](../../naukri_agent/naukri/auth.py).
- **Session Persistence**: The browser manager persists account-specific session state through the repository. Check the configured storage and encryption settings before describing their guarantees.
- **Account Separation**: Dual-account support (`primary` and `secondary`). Active resumes and daily application caps are tracked independently.
- **Challenge Gate**: If Naukri demands OTP or Cloudflare/perimeter challenges, authentication stops and the orchestrator handles the failure. This does not automatically create a question-review item. **Never** attempt to bypass or solve challenges automatically.

---

## 2. Job Discovery & Feed Ingestion
- **Primary feed**: `https://www.naukri.com/mnjuser/recommendedjobs`. This page has no experience slider.
- **Experience-filtered Search**: Profiles with `naukri_experience_search_terms` add up to 12 cards per configured term from the first results page. The agent uses the same `experience` query parameter set by the native left sidebar slider and checks that the visible slider and URL both confirm the profile's whole years. A failed check leaves the Search page out of the candidate pool. Search shares native job IDs, dedupe, planner rules and the mandatory description gate with Recommendations.
- **Extracted Attributes**:
  - `job_id`: Extracted from card URL (e.g., `reco-<numeric_id>`).
  - `title`, `company`, `location`, `min_experience`, `max_experience`, `salary_text`, `posted_days_ago`, `tags`, `is_walkin`.
- **Deduplication Pre-Filter**:
  - `repo.known_job_ids()`: Checks database for existing terminal outcomes (`applied`, `already_applied`, `external`, `needs_review`) within `dedupe_window_days` (model default 90 days, configurable).
  - Ordinary failures use `failed_retry_cooldown_hours` (model default 12 hours). Unconfirmed submissions remain known for the full dedupe window. Resolved review jobs can re-enter under the attempt budget.
  - `_company_rejection()`: Enforces daily per-company application limits and collapses same-run duplicate reposts.

---

## 3. Screening & Multi-Tier Filter Pipeline
Every discovered listing is evaluated across two distinct filter gates before an application is attempted:

### Gate A: Card-Level Filter ([`FilterEngine.evaluate_card`](../../naukri_agent/core/filters.py))
1. **Title Inclusion**: Match the profile's role keywords, including `AIML` as `AI/ML`. Naukri's bare `Tester` and `Testing Engineer` titles can reach detail review for QA-enabled profiles. The full description must identify software QA before they can reach Apply or Sidekick. Other generic titles can still require matching card skills.
2. **Title Exclusion & Disjoint Specialization**: Rejects titles matching `title_must_not_include_any` or disjoint specializations (e.g., Salesforce, SAP, Mainframe, Embedded, Mobile/iOS/Android, DevOps when looking for Dev, etc.).
3. **Experience Bounds**:
   - `job.min_experience > exp.max_years` (rejects requisitions requiring more experience than candidate ceiling).
   - `job.max_experience < exp.min_years` (rejects fresher/internship positions below candidate floor).
   - Dedicated AI/ML gate: If the role is dedicated AI/ML, enforces `dedicated_ai_max_years` (e.g., 3.0y).
   - Seniority Imputation: Titles with Senior, Lead, Staff, Principal, Architect impute seniority bounds even when raw numeric years are missing.
4. **Compensation**: Rejects jobs whose maximum salary is strictly below `min_salary_lpa`.
5. **Freshness**: Rejects jobs posted more than `max_posted_days` ago (default 3 days).
6. **Walk-in Drives**: Uses configured walk-in policy and home-location filtering. The application engine can separately skip a walk-in page with no native online route.
7. **Company Blacklist**: Rejects companies listed in `blocked_companies`.
8. **Location**: Rejects locations in `blocked_locations` or outside `allowed_locations`.

### Gate B: JD-Level Filter ([`FilterEngine.evaluate_detail`](../../naukri_agent/core/filters.py) & `pre_submit_check`)
- Opens the exact job listing URL.
- Reads the full job description text.
- Rejects listings containing configured negative phrases, after normalizing whitespace. This is a phrase filter, not a complete parser of every mandatory skill, qualification or skill-specific tenure requirement.
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
   - **Applied Marker**: Require exact applied state on the reloaded listing, including the observed `span#already-applied` with exact `Applied` text. A confirmation title or URL alone is insufficient.
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
2. **DO NOT skip `_verify_post_submit(job)`**: Keep the reload verification path. A title or URL alone must not establish a receipt.
3. **DO NOT treat answering screening questions as proof of application**: Answers can be rejected by the server after the drawer closes.
4. **DO NOT guess tenure or skill questions**: Send unlisted questions to `question_review` so the human can train the knowledge base.


## Evidence and implementation gaps

The runs 702–703 audit removed the title/URL-only confirmation branch and image-only success marker, and tightened applied selectors to exact text. Read-only run 704 reopened all 13 reported application URLs and saved persisted `span#already-applied` receipts. This verifies current application state; it does not replay the original submission or prove job fit.

The shared answer engine now holds unknown quantitative skill tenure for review, prevents contact-number collisions, and respects specific configured skill tenure over generic testing answers. Boolean skill adjacency remains a separate policy. Top Technical Skills can use the profile's configured core skills. Self-ratings without an approved value/scale and unlisted WebdriverIO tenure remain review items.

The [detailed audit](../../analysis/run702_703_audit.md) covers 324 planner/outcome records. Current descriptions for the 13 applications show nine explicit requirement/role conflicts, three supported stack routes and one unverified CDAC qualification. Narrow secondary-profile description exclusions address the observed conflicts, while preserving Java-or-Python alternatives. Arbitrary qualification and named-skill JD tenure requirements are not comprehensively enforced. Archived rejected rows lack full original descriptions, so rejection recall cannot be certified. No new applications or review-queue resolutions were performed during this audit.

### Primary run 716

Run 716 selected 25 of 193 planner inputs. It recorded seven applications, eight existing applications, seven external routes, three screening reviews and one failure. A read-only recheck found the exact persisted Applied receipt and read the description for all seven new applications.

AtkinsRéalis's Water Quality Analyst/Modeller matched the software profile's `quality analyst` title term. The role calls for civil or water resources qualifications and water-industry experience. The disjoint-title gate now catches water quality and related modeller titles. Both Naukri profiles also exclude the observed explicit water-industry tenure wording at the description gate. The primary profile now catches the exact lead analyst wording in MoveInSync's generic Data Analyst posting and the mandatory ClickHouse wording in the steel manufacturer's Data Engineer posting. All three cases reject when replayed against the current listing text. The rules preserve the ordinary QA title and optional ClickHouse references.

Jero's Agentic AI Automation Engineer posting asks for hands-on low-code workflow experience and accepts tools similar to Power Automate. Confirm equivalent experience separately. Datamatics again opened a screening drawer without a detectable question. Run 716's evidence does not identify the widget layout needed to correct that path.

The seven external-route listings and three screening-review listings were also reopened read-only. Acenet explicitly asks for three years of relevant AI/LLM development, above the two years configured for the primary profile. nVent asks for ERP, supply-chain or procurement data experience. These requirements expose remaining fit gaps in the route gate. Naukri showed Applied on the routed listings, while the run recorded Sidekick queueing. Neither verifies submission at the destination ATS.

### Rejection audit and experience discovery

The 108 attached rejection rows were all present in run 716's 168 planner rejections. The planner rejected 153 by title, 14 by freshness and one by experience. A read-only description check of 16 ambiguous cases found that Arvind's Tester and Prescient's Testing Engineer were software testing roles worth inspecting. Intellect Design Arena's AIML title exposed a spelling gap, but its description requires a specialist model-development stack. TTEC, Akamai, Mindteck, Vertiv and DXC had independent domain or stack mismatches. Qapitol's prompt role combines different seniority bands and remains a manual fit decision. Older listings remain subject to the three-day freshness rule.
