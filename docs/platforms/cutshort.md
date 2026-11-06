# Cutshort workflow

This guide follows the [Cutshort adapter](../../naukri_agent/platforms/cutshort.py) and verified runs 668 and 682–684. The pitch application and Messages questionnaires have separate actions and receipts.

## Authentication and discovery

1. Restore the candidate session and confirm access to the candidate area. Handle dashboard overlays, availability prompts and feedback surveys before opening a job.
2. Read Recommended and filtered All Jobs through `fetch_jobs`. The adapter switches recommendations, configures UI filters and scrolls each feed.
3. Parse job IDs, title, employer, location, posted age, salary and experience where available. Deduplicate and rank through the orchestrator.
4. Before applying, the adapter opens the description on a temporary page, verifies its URL and runs the supplied detail check. This happens before submission. Hard requirements can reject a card that initially looked eligible.

## Pitch application in order

1. Find the exact listing card and inspect its current action state. Existing View conversation or applied evidence produces an already-applied outcome.
2. Open the job's application dialog. It contains the message field, résumé, optional company follow, Send and Cancel.
3. Verify the dialog heading identifies the intended role and employer. Select the track résumé and verify the configured file when one is resolved. If replacement is needed, use the native file chooser opened by the dialog's Upload another resume control. The underlying file input may be mounted outside the dialog.
4. Fill the tailored pitch and dispatch input/change events. The current application path does not run a questionnaire handler inside this dialog.
5. Find Send within that dialog and click once under the mutation policy. A click error is treated as uncertain and still goes through confirmation.
6. Observe the same page for the exact job's new View conversation link. The card's Apply and Not interested actions change after application.
7. Open the captured conversation on a temporary page. Verify its supported Cutshort path, unchanged thread ID and View full job description link back to the exact job.
8. Record APPLIED only when that verification passes. An attempted Send without a verified thread records `FAILED / submission_unconfirmed`. Preserve the before/after dumps.
9. Clean up the dialog as needed and return to All Jobs for the next opportunity.

An applied listing can disappear or show stale controls when reopened. Confirmation uses the exact job's new conversation and its linked job identity. Do not reintroduce a post-Send job-page reload as the required receipt. The pre-submit description visit and post-submit conversation visit have different purposes.

## Messages questionnaires in order

1. After the application pass, the orchestrator can run the separate sweep when mutations are enabled and enough time remains. Dry-run planning skips it.
2. Open `https://cutshort.io/profile/candidate-conversations?stage=awaiting` and focus Pending through `#tab-awaiting`.
3. Process questionnaire rows, then inspect the unfiltered candidate inbox. Share processed-thread keys across both passes.
4. Skip threads classified as older than three days. Relative timestamps that cannot be parsed do not establish that a thread is fresh.
5. Save the opened thread and inspect its form. An existing Questionnaire submitted! or Your response has been recorded receipt ends processing.
6. Resolve configured answers, including dates and supported uploads. Preserve an already selected answer when it matches the resolved option. Verify selection and required controls.
7. Unresolved answers block questionnaire submission and return review items.
8. Click Submit once. If the same form asks “Responses cannot be changed after submission. Are you sure?”, click its Yes control once.
9. Wait for Questionnaire submitted! or Your response has been recorded. A missing receipt leaves the questionnaire unconfirmed. Application and questionnaire confirmation remain separate.
10. Save the after-state and return unresolved question items to the orchestrator for bounded, deduplicated review persistence.

Free-form recruiter questions go to review. The general chat composer is separate from form submission. The application path does not answer questions before Send or immediately after Send.

## Current limitations

- The sweep selects fresh threads from Pending and the inbox. It does not verify every thread against the agent's application records and current job eligibility.
- Shared answer-engine fallbacks still need a caller-by-caller integrity review.
- The adapter treats an unresolved résumé mapping differently from a configured file that cannot be verified. A configured file mismatch blocks Send, but the absence of a mapping is not itself proof that the existing résumé is the right one.
- An unconfirmed questionnaire without explicit unanswered items can produce a diagnostic warning without creating a question-review row.

## Evidence

Run 668 saved the card transition and Messages receipt examples. Run 682 confirmed the AMPM Network application and exposed the questionnaire's Yes confirmation. Run 683 produced three new questionnaire receipts. Run 684 checked 16 threads and ended with zero unanswered items. These observations establish those paths, not permanent compatibility with every future form.
