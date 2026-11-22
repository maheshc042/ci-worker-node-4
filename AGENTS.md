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

---

## 4. Profile Architecture & Specialization Invariants (NEVER VIOLATE)

The system relies on two strictly specialized profiles to maximize candidate match quality and interview conversion. Under no circumstances should an agent cross-contaminate or alter these profile boundaries without explicit user authorization.

### A. Profile 0: `Full Stack / Web Developer`
- **Resume Attached**: `resumes/CV_Mahesh_Chitakoti_2026_1_.pdf` ("Full Stack Engineer").
- **Core Tech Stack**: React.js, Next.js, TypeScript, Node.js, Express.js, Python, FastAPI, SQL, PostgreSQL, MongoDB, Redis, REST APIs, WebSockets, Docker, AWS.
- **Target Job Families**: Full-Stack, Web Developer, Backend Developer, Frontend Developer (React/TS), Database Developer, SQL Engineer, Analytical Engineer, QA Automation / SDET, General SDE.
- **SQL & Database Invariant**:
  - ✅ **DO NOT REMOVE** `sql developer`, `sql engineer`, `database developer`, `database engineer`, `database programmer`, `database tester`, `analytical engineer` from Profile 0 includes or keywords.
  - ❌ **NEVER PUT** `sql developer` or `database developer` into `title_must_exclude_any`. Mahesh possesses extensive SQL/database expertise.
- **AI Separation Rule**:
  - ❌ **NEVER DUMP PURE AI TITLES** (`generative ai`, `genai`, `llm`, `rag`, `ai agent`, `agentic ai`, `prompt engineer`, `ai developer`, `ai engineer`, `chatbot developer`) into Profile 0. Submitting the Full Stack resume to dedicated AI/RAG roles results in immediate recruiter rejection and wastes daily quotas.
  - Only true hybrid engineering roles (e.g. `full stack ai engineer`, `full-stack ai developer`, `full stack genai developer`) are permitted in Profile 0.

### B. Profile 1: `AI / Python Engineer`
- **Resume Attached**: `resumes/CV_Mahesh_Chitakoti_2026.pdf` ("AI & Backend Engineer").
- **Core Tech Stack**: LangGraph multi-agent workflows, Qdrant hybrid vector search, RAG pipelines, LLM inference caching, embeddings, Python, FastAPI, Node.js, PostgreSQL, Redis.
- **Target Job Families**: AI / GenAI, LLM Applications, RAG Pipelines, Multi-Agent Orchestration, Python Backend, Data Engineering, AI QA / Testing.
- **Frontend Separation Rule**:
  - ❌ **NEVER DUMP PURE FRONTEND TITLES** (`frontend developer`, `frontend engineer`, `front end engineer`, `senior frontend engineer`) into Profile 1. Submitting the AI & Backend resume to pure React/UI jobs makes zero sense and gets rejected by recruiters.
  - `frontend developer`, `frontend engineer`, `front end developer`, `front end engineer`, `pure frontend` MUST REMAIN in Profile 1's `title_must_exclude_any`.

### C. Experience Ceiling Invariant (3.5y Total, 3.0y Dedicated AI)
- **Candidate Commercial Experience**: ~3.2 years total software tenure (~2.0 years dedicated commercial AI experience).
- **Configured Hard Caps**:
  - `experience.max_years: 3.5`
  - `dedicated_ai_max_years: 3.0`
- **Mandatory Guardrail**:
  - ❌ **NEVER BUMP** `max_years` or `dedicated_ai_max_years` to 4.0y or 5.0y in `config.yaml` or `naukri_agent/config.py`!
  - Applying to 5-year requisitions guarantees ATS/recruiter rejection for a 3-year engineer, burning valuable daily submission quotas.
  - If a job is skipped because `requires 4.0y > ceiling 3.5y` or `requires 5.0y > ceiling 3.5y`, **THIS IS AN INTENDED REJECTION**. Do not attempt to bypass intentional filters by altering system design thresholds.
