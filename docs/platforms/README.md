# Platform playbooks

| Platform | Guide | Application route |
| --- | --- | --- |
| Naukri | [Naukri](naukri.md) | Native Apply and in-flow screening |
| LinkedIn | [LinkedIn](linkedin.md) | Easy Apply or company-site handoff |
| Cutshort | [Cutshort](cutshort.md) | Pitch dialog, then questionnaires in Messages |
| Wellfound | [Wellfound](wellfound.md) | Native pitch or company-site handoff |
| Instahyre | [Instahyre](instahyre.md) | Native opportunity modal or company-site handoff |
| HiringCafe | [HiringCafe](hiringcafe.md) | Company-site handoff |

## Rules shared across platforms

1. Confirm authentication where the platform requires it. Respect platform pauses and stop at challenges that require human action.
2. Collect and deduplicate jobs, evaluate the card, rank eligible jobs, then check the full description or other available detail before submitting. Keep the configured title, experience, location, compensation and account limits in force. Log the reason for each rejection.
3. Use the candidate profile and approved answers. A matching title establishes job relevance. A specific claim about years of experience needs a separate answer. Send unlisted skill-tenure questions to review instead of entering zero, a negative answer or guessed years. **Implementation gap:** the shared [answer engine](../../naukri_agent/core/answers.py) still has a one-year fallback for some unlisted tenure prompts. Verify the caller and prompt type when testing this rule.
4. Keep `applied`, `already_applied`, `needs_review`, external handoff and unconfirmed submission distinct. Use the evidence in the platform guide. Hold an uncertain submission for reconciliation before attempting it again.
5. Run `naukri-agent run --platform <platform> --account primary --dry-run` to inspect discovery and planning. The orchestrator stops before the application engine. Use browser fixtures to test form handling and receipts, and a bounded live run on an eligible job to verify a fresh application. Save the run ID, job ID, decision reason and confirmation evidence.

Update the affected playbook with any workflow change. Label whether a behavior comes from code, saved browser evidence or a fresh live run. When a platform introduces a new screen, preserve evidence of it and add a regression check before changing submission or confirmation rules.
