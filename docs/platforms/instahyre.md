# Instahyre Platform Playbook

Comprehensive architecture and operational specification for the Instahyre apply adapter. This document strictly mirrors the runtime logic in [`naukri_agent/platforms/instahyre.py`](../../naukri_agent/platforms/instahyre.py), [`filters.py`](../../naukri_agent/core/filters.py), and [`orchestrator.py`](../../naukri_agent/core/orchestrator.py).

---

## 1. Authentication & Session Management
- **Target URL**: `https://www.instahyre.com/candidate/opportunities/?matching=true`
- **Session Verification**:
  - Reuses active session if `a[href*='/candidate/profile']`, `div.employer-row`, or `button:has-text('Logout')` are visible.
  - In headed mode, waits up to 30 seconds for manual login if prompted.
  - In automated mode, populates email/password and submits credentials via `button:has-text('Login')`.

---

## 2. Dynamic UI Filters & Opportunity Discovery ([`_apply_ui_filters`](../../naukri_agent/platforms/instahyre.py))

### 1. Overlay & Modal Clearance ([`_dismiss_modals`](../../naukri_agent/platforms/instahyre.py))
Before configuring filters, clears blocking modals:
- Active search check (*"Are you looking for a job actively?"* $\rightarrow$ `div.candidate-active-check-modal button.btn-success`).
- Bulk-apply modal (*"Want to apply to other similar jobs?"* $\rightarrow$ `div.candidate-apply-all-modal button.back-button-modal-close`).
- Premium upsell / referral overlays (`div.application-modal-close`, `a:has-text('No thanks')`).
- Notification toast alerts (`#messages button.close`).

### 2. Search Section Expansion & Skill Cleansing
- **Accordion Toggle**: Expands *"Search other jobs"* heading (`.job-search-heading`, `h6:has-text('Search other jobs')`) if collapsed.
- **Fossil Tag Removal (`_clear_skill_tags`)**:
  - Loops up to 3 passes clicking tag remove controls (`div.selectize-input a.remove`).
  - Guarantees legacy tags (like historical SDET tags) do not skew the current persona search.
- **Max 15 Tag Ceiling**: Instahyre silently ignores skill tags past position 15; the adapter truncates and logs excess skills.

### 3. Skill Input & Autocomplete Selection ([`_select_skill`](../../naukri_agent/platforms/instahyre.py))
- Focuses `#skills-selectized` (bypassing actionability coverage issues when tags exist).
- Types target skills (e.g. `Python`, `Node.js`, `React.js`, `TypeScript`, `FastAPI`, `Next.js`, `Generative AI`, `LLMs`, `AWS`).
- Awaits exact visible suggestion in `.selectize-dropdown:visible .selectize-option`.
- Enforces a **15-second timebox per skill** to prevent frozen selectize inputs from stalling the entire run.
- **Unfiltered Fallback**: If $<50\%$ of configured skills land successfully, clears tags to fall back to an unfiltered feed (allowing downstream Python `FilterEngine` to screen jobs rather than starving the run).

### 4. Experience Filter & Submission
- Fills `input#years` with candidate's total experience years.
- Clicks search trigger: Supports modern renamed button `button.skills-search-btn` (*"Search"*), with fallback to legacy `button#show-results` (*"Show results"*).
- Dumps search results to `instahyre-search-results.html`.

---

## 3. Feed Ingestion & Card Scraping ([`_paginate_and_collect`](../../naukri_agent/platforms/instahyre.py))

### 1. Card Scraping Engine ([`_scrape_page_cards`](../../naukri_agent/platforms/instahyre.py))
- **Card Selectors**: `div.employer-row, div.opportunity-box, div.opportunity-card`.
- **Settle Budget**: 30 seconds for Page 1 (allowing complex 16-skill backend queries to complete); 8 seconds for subsequent pages.
- **Extracted Attributes**:
  - `company` and `title`: Parsed from `div.employer-job-name div.company-name`, split on `" - "`.
  - `tags`: Extracted from `ul.tags li`.
  - `location`: Parsed from `div.employer-locations span`.
  - `description`: Notes parsed from `div.employer-notes`.
- **Authoritative Opportunity ID Extraction**:
  - Evaluates card's native AngularJS scope: `window.angular.element(el).scope().opp.id`.
  - Normalizes `job_id` as `instahyre-<opp_id>` (falls back to stable hash only if Angular is not bootstrapped).

### 2. Pagination & Visited Page Memory
- Traverses pagination controls (`div.pagination li:has-text('Next')` or `li:text-is('{page+1}')`).
- Records all rendered pages into `self._visited_pages`.
- Returns browser cleanly to Page 1 upon fetch completion.

