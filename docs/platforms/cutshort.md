# Cutshort Platform Playbook

Comprehensive architecture and operational specification for the Cutshort apply adapter. This document mirrors the runtime logic in [`naukri_agent/platforms/cutshort.py`](../../naukri_agent/platforms/cutshort.py), [`filters.py`](../../naukri_agent/core/filters.py), and [`answers.py`](../../naukri_agent/core/answers.py).

---

## 1. Authentication & Session Management
- **Target URL**: `https://cutshort.io/`
- **Session Verification**: Validates candidate profile avatar, navigation menu, and access to `https://cutshort.io/profile/all-jobs`.
- **Overlay & Survey Dismissal**:
  - `_dismiss_dashboard_overlays()`: Dismisses onboarding popups.
  - `_dismiss_availability_prompt()`: Automatically handles and closes availability checks (*"Are you actively looking?"*).
  - `_skip_feedback_survey()`: Skips feedback rating modals (*"How is your experience?"*) to prevent blocked click targets.
- **Perimeter Defense**: If a Cloudflare challenge is encountered (`_cloudflare_wall`), the platform pauses gracefully.

---

## 2. Ingestion & Dynamic Feed Traversal ([`fetch_jobs`](../../naukri_agent/platforms/cutshort.py))
- **Discovery Sources**:
  - `https://cutshort.io/profile/all-jobs` (Traverses both the Recommended tab and the All Jobs feed).
- **Feed Preparation**:
  - `_set_recommendation_switch(want_on=True)`: Ensures the candidate feed is set to personalized AI recommendations.
  - `_apply_ui_filters(profile)`: Programmatically configures UI filters (skills filter badge, experience ranges, and salary expectations).
  - `_scroll_feed()`: Drives dynamic DOM scrolling until the target candidate pool is loaded.
- **Extracted Attributes**:
  - `job_id`: Normalized as `cutshort-<slug_or_id>`.
  - `title`, `company`, `location`, `salary_text` (`_parse_cutshort_salary`), `experience_text` (`_parse_cutshort_experience`), `tags`, `posted_days_ago` (`_parse_cutshort_posted`).
- **Deduplication Pre-Filter**:
  - `repo.known_job_ids()` database window check.
  - `_company_rejection()` collapses duplicate postings from the same company.

---

## 3. Screening & Multi-Tier Filter Pipeline
1. **Card Filter ([`FilterEngine.evaluate_card`](../../naukri_agent/core/filters.py))**:
   - Matches role keywords against `title_must_include_any`.
   - Experience bounds check against profile limits (e.g. 0 to 3.5y).
   - Rejects non-technical titles and blocked company profiles.
2. **Detail Filter ([`FilterEngine.evaluate_detail`](../../naukri_agent/core/filters.py) & `pre_submit_check`)**:
   - Opens the listing description, reads full text.
   - Validates technology stack compatibility and rejects hard negative requirements.

---

## 4. Application Flow & Pitch Submission ([`apply_to_job`](../../naukri_agent/platforms/cutshort.py))

### 1. Opening Opportunity Modal
- Locates the target card on the feed.
- Verifies if the card is already applied (`button:text-is('Applied')` or `a:text-is('View conversation')`). If so, returns `ALREADY_APPLIED`.
- Clicks the native **Apply now** control.

### 2. Track-Aware Resume Selection ([`_track_resume_for`](../../naukri_agent/platforms/cutshort.py))
- Evaluates the job description to determine track (`want_ai` vs `want_fullstack`).
- Selects the matching resume file and label corresponding to the target role.

### 3. Pre-Send Pitch & Screening Chips
- **Pitch Injection**: Injects the customized candidate pitch into the message textarea.
- **Company Follow Checkbox**: Unchecks optional company marketing follow if desired.
- **Pre-Send Screening Chips**:
  - Frequently, Cutshort places screening chips (e.g. Notice Period, Current CTC, Primary Tech) **inside the modal before the Send button**.
  - Invokes `CutshortChatbot(allow_submit=False)`:
    - Answers radio chips, dropdowns, and date inputs using [`AnswerEngine`](../../naukri_agent/core/answers.py).
    - `_click_option_verified()` asserts visual selection (`active` class or `aria-checked="true"`).
    - `allow_submit=False` ensures it answers the chips **without** attempting to click submit prematurely.

### 4. Single Submit Action
- Clicks the primary **Send** button exactly once.

### 5. Post-Send In-Modal Questionnaire
- If an in-modal multi-step questionnaire or chatbot appears immediately post-send, answers questions from `AnswerEngine`.
- Closes the application modal cleanly to avoid DOM backdrop interference.

---

## 5. Compulsory Post-Submit Verification
> [!IMPORTANT]
> **Strict Landing Audit**: In earlier iterations, modal closing, textarea disappearance, or detached DOM handles were mistakenly treated as applied. Cutshort modals can detach on network drops. The adapter mandates explicit server state confirmation on the actual job URL.

1. **Dedicated Job Page Navigation**:
   - Calls `page.goto(job.url, wait_until="domcontentloaded")`.
   - Uses `urlsplit` to assert that the current URL hostname and path match `https://cutshort.io/job/<id>`.
2. **Authoritative Applied Markers**:
   - Inspects the target job page for:
     - `button:text-is('Applied')`
     - `a:text-is('View conversation')`
     - `button:text-is('View conversation')`
     - `text=You have already applied`
3. **Outcome Resolution**:
   - **Confirmed**: Returns `status = APPLIED`, `confirmation_type = "dom_marker"`, `confirmation_evidence = "Cutshort target job page shows applied state after navigation"`.
   - **Unconfirmed**: Captures diagnostic state dump, returns `status = FAILED`, `reason = SkipReason.SUBMISSION_UNCONFIRMED`.

---

## 6. Messages Sweep (Post-Apply Assessments) ([`handle_messages`](../../naukri_agent/platforms/cutshort.py))
Cutshort employers frequently send screening questionnaires to the candidate's **Messages** inbox after application:
1. **Inbox Polling**:
   - Navigates to `https://cutshort.io/messages` and opens the **Awaiting reply / Pending** tab (`_open_awaiting_tab`).
2. **Thread Verification**:
   - Inspects candidate threads, skipping stale threads (`_is_stale_thread > 3 days`).
   - Verifies that the thread links to an eligible, agent-applied role.
3. **Autonomous Questionnaire Execution ([`CutshortChatbot`](../../naukri_agent/platforms/cutshort.py))**:
   - Answers radio options, text inputs, chips, and date inputs (`_resolve_date_answer` formats ISO and Indian date formats for notice period/last working day).
   - If a confirmation alert appears (*"Responses cannot be changed after submission. Are you sure?"*), clicks **Yes** once.
4. **Receipt Validation**:
   - Requires explicit confirmation text: `"Questionnaire submitted!"`, `"Your response has been recorded"`, or `"Thanks for your response"`.
   - Unresolved questions are escalated to `question_review` with Telegram notifications.
   - Closes the chat overlay cleanly (`_close_chat_overlay`) to reset state.

---

## 7. Invariants & Anti-Patterns (Do NOT Break)
1. **DO NOT assume modal detachment means applied**: Detached modals occur on network timeouts and DOM re-renders.
2. **DO NOT use loose substring matches like `:has-text('Applied')`**: These match peripheral UI text, sidebar recommendations, and unrelated conversation links. Use `:text-is('Applied')`.
3. **DO NOT bypass `/job/<id>` navigation**: Visiting the dedicated job URL is the sole proof of delivery.
4. **DO NOT answer free-form recruiter chat messages automatically**: Only structured questionnaires are automated; recruiter conversational threads must be reviewed.
