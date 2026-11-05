# AGENTS.md — Guardrails for AI Coding Assistants & Contributors

This file governs the behavior of any automated agent (frontier models, local/open-source LLMs, or coding assistants) working on the `naukri-auto-apply-agent` codebase.

---

## 1. Mandatory Pre-Read
Before making any code changes or refactoring existing platform adapters:
1. Read [`docs/platforms/README.md`](docs/platforms/README.md) for universal filter and outcome rules.
2. Read the specific platform playbook in [`docs/platforms/`](docs/platforms/) (`naukri.md`, `cutshort.md`, `instahyre.md`, `linkedin.md`, `wellfound.md`, `hiringcafe.md`).

---

## 2. Core Invariants (DO NOT BREAK)

### A. The Zero False-Positive Mandate
- **Rule**: Record `APPLIED` only after the platform-specific receipt or persisted target-job state has been verified following submission. Browser-rendered DOM evidence alone does not establish whether a page was server-rendered.
- **Forbidden Heuristics**:
  - ❌ **DO NOT** assume modal closure or disappearing dialogs mean submission succeeded.
  - ❌ **DO NOT** assume clicking "Apply" or "Send" succeeded. Follow the platform-specific receipt rule below.
  - ❌ **DO NOT** use client-side SPA layout meta tags (e.g. `meta[name='atdlayout'][content='jobapplied']`) or page title substrings like `"confirmation"`.
  - ❌ **DO NOT** use loose substring selectors like `button:has-text('Applied')` or `div[class*='applied']`. Always use strict exact text matching: `button:text-is('Applied')`.
- **Required Verification**:
  - Naukri: Mandatorily invoke `_verify_post_submit(job)` (`page.goto(job.url)`). If the active Apply button is still present, return `status = FAILED`, `reason = SkipReason.SUBMISSION_UNCONFIRMED`.
  - Cutshort: Capture the new View conversation link for the exact job, open that thread read-only, and verify its View full job description link points back to the job. Reopening the applied listing is unreliable.
  - Instahyre: Require the exact target feed card's Applied button or exact applied badge. Employer-level popups and carousel movement are insufficient.
  - LinkedIn: Require an explicit sent receipt in a supported dialog or applied state on the exact target job view.
  - Wellfound: Reload the exact listing and verify its persisted applied evidence using `_APPLIED_JOB_MARKERS`.
  - HiringCafe: Record an external route after the detail gate. Verify outbox delivery and eventual ATS submission separately.

The playbooks distinguish these requirements from implementation gaps. A documented requirement or passing unit test is not evidence that every caller currently enforces it.

### B. Screening & Answer Integrity
- **Rule**: Never fabricate or guess screening answers.
- If a question is not answerable from verified candidate profile facts:
  - If it's a hard eligibility shortfall (e.g. requires 5+ years, candidate has 2.5) -> return `SKIPPED` (`unfit`).
  - If it's an unlisted skill or custom question -> capture screenshot and escalate to `question_review` (`NEEDS_REVIEW`).
- Do NOT enter `0`, negative values, or guessed numbers for unlisted skill tenure.

### C. Filter & Handoff Integrity
- External company ATS links must ALWAYS pass `pre_submit_check(job)` before being routed to `EXTERNAL` or placed in the Sidekick outbox.
- Do NOT bypass `pre_submit_check` for external jobs.

---

## 3. Git & Execution Boundaries
- **No Unsolicited Commits or Pushes**:
  - Do NOT execute `git add`, `git commit`, or `git push` unless explicitly asked by the user in the prompt.
  - Leave modified files in the working tree and provide the suggested commit message for review.
- **Verification Before Completion**:
  - Always run the relevant test suite:
    ```bash
    .venv\Scripts\python -m pytest tests/
    ```
  - Run all currently discovered unit and regression tests, report the actual counts, and disclose any failures or unavailable checks before presenting completion.
