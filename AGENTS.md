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
- **Rule**: An application must NEVER be declared `APPLIED` unless verified by the server-rendered DOM state after submission.
- **Forbidden Heuristics**:
  - ❌ **DO NOT** assume modal closure or disappearing dialogs mean submission succeeded.
  - ❌ **DO NOT** assume clicking "Apply" or "Send" succeeded without post-click reload verification.
  - ❌ **DO NOT** use client-side SPA layout meta tags (e.g. `meta[name='atdlayout'][content='jobapplied']`) or page title substrings like `"confirmation"`.
  - ❌ **DO NOT** use loose substring selectors like `button:has-text('Applied')` or `div[class*='applied']`. Always use strict exact text matching: `button:text-is('Applied')`.
- **Required Verification**:
  - Naukri: Mandatorily invoke `_verify_post_submit(job)` (`page.goto(job.url)`). If the active Apply button is still present, return `status = FAILED`, `reason = SkipReason.SUBMISSION_UNCONFIRMED`.
  - Cutshort: Mandatorily navigate to `job.url` (`/job/<id>`) and match `button:text-is('Applied')` or `a:text-is('View conversation')`.
  - Instahyre: Verify `card.locator("button:text-is('Applied')")` on the feed card.
  - LinkedIn: Require explicit `[role='dialog'] :text-is('Application sent')` or target job view status.
  - Wellfound: Reload listing URL and verify persisted `button:text-is('Applied')`.

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
  - Ensure all 469+ unit and regression tests pass with 0 errors before presenting your work.
