# Cutshort

1. Confirm the candidate session. Collect from Recommended and the filtered All Jobs feed. Deduplicate by job ID and check the full job description for hard requirements before opening the application.
2. Open Apply on the exact card. The pitch dialog contains the message field, résumé, optional company follow, **Send** and **Cancel**. Check the job heading and selected résumé, then fill the pitch. Employer questionnaires belong to Messages.
3. Click **Send** once. Watch the same card for **Apply now** and **Not interested** to be replaced by **View conversation**. An applied listing can disappear or show a stale Apply button when reopened.
4. Open the new conversation read-only and check that **View full job description** links to the exact job. Use that evidence to confirm the application. If the link cannot be verified, record `submission_unconfirmed` and preserve the evidence.
5. Run a separate Messages sweep, starting with **Pending**. Skip questionnaires that already show a receipt and preserve answers already selected correctly. Verify configured answers before clicking **Submit**. If the same form asks “Responses cannot be changed after submission. Are you sure?”, click **Yes** once.
6. Wait for **Questionnaire submitted!** or **Your response has been recorded**. Queue unresolved questions for review. Free-form recruiter questions also go to review. Keep the chat composer and external application instructions out of questionnaire submission.

**Verified live** in runs 682–684. Run 668 also captured the card transition and a Messages receipt. Run 682 confirmed the AMPM Network application and captured the questionnaire confirmation prompt in `cutshort-thread-1-after.html`. Run 683 produced three new receipts. Run 684 checked 16 threads and finished with zero unanswered questions. See the [Cutshort adapter](../../naukri_agent/platforms/cutshort.py).
