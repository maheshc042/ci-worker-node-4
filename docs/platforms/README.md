# Multi-Platform Application Playbooks & System Specification

This directory contains the operational and architectural playbooks for every platform supported by the autonomous job application agent.

---

## 1. Platform Matrix

| Platform | Playbook | Mode | Primary Receipt / Verification Mechanism |
| :--- | :--- | :--- | :--- |
| **Naukri** | [Naukri Playbook](naukri.md) | Native 1-Click & Chatbot Drawer | Compulsory server reload (`page.goto(job.url)`); requires `"Applied"` button state |
| **Cutshort** | [Cutshort Playbook](cutshort.md) | In-Modal Pitch & Messages Sweep | Navigation to dedicated `/job/<id>` URL; requires `button:text-is('Applied')` or `a:text-is('View conversation')` |
| **Instahyre** | [Instahyre Playbook](instahyre.md) | In-Modal Opportunity Action | Feed card audit; requires `card.locator("button:text-is('Applied')")` or badge |
| **LinkedIn** | [LinkedIn Playbook](linkedin.md) | Multi-Step Easy Apply Dialog | In-dialog `:text-is('Application sent')` or target view `main button:text-is('Applied')` |
| **Wellfound** | [Wellfound Playbook](wellfound.md) | Native Modal Pitch Injection | Listing reload; requires persisted `button:text-is('Applied')` on the exact URL |
| **HiringCafe** | [HiringCafe Playbook](hiringcafe.md) | Direct ATS Link Extraction | Pre-submit check passed + resolved ATS URL routed to Sidekick outbox |

---

## 2. Universal Screening & Filter Architecture

Every job discovered across all platforms undergoes a standardized two-stage gate before an application can be attempted:

```
[ Discovered Listing ]
         │
         ▼
[ Stage 1: Card-Level Filter (FilterEngine.evaluate_card) ]
   ├── Title Inclusion: Matches title_must_include_any (or tech garnish tags if generic)
   ├── Title Exclusion: Rejects title_must_not_include_any and disjoint tech stacks
   ├── Experience Bounds: min_experience <= candidate.max_years AND max_experience >= candidate.min_years
   ├── Dedicated AI Cap: AI/ML roles require min_experience <= candidate.dedicated_ai_max_years (3.0y)
   ├── Seniority Imputation: Senior/Lead/Architect titles impute experience bounds even if missing
   ├── Compensation: Maximum offered salary >= rules.min_salary_lpa (5 LPA)
   ├── Freshness: posted_days_ago <= rules.max_posted_days (3 days)
   ├── Walk-ins: Rejects walk-in interview drives outside home city (Bengaluru)
   └── Blacklists: Rejects companies in blocked_companies and locations in blocked_locations
         │ (Passed)
         ▼
[ Stage 2: JD-Level Filter (FilterEngine.evaluate_detail & pre_submit_check) ]
   ├── Description Text: Scrapes full JD body
   ├── Negative Keywords: Rejects rotational night shifts, service bonds, mandatory non-tech degrees
   ├── Stack Compatibility: Rejects mandatory requirements for stacks foreign to candidate
   └── Apply Route: Detects native apply vs external company ATS
         │ (Passed)
         ▼
[ Stage 3: Application & Compulsory Post-Submit Verification ]
```

---

## 3. Answer Engine & Questionnaire Invariants ([`answers.py`](../../naukri_agent/core/answers.py))

When an application flow requires screening answers:
1. **Resolution Hierarchy**:
   - **Step 1**: Exact pattern match in `answer_kb` (priority-ordered).
   - **Step 2**: Candidate profile identity facts (notice period, current location, CTC).
   - **Step 3**: Structured compensation comparisons (`offers around ₹X - ₹Y / yr` evaluated against expected CTC).
   - **Step 4**: Experience & tenure calculations:
     - Named skill tenure (e.g., *"at least 1 year with Temporal"* matches `experience.skills['temporal']`).
     - Unit normalization: Questions asking for months auto-multiply years by 12 or convert months to years.
     - Strict boundary operators: `<` / `>` vs `<=` / `>=`.
   - **Step 5**: Fallback to `question_review` (human escalation) if no approved answer exists.
2. **Never Fabricate**: Low-confidence or unlisted skills must NEVER be guessed as 0, negative, or fabricated years. Escalate to human review to train the knowledge base.

---

## 4. Universal Outcome Invariants

| Status | Exact Meaning & Invariant |
| :--- | :--- |
| `APPLIED` | **Proven delivery**. Verified by a post-submit server reload or unambiguous exact-text receipt (`button:text-is('Applied')`). |
| `ALREADY_APPLIED` | Confirmed that the candidate previously applied to this job (verified via live DOM state). |
| `NEEDS_REVIEW` | An unanswered screening question, verification challenge, or form error blocked submission. Stored in `question_review`. |
| `EXTERNAL` | Company-site redirect URL resolved and forwarded to the Sidekick pickup outbox. |
| `SKIPPED` | Disqualified by filters (title mismatch, tenure shortfall, salary below minimum, blacklisted company). |
| `FAILED` (`submission_unconfirmed`) | Submit was clicked, but post-submit verification reload did NOT show applied state. **Fails closed**. |

---

## 5. Strict Guardrails for AI Models & Contributors

> [!CAUTION]
> **Do NOT Regress Hard-Earned Verification Fixes**:
> 1. **Zero Heuristic Approvals**: Never mark a job `APPLIED` based on modal closure, button click events, or client-side meta tags. Always verify the reloaded server state.
> 2. **Exact Selector Policy**: Avoid loose substring selectors like `:has-text('Applied')` or `div[class*='applied']`. Use exact `:text-is('Applied')`.
> 3. **Preserve Database Cooldowns**: When retrying failed jobs, maintain the 12-hour cooldown unless explicitly performing a manual database state reset.
> 4. **No Unsupervised Git Pushes**: Never execute `git commit` or `git push` without direct user approval.