---

## 4. Application Flow & Opportunity Execution ([`apply_to_job`](../../naukri_agent/platforms/instahyre.py))

### 1. Clean Slate & Modal Shutdown ([`_ensure_modal_closed`](../../naukri_agent/platforms/instahyre.py))
- Triggers native AngularJS scope close method on `employerProfileModalCtrl`:
  ```javascript
  scope.closeApplyModal();
  if (scope.bulkParams) scope.bulkParams.showApplyModal = false;
  scope.$evalAsync();
  ```
- Clicks standard modal close buttons and presses `Escape`.
- Force-removes stray promotional backdrops (`.modal-backdrop, #go-premium-modal, #refer`) without destroying `.application-modal` templates.

### 2. Bounded Page Switching & Target Card Discovery
- Determines target pagination page from `job.recommendation_tab`.
- If on a different page, switches via `_switch_to_page(target_page)` (verifying `li.active` text).
- Locates target card via exact match: `div.employer-job-name div.company-name` matching `"{company} - {title}"`.
- **Phantom Page Guard**: If card is not found on the expected page, scans ONLY across `_visited_pages` (max 6 pages). Prevents chasing unrendered phantom pages (run 366 fix).
- **Stale Job Skip**: If the card has disappeared due to feed churn, returns `status = SKIPPED`, `reason = SkipReason.STALE_JOB`.

### 3. Pre-Apply State Check
- Inspects card for `button:text-is('Applied')` or `span.applied-badge:text-is('Applied')`.
- If present, returns `ALREADY_APPLIED`.

### 4. Opening Opportunity Modal
- Clicks `a#employer-profile-opportunity[ng-click='openApplyModal(opp)']`.
- Awaits modal: `.candidate-apply-modal > .application-modal-block` or `#employer-profile-modal`.
- Dumps modal HTML to `instahyre-modal-{job_id}.html`.

### 5. Detail Screening & External ATS Handoff
- Reads full description from `#job-description .job-description`.
- Extracts JD experience bounds (`extract_jd_experience`).
- Runs `pre_submit_check(job)`. If rejected, closes modal and skips with filter decision.
- **External Company Site Link**:
  - Checks for `a:text-is('Apply on company site')`.
  - If target is a valid external URL, closes modal and returns `status = EXTERNAL`, `reason = SkipReason.EXTERNAL_APPLY`, and routes the URL to Sidekick.

### 6. Native Single Apply Click
- Verifies dry-run policy (`policy.may_mutate`). If testing, skips with `SkipReason.DRY_RUN`.
- Scrolls action bar into view (`div.bar-actions`).
- Locates primary Apply button: `div.apply[ng-click='submitChoice(opp, true)'] button:text-is('Apply')`.
- Clicks Apply once.

---

## 5. Compulsory Target Card Verification
> [!IMPORTANT]
> **No Carousel Heuristics**: In earlier versions, carousel advancement or modal closing was assumed to mean applied. Rate limits or unhandled alerts also close the modal. Verification now strictly demands card-level proof.

1. **Card-Level Proof**:
   - Re-finds the exact target card on the active feed view.
   - Waits up to 8 seconds for:
     - `button:text-is('Applied')`
     - `span.applied-badge:text-is('Applied')`
2. **Outcome Resolution**:
   - **Confirmed**: Returns `status = APPLIED`, `confirmation_type = "dom_marker"`, `confirmation_evidence = "Instahyre target card shows Applied"`.
   - **Unconfirmed**: Closes modal, dismisses popups, and returns `status = FAILED`, `reason = SkipReason.SUBMISSION_UNCONFIRMED`, `detail = "Submission clicked but no Instahyre success confirmation was observed"`.

---

## 6. Invariants & Anti-Patterns (Do NOT Break)
1. **DO NOT treat modal closure as success**: A modal closing unexpectedly is usually a sign of an unhandled alert or backdrop error.
2. **DO NOT rely on carousel advancing**: Carousel advancing happens on "Not interested" or skip clicks as well.
3. **DO NOT chase phantom pages**: Only search pages that were actually rendered during the fetch phase (`_visited_pages`).
4. **DO NOT use loose class selectors like `div[class*='applied']`**: These match peripheral UI containers. Use exact text matches `:text-is('Applied')`.
5. **DO NOT destroy `.application-modal` containers**: Force-remove only `.modal-backdrop` and promotional modals (`#go-premium-modal`, `#refer`). Destroying Angular templates breaks subsequent card openers.