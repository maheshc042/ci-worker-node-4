# Instahyre Platform Playbook

Workflow and implementation reference for the Instahyre apply adapter. This guide describes the current workflow and flags verification limits in the implementation in [`naukri_agent/platforms/instahyre.py`](../../naukri_agent/platforms/instahyre.py), [`filters.py`](../../naukri_agent/core/filters.py), and [`orchestrator.py`](../../naukri_agent/core/orchestrator.py).

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
  - Uses the skill input's native select-all/backspace gesture to clear collapsed tags, with up to three verified remove-control fallback passes. Count only real `data-value` items, excluding `+N more`.
  - Attempts to clear stale tags and checks the UI. Partial filter application remains possible and must be diagnosed from logs.
- **Max 15 Tag Ceiling**: Instahyre silently ignores skill tags past position 15; the adapter truncates and logs excess skills.

### 3. Skill Input & Autocomplete Selection ([`_select_skill`](../../naukri_agent/platforms/instahyre.py))
- Focuses the current `#skills-drop-select-job-search-selectized`, with legacy skill-input fallbacks. Skill tag clearing/inspection is scoped away from company and location facets.
- Types target skills (e.g. `Python`, `Node.js`, `React.js`, `TypeScript`, `FastAPI`, `Next.js`, `Generative AI`, `LLMs`, `AWS`).
- Awaits exact visible suggestion in `.selectize-dropdown:visible .selectize-option`.
- When the current UI offers an exact `Add <term> …` control, selects it through the UI as a free-text skill/title keyword. This is not proof of a taxonomy-backed skill or a candidate skill claim; inspect actual search results. Never inject a tag through JavaScript.
- Enforces a **15-second timebox per skill** to prevent frozen selectize inputs from stalling the entire run.
- **Unfiltered Fallback**: If the landed count is below `max(1, len(target_skills) // 2)`, clears tags to fall back to an unfiltered feed (allowing downstream Python `FilterEngine` to screen jobs rather than starving the run).

### 4. Experience Filter & Submission
- Fills `input#years` using the configured Instahyre override first, followed by the profile, shared total tenure or profile ceiling. Values are converted to integer search years. Search experience is separate from a screening claim.
- Clicks search trigger: Supports modern renamed button `button.skills-search-btn` (*"Search"*), with fallback to legacy `button#show-results` (*"Show results"*).
- On the current UI, waits for the **Experience Level** slider after the initial search and moves its native handle to the configured value. Checks for a successful search response and the selected label. `0 years` and `25+` label the scale endpoints, while the handle shows the selected value. If the current layout cannot be verified, logs the gap and keeps downstream eligibility checks in force.
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
  - Evaluates the nested native job ID (`opp.job.id`) and legacy scope fields, with a DOM fallback to `ul#job-skills-<id>`.
  - Normalizes `job_id` as `instahyre-<opp_id>` (falls back to a stable hash whenever a usable ID cannot be resolved).
  - Checks both native ID and the legacy title/company hash against the exclusion set, preserving deduplication across the ID transition.

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
- Locates a unique target card through its exact title attribute or normalized `.company-name` heading (including current `h2` markup). For native IDs, also requires the card's matching `job-skills-<id>` element.
- **Phantom Page Guard**: If card is not found on the expected page, scans bounded `_visited_pages`, or up to six available pagination pages when that set is empty. Prevents chasing unrendered phantom pages (run 366 fix).
- **Stale Job Skip**: If the card has disappeared due to feed churn, returns `status = SKIPPED`, `reason = SkipReason.STALE_JOB`.

### 3. Pre-Apply State Check
- Inspects the exact card using `_APPLIED_CARD_MARKERS`, including `.application-status-box .status-label:text-is('Applied')`.
- If present, returns `ALREADY_APPLIED`.

### 4. Opening Opportunity Modal
- Clicks `a#employer-profile-opportunity[ng-click='openApplyModal(opp)']`.
- If the native click times out, checks for an opening modal first. Otherwise reacquires the exact native-ID card and dispatches one details-opening click. A recovered modal must expose the same job ID before eligibility is evaluated.
- Awaits modal: `.candidate-apply-modal > .application-modal-block` or `#employer-profile-modal`.
- Dumps modal HTML to `instahyre-modal-{job_id}.html` after waiting for the description, so diagnostics can include the hydrated listing.

