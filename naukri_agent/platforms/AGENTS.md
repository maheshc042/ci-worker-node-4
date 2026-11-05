# Platform Adapter Implementation Rules

Any agent modifying adapter code in `naukri_agent/platforms/` or `naukri_agent/naukri/` must follow these rules:

1. **Read the Platform Playbooks**:
   Before modifying an adapter, read [`docs/platforms/README.md`](../../docs/platforms/README.md) and the adapter's playbook (`naukri.md`, `cutshort.md`, `instahyre.md`, `linkedin.md`, `wellfound.md`, `hiringcafe.md`).

2. **Platform-Specific Verification is Compulsory**:
   - Verify the receipt or persisted target state specified in the platform playbook. A DOM check does not establish server rendering. Naukri and Wellfound reopen listings, Cutshort verifies the exact job's new conversation, and Instahyre checks the target card.
   - An unconfirmed click or closed modal MUST return `status = FAILED`, `reason = SkipReason.SUBMISSION_UNCONFIRMED`. Never return `APPLIED` without empirical evidence.

3. **Selector Strictness**:
   - Use `:text-is('Applied')` rather than `:has-text('Applied')`.
   - Never use generic meta tags or loose window title matches.

4. **Preserve External Handoff Gates**:
   - External jobs must pass `pre_submit_check(job)` before being routed to `EXTERNAL`.
