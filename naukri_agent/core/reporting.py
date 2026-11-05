"""
Report Generation and Dataset Exporter.
"""
from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .application_planner import ApplicationPlan
from .models import ApplicationStatus, ApplyOutcome, Job
from .ranking import RankedJob

log = get_logger(__name__)

RANKED_JOBS_CSV_HEADER = [
    "rank",
    "score",
    "job_id",
    "tab",
    "position",
    "company",
    "title",
    "url",
    "applicants",
    "reasons",
]

OUTCOME_JOBS_CSV_HEADER = [
    "job_id",
    "status",
    "company",
    "title",
    "url",
    "min_exp",
    "max_exp",
    "salary",
    "questions_answered",
    "detail",
    "reason",
    "attempts",
    "confirmation_type",
    "confirmation_evidence",
    "screenshot_path",
    "external_url",
]


def _now_utc() -> datetime:
    return datetime.now(UTC)


class ReportExporter:
    def __init__(self, output_dir: Path, run_id: str = "") -> None:
        self.output_dir = output_dir
        self.run_id = run_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _names(self, base: str, platform: str = "") -> Path:
        """Per-platform filenames so phases stop overwriting each other.

        Prefixed with the run id so consecutive runs stop clobbering each
        other's forensics (run 377's plan CSVs were lost to run 359's files).
        """
        slug = "".join(ch if ch.isalnum() else "_" for ch in (platform or "").lower()).strip("_")
        run_prefix = f"run{self.run_id}_" if self.run_id not in (None, "") else ""
        stem = f"{slug}_{base}" if slug else base
        return self.output_dir / f"{run_prefix}{stem}"

    def export_plan_reports(
        self,
        plan: ApplicationPlan,
        collected_jobs: list[Job],
        profile_name: str = "",
        platform: str = "",
    ) -> None:
        try:
            with self._names("collected_jobs.csv", platform).open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "job_id",
                    "tab",
                    "position",
                    "total_jobs_in_tab",
                    "company",
                    "title",
                    "url",
                    "applicants",
                    "scraped_at",
                ])
                for j in collected_jobs:
                    scraped = j.scraped_at.isoformat() if j.scraped_at else ""
                    w.writerow([
                        j.job_id,
                        j.recommendation_tab,
                        j.recommendation_position or "",
                        j.total_jobs_in_tab or "",
                        j.company,
                        j.title,
                        j.url,
                        j.applicant_count if j.applicant_count is not None else "",
                        scraped,
                    ])

            self._write_ranked_jobs_csv(self._names("ranked_jobs.csv", platform), plan.eligible_jobs)
            self._write_ranked_jobs_csv(self._names("selected_jobs.csv", platform), plan.selected_jobs)

            with self._names("rejected_jobs.csv", platform).open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["job_id", "reason", "detail", "company", "title", "url"])
                for rinfo in plan.rejected_jobs:
                    reason_str = rinfo.reason.value if rinfo.reason else "other"
                    w.writerow([
                        rinfo.job.job_id,
                        reason_str,
                        rinfo.detail,
                        rinfo.job.company,
                        rinfo.job.title,
                        rinfo.job.url,
                    ])

        except Exception as exc:
            log.warning("reporting.plan_export_failed", error=str(exc)[:200])

    def export_outcome_reports(
        self,
        applied: list[tuple[Job, ApplyOutcome]],
        failed: list[tuple[Job, ApplyOutcome]],
        profile_name: str = "",
        platform: str = "",
    ) -> None:
        try:
            # 1. Base outcome CSVs (preserves 100% backward compatibility)
            self._write_outcomes_csv(self._names("applied_jobs.csv", platform), applied)
            self._write_outcomes_csv(self._names("failed_jobs.csv", platform), failed)

            # 2. Categorized partitions for crystal-clear human auditing
            external_items = [(j, o) for j, o in failed if o.status == ApplicationStatus.EXTERNAL]
            skipped_items = [
                (j, o) for j, o in failed
                if o.status in (ApplicationStatus.SKIPPED, ApplicationStatus.ALREADY_APPLIED)
            ]
            true_failed_items = [
                (j, o) for j, o in failed
                if o.status not in (ApplicationStatus.EXTERNAL, ApplicationStatus.SKIPPED, ApplicationStatus.ALREADY_APPLIED)
            ]

            if external_items:
                self._write_outcomes_csv(self._names("external_jobs.csv", platform), external_items)
            if skipped_items:
                self._write_outcomes_csv(self._names("skipped_jobs.csv", platform), skipped_items)

            # 3. Complete unified ledger
            all_items = applied + failed
            self._write_outcomes_csv(self._names("all_outcomes.csv", platform), all_items)

            # 4. Human-Readable Single-Page Markdown Report
            self._write_human_markdown_report(
                self._names("run_report.md", platform),
                applied=applied,
                external=external_items,
                skipped=skipped_items,
                failed=true_failed_items,
                profile_name=profile_name,
                platform=platform,
            )
        except Exception as exc:
            log.warning("reporting.outcome_export_failed", error=str(exc)[:200])

    def export_summary_json(
        self,
        plan: ApplicationPlan,
        applied_count: int,
        failed_count: int,
        profile_name: str,
        dry_run: bool,
        duration_seconds: float = 0.0,
        platform: str = "",
    ) -> None:
        try:
            summary_data: dict[str, Any] = {
                "profile": profile_name,
                "run_id": self.run_id,
                "platform": platform,
                "dry_run": dry_run,
                "duration_seconds": round(duration_seconds, 2),
                "generated_at": _now_utc().isoformat(),
                "plan_stats": {
                    "collected": plan.stats.collected_count,
                    "rejected": len(plan.rejected_jobs),
                    "eligible": len(plan.eligible_jobs),
                    "selected": len(plan.selected_jobs),
                    "overflow": len(plan.overflow_jobs),
                    "rejection_reasons": plan.stats.rejection_reasons,
                    "top_score": plan.selected_jobs[0].score if plan.selected_jobs else 0.0,
                    "cutoff_score": plan.selected_jobs[-1].score if plan.selected_jobs else 0.0,
                },
                "applied_count": applied_count,
                "failed_count": failed_count,
            }
            summary_file = self._names("summary.json", platform)
            summary_file.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("reporting.summary_json_export_failed", error=str(exc)[:200])

    def _write_ranked_jobs_csv(self, path: Path, ranked_jobs: list[RankedJob]) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(RANKED_JOBS_CSV_HEADER)
            for rj in ranked_jobs:
                reasons_str = " | ".join(rj.reasons)
                writer.writerow([
                    rj.rank or "",
                    f"{rj.score:.2f}",
                    rj.job.job_id,
                    rj.job.recommendation_tab or "",
                    rj.job.recommendation_position or "",
                    rj.job.company,
                    rj.job.title,
                    rj.job.url,
                    rj.job.applicant_count if rj.job.applicant_count is not None else "",
                    reasons_str,
                ])

    def _write_outcomes_csv(self, path: Path, items: list[tuple[Job, ApplyOutcome]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(OUTCOME_JOBS_CSV_HEADER)
            for job, outcome in items:
                writer.writerow([
                    job.job_id,
                    outcome.status.value,
                    job.company,
                    job.title,
                    job.url,
                    job.min_experience if job.min_experience is not None else "",
                    job.max_experience if job.max_experience is not None else "",
                    job.min_salary_lpa if job.min_salary_lpa is not None else "",
                    outcome.questions_answered,
                    outcome.detail or "",
                    outcome.reason.value if outcome.reason else "",
                    outcome.attempts,
                    outcome.confirmation_type or "",
                    outcome.confirmation_evidence or "",
                    str(outcome.screenshot_path or ""),
                    outcome.external_url or "",
                ])

    def _write_human_markdown_report(
        self,
        path: Path,
        applied: list[tuple[Job, ApplyOutcome]],
        external: list[tuple[Job, ApplyOutcome]],
        skipped: list[tuple[Job, ApplyOutcome]],
        failed: list[tuple[Job, ApplyOutcome]],
        profile_name: str,
        platform: str,
    ) -> None:
        title_plat = (platform or "Run").title()
        lines: list[str] = [
            f"# 🎯 Application Run Report: {title_plat} ({profile_name})",
            "",
            f"- **Run ID:** `{self.run_id or 'local'}`",
            f"- **Platform:** {title_plat}",
            f"- **Profile:** {profile_name}",
            f"- **Generated At:** {_now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
            "## 📊 Executive Overview",
            "",
            "| Category | Count | Status | Notes |",
            "|:---|:---:|:---:|:---|",
            f"| **Direct Applied** | **{len(applied)}** | ✅ Success | Completed directly via 1-click or screening chatbot |",
            f"| **External Routes** | **{len(external)}** | 🌐 Company-site route | Check outbox records separately for queueing and delivery |",
            f"| **Skipped** | **{len(skipped)}** | ⏭️ Intentional | Repost collapsed, daily cap, stale redirect, or JD experience mismatch |",
            f"| **Failures / Review** | **{len(failed)}** | {'🚨 Review' if failed else '✨ Clean'} | True errors, timeouts, or unanswered mandatory questions |",
            f"| **Total Processed** | **{len(applied) + len(external) + len(skipped) + len(failed)}** | | |",
            "",
        ]

        if applied:
            lines.extend([
                "## ✅ Direct Applications Submitted",
                "",
                "| Job ID | Company | Title | Exp Required | Salary | Screening Qs | Link |",
                "|:---|:---|:---|:---:|:---:|:---:|:---|",
            ])
            for job, outcome in applied:
                exp_str = f"{job.min_experience or 0}-{job.max_experience or 0}y" if (job.min_experience is not None or job.max_experience is not None) else "-"
                sal_str = f"{job.min_salary_lpa} LPA" if job.min_salary_lpa else "Not Disclosed"
                qs_str = f"{outcome.questions_answered} answered" if outcome.questions_answered else "1-click"
                url_str = f"[View Posting]({job.url})" if job.url else "-"
                lines.append(f"| `{job.job_id}` | **{job.company}** | {job.title} | {exp_str} | {sal_str} | {qs_str} | {url_str} |")
            lines.append("")

        if external:
            lines.extend([
                "## 🌐 External Applications Routed to Sidekick",
                "",
                "| Job ID | Company | Title | External Career Portal Link |",
                "|:---|:---|:---|:---|",
            ])
            for job, outcome in external:
                target_url = outcome.external_url or job.url
                lines.append(f"| `{job.job_id}` | **{job.company}** | {job.title} | [Apply on Company Site]({target_url}) |")
            lines.append("")

        if skipped:
            lines.extend([
                "## ⏭️ Skipped Roles (Intentional Business Logic)",
                "",
                "| Job ID | Company | Title | Skip Reason | Detail |",
                "|:---|:---|:---|:---|:---|",
            ])
            for job, outcome in skipped:
                reason_str = outcome.reason.value if outcome.reason else outcome.status.value
                detail_str = (outcome.detail or "").replace("|", "-")
                lines.append(f"| `{job.job_id}` | **{job.company}** | {job.title} | `{reason_str}` | {detail_str} |")
            lines.append("")

        if failed:
            lines.extend([
                "## 🚨 Failures & Blockers (Action Required)",
                "",
                "| Job ID | Company | Title | Status | Error Detail | Screenshot / Artifact |",
                "|:---|:---|:---|:---:|:---|:---|",
            ])
            for job, outcome in failed:
                reason_str = outcome.reason.value if outcome.reason else outcome.status.value
                detail_str = (outcome.detail or "").replace("|", "-")
                shot_str = f"[Screenshot]({outcome.screenshot_path})" if outcome.screenshot_path else "None"
                lines.append(f"| `{job.job_id}` | **{job.company}** | {job.title} | `{reason_str}` | {detail_str} | {shot_str} |")
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
