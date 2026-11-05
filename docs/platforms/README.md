# Platform workflow reference

Read this guide and the relevant playbook before changing discovery, answers, application actions, confirmation or external handoffs. These guides describe the repository at the time of review. Platform websites can change independently.

## Platform routes and receipts

| Platform | Job source and application route | Required application evidence |
| --- | --- | --- |
| [Naukri](naukri.md) | Recommended jobs, native Apply and screening drawer | Reopen through `_verify_post_submit(job)`, check the Apply state and explicit receipt |
| [Cutshort](cutshort.md) | Recommended and filtered feeds, pitch dialog, separate Messages sweep | New View conversation for the exact job, then verify its thread links back to that job |
| [LinkedIn](linkedin.md) | Track-specific search, Easy Apply or company-site link | Explicit sent receipt in the dialog or applied state on the exact job view |
| [Wellfound](wellfound.md) | Candidate jobs feed, pitch form or company-site link | Reopen the exact listing and check persisted applied state |
| [Instahyre](instahyre.md) | Filtered Opportunities feed and opportunity modal | Applied button or exact applied badge on the target feed card |
| [HiringCafe](hiringcafe.md) | Public listing discovery and company-site handoff | A validated destination and passing detail check establish an external route |

## Shared decision sequence

1. Confirm the account session or public-feed access. Respect platform pauses and challenge handling. A blocked session is an access failure, not an empty job market.
2. Collect job metadata, deduplicate, evaluate card eligibility and rank candidates. After opening the actual description, run the detail check before submitting or returning an external route.
3. Keep missing metadata distinguishable from verified values. Experience ceilings, allowed locations, salary floors, freshness, per-platform limits and daily budgets come from [configuration](../../config/config.yaml) and [config models](../../naukri_agent/config.py). Adapter-specific gates can be stricter. A search filter or title match alone does not prove that the full description passes.
4. Select the correct account and role résumé where supported. Resolve screening from approved facts. A hard verified mismatch should skip the job. An unlisted skill-tenure or custom question should go to review, without guessed years, zero or a negative default.
5. Require the platform-specific receipt before recording a new application. Keep prior application evidence separate from a newly confirmed application.
6. Preserve unconfirmed submissions for reconciliation. [Repository deduplication](../../naukri_agent/db/repository.py) holds them for the dedupe window because a submission may already have landed. Ordinary failures use the configured cooldown. Resolved review items may be retried subject to the attempt budget.
7. Return an external route only after the detail check. The orchestrator queues it only when side effects and Sidekick integration are enabled and the destination passes dispatch validation. Queueing, delivery and final ATS submission are separate events.

## Outcome meanings

| Outcome | Meaning |
| --- | --- |
| `APPLIED` | A new application has the receipt required by its platform |
| `ALREADY_APPLIED` | The platform reports an existing application |
| `NEEDS_REVIEW` | An answer or form blocker needs attention. Only supplied question items populate `question_review` |
| `EXTERNAL` | The adapter identified a company-site route. Check the outbox separately for delivery |
| `SKIPPED` | A filter, stale listing or dry-run boundary prevented submission. Read the reason |
| `FAILED` with `submission_unconfirmed` | Submission may have been attempted but its receipt could not be verified |
| Other `FAILED` | Navigation, access, missing controls or another operational failure. Read the detail |

## Answer and review boundaries

The [answer engine](../../naukri_agent/core/answers.py) includes special intent handlers before and after configured KB matching. Its actual ordering is more detailed than a simple KB-first hierarchy.

**Required policy** is to use verified facts and send unknown tenure to review. **Current gap** is that the shared engine still contains unlisted/default tenure and generic willingness fallbacks. LinkedIn rejects the identified inferred-answer patterns through its adapter guard. Equivalent enforcement has not been demonstrated for every other caller. A configured zero for a known skill is different from inventing zero for an unlisted skill.

The Cutshort Messages sweep examines fresh Pending and inbox threads. Its current code does not restrict every thread to an eligible job applied by this agent. Read that platform's limitations before enabling the sweep.

## Verification and maintenance

- `naukri-agent run --platform <name> --account primary --dry-run` verifies discovery, filtering and planning. The orchestrator stops before the normal application loop.
- Direct adapter probes can inspect reversible form steps under dry-run policy where supported. They do not verify delivery.
- Local tests and saved browser replays verify logic and selectors. A fresh application receipt needs an authorized live submission.
- Save run IDs, job IDs, decisions and before/after receipts. Update the affected guide with the evidence type and remaining gaps.
- Inspect actual limits in configuration. The model default dedupe window is 90 days and ordinary failure cooldown is 12 hours. Neither describes how long an uncertain submission is held in every situation.

## Known verification limits

| Platform | Evidence from this chat | Remaining limit |
| --- | --- | --- |
| Cutshort | Runs 682–684 confirmed an application and questionnaire receipts | Messages eligibility ownership and shared answer fallback policy need further enforcement |
| LinkedIn | Run 688 completed discovery. Run 695 held a custom question. Run 698 reached final Submit under dry run | A fresh live submission receipt is unverified |
| Instahyre | Run 699 completed discovery. Run 701 reached native Apply under dry run. Saved-page replay scoped the receipt to the job | A fresh live submission receipt is unverified |
| HiringCafe | Run 700 stopped at clearance. Local handoff checks passed | Live discovery and downstream delivery remain unverified |
| Naukri | Implementation and existing regression tests reviewed here | The verifier still contains title/URL and broad-marker branches that require a separate correctness review |
| Wellfound | Implementation and existing regression tests reviewed here | Hard-coded modal tenure and fallback pitch claims need a separate correctness review |

## Contributor boundaries

Keep work local unless the user authorizes Git staging, commits or pushes. Run the full regression suite required by [AGENTS.md](../../AGENTS.md). Preserve user changes. Do not transfer one platform's reload or receipt rule to another platform.