### 5. Detail Screening & External ATS Handoff
- Reads full description from `#job-description .job-description`.
- Extracts JD experience bounds (`extract_jd_experience`).
- Runs `pre_submit_check(job)`. If rejected, closes modal and skips with filter decision.
- Explicit numeric GenAI/LLM tenure requirements are compared with configured domain years, rather than treating the overall 3.5-year ceiling as domain experience. Optional qualifications are excluded from this narrow check. Unknown domain tenure is not invented.
- **External Company Site Link**:
  - Checks for `a:text-is('Apply on company site')`.
  - If target is a valid external URL, closes modal and returns `status = EXTERNAL`, `reason = SkipReason.EXTERNAL_APPLY`, and exposes the URL for the orchestrator's conditional Sidekick handoff.

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
   - Close application/promotional overlays, then re-find the unique exact target card.
   - Accept only `_APPLIED_CARD_MARKERS`: exact Applied button, legacy badge, or current application-status label.
   - Search cards may disappear after applying and need not show a status. If no receipt is present, open a separate read-only tab, select Applied jobs, use the native company facet, and match the exact role and native ID where known. Require that exact card's Applied label while Applied jobs is selected. Preserve the main search tab and close the verification tab.
   - A missing/ambiguous company option, duplicate role cards, wrong native ID, missing badge or inaccessible feed remains unconfirmed. Global counts, employer popups and hidden templates are insufficient.
2. **Outcome Resolution**:
   - **Confirmed**: Returns `status = APPLIED`, `confirmation_type = "dom_marker"`, `confirmation_evidence = "Instahyre target card shows Applied"`.
   - **Unconfirmed**: Saves failure HTML/screenshot evidence and returns `status = FAILED`, `reason = SkipReason.SUBMISSION_UNCONFIRMED`. The uncertain-submission dedupe hold remains; do not blindly resubmit.

---

## 6. Invariants & Anti-Patterns (Do NOT Break)
1. **DO NOT treat modal closure as success**: A modal closing unexpectedly is usually a sign of an unhandled alert or backdrop error.
2. **DO NOT rely on carousel advancing**: Carousel advancing happens on "Not interested" or skip clicks as well.
3. **DO NOT chase phantom pages**: Prefer rendered pages and preserve the bounded available-page fallback when the visited set is empty.
4. **DO NOT use loose class selectors like `div[class*='applied']`**: These match peripheral UI containers. Use exact text matches `:text-is('Applied')`.
5. **DO NOT destroy `.application-modal` containers**: Force-remove only `.modal-backdrop` and promotional modals (`#go-premium-modal`, `#refer`). Destroying Angular templates breaks subsequent card openers.

## Verification evidence and limits

Run 699 authenticated, collected 130 opportunities, removed 12 duplicates and selected six after filtering. Run 701 opened The Agency Fund's Forward Deployed Engineer opportunity, checked the description and reached native Apply with submission disabled. A saved-page replay verified the exact card, native opener, description and card-scoped receipt selector.

Run 707 reported five unconfirmed submissions. Subsequent read-only company-filtered Applied-feed checks found exact Applied-today labels for Snabbit, Swiggy, Anaira AI, Neoqubit and Kaleidofin. The revised verifier confirmed all five native IDs live without resubmitting. Search-only probes selected all 15 terms, parsed 30 native-ID cards with locations and cleared all collapsed tags. Twenty saved-card checks and the full 516-test suite (61 subtests) passed. Three current JDs expose mandatory stack/domain-tenure conflicts. See the [run 707 audit](../../analysis/run707_instahyre_audit.md) for evidence and limits. Historical run outcomes were not rewritten and no new application was submitted during this audit.

The native path does not implement a general screening-questionnaire solver. If a different form or an external action without a destination appears, capture it and preserve the failure or review outcome. A linkless external button currently produces a review status without guaranteed question items. Mandatory skill filtering and domain-tenure parsing remain narrow rules, not complete semantic eligibility checks.

A failed or partial UI filter is not proof that returned jobs satisfy that filter. Keep card and detail checks authoritative.
