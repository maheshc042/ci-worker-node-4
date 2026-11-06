# Cross-platform verification — 2026-11-06

## Run 721 from GitHub artifact 42180823120

The primary run reached all five enabled platforms and ended `partial`. Naukri reported nine new applications, one failure and four Sidekick items queued. Eight exact listings still showed Applied during read-only recheck. One redirected. The local detail rules now reject the observed financial-data, specialist BI and model-training requirements that passed in run 721. Datamatics remains an unresolved screening widget failure.

LinkedIn authenticated and recorded four Easy Apply successes across two profiles and one outreach email sent. The second profile overwrote the first profile's exported files. LinkedIn now includes the profile in report filenames. Cutshort recorded one exact conversation receipt and two pre-Send résumé failures. The saved page has file inputs outside the modal. The local fix uses the chooser opened by the verified dialog's upload button and retains filename verification.

Wellfound rejected all eight jobs at planning and exercised no application flow. Instahyre verified the 2-year slider setting and recorded eight exact Applied receipts and ten listing-opening timeouts before Apply. The local recovery is restricted to the exact opportunity and verifies the recovered modal's job ID. The CLI now returns a failing exit code for job failures or recorded run errors even when other jobs were applied. Screening review alone does not fail the CLI.

Five platforms are enabled; HiringCafe is disabled. This review combines archived GitHub runs 708–709, earlier live receipts, a new read-only Instahyre receipt check and a HiringCafe access probe. It did not submit an application, answer a screening question, send a pitch or change historical outcomes.

## Archived GitHub runs

`naukri-agent-diagnostics-42063705044.zip` contains two live runs from November 5, 2026. The primary run (708) and secondary Naukri run (709) both ended `partial`, after about 43 and 20.5 minutes respectively. Both had substantial time left in the 90-minute account budget. The LinkedIn Easy Apply loop was skipped in run 708 because the headless session could not be verified. The separate LinkedIn outreach campaign logged an expired cookie and sent zero emails. The later run 715 receipts and Gmail acceptance are separate evidence and do not verify a fresh GitHub runner's session.

Run 708's Instahyre search reached an unfiltered fallback after applying only one of the 15 requested skill tags, then clearing them. The later search-only probe applied all 15. The combined archive shows 23 external routes and 22 Sidekick items queued for pickup. Final ATS submission is a separate outcome. The workflow also sets `DB_SSL_INSECURE=true`, and both archived runs logged `db.tls_verification_disabled`. Database certificate verification remains a deployment gap.

The CLI can return exit code zero when an account run applies to at least one job despite platform errors. Check `run.finished` and per-platform outcomes when assessing the workflow, including platforms skipped at login.

| Platform | Evidence | Remaining live check |
| --- | --- | --- |
| Naukri | Primary run 708 recorded 7 applied, 5 screening reviews and 1 failure; secondary run 709 recorded 3 applied, 1 screening review and 2 failures. Earlier run 704 verified all 13 exact receipts from runs 702–703. | Reproduce the Datamatics screening detection failure and the Dbiz.ai and Intellect Design Arena Apply controls that did not open a dialog. The archived Datamatics screenshot shows the active Apply button, but does not identify the earlier screening widget. |
| Cutshort | Run 708 recorded one MERN application after the new View conversation thread linked to the exact job. A separate Messages event recorded three answers and success for the same named thread. The other planned job was rejected at detail for requiring 4 years against the 3.5-year ceiling. | Continue checking ownership of eligible Messages threads and handling of unknown questions. Run 708's sweep log carried a stale Instahyre job ID and profile; its Cutshort thread name is the useful identifier. |
| LinkedIn | Run 715 verified two exact Easy Apply receipts. The separate Python Developer pitch was accepted by Gmail SMTP. | Unknown screening answers still hold some roles for review. SMTP acceptance does not prove arrival in a recipient inbox. |
| Instahyre | Earlier run 707's five Applied cards were verified. Run 708 had five unconfirmed submissions. A read-only check of its exact native IDs on the Applied feed found no matching receipt for any of the five. | Determine whether a new eligible, never-attempted job reaches an exact Applied card using the updated flow. Do not retry run 708 jobs based on missing evidence alone. |
| Wellfound | All four run 706 jobs later showed settled Applied receipts. Run 708 stopped at planning: five title rejections and two roles with explicit 4- or 5-year minima. | Exercise the updated eligibility, relocation and pitch paths with a fresh eligible listing; require settled Applied after the Undo window. |
| HiringCafe | Public listing probe returned HTTP 403. Local tests cover the detail gate and external-route validation. | Feed access, downstream delivery and ATS submission remain unverified. Keep disabled. |

Instahyre run 708 exact native IDs: Google `446643`, Airtel X Labs `439060`, The Agency Fund `449262`, Zavo `449066`, Rocketlane `438036`. Absence from the observed Applied feed does not establish whether a submission was accepted or rejected.

## Selection and review coverage

The archived planner rejected 51 Cutshort jobs by title; 85 Instahyre jobs by title and 40 by experience; and 247 Naukri jobs by title, 20 by freshness and one as a walk-in. These are observed decisions, not measured accuracy. The archived rejection rows lack full descriptions, so they cannot establish how many qualified jobs the planner missed. A positive title match is permission to inspect the job, not proof of detailed eligibility or evidence for answering a screening question. Unlisted skill tenure stays in review without a guessed number.

Run 708 rejected **Automation QA with Python or Java** solely for the `java` title term. That is a possible missed match: the title explicitly offers Python, but the description is needed to determine whether Java is mandatory elsewhere. Conversely, **MS Fabric Data and SQL Engineer** was recorded as applied; confirm that Microsoft Fabric specialist work is in scope before treating this as a successful fit. Cutshort rejected **Full Stack Developer (PHP / Python / React.js)** on `php`; the title alone does not establish whether all three skills were mandatory. These are useful cases for testing how the planner distinguishes alternatives from requirements without admitting unrelated roles.

For the next live checks, use one platform at a time to give it the full run budget and isolate any failure. Capture the target job, the decision and the platform-specific receipt. Combined runs share an account time budget; use them once the individual flows are verified.
