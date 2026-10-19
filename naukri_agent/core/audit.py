"""
Agent Audit System — JSON Lines daily log + quality report.

Every job the agent sees is recorded to a daily JSONL file:
  artifacts/audit-YYYY-MM-DD.jsonl

Each line is one JSON object:
  {"ts": "...", "run_id": 582, "platform": "naukri", "job_id": "...",
   "title": "...", "company": "...", "outcome": "applied",
   "reason": "...", "detail": "...", "score": 56.38}

Auto-cleanup: files older than 7 days are deleted on write.

CLI:
  python -m naukri_agent audit --days 3
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

AUDIT_DIR = Path("artifacts")
RETENTION_DAYS = 7


def _audit_path(day: datetime | None = None) -> Path:
    day = day or datetime.now(UTC)
    return AUDIT_DIR / f"audit-{day.strftime('%Y-%m-%d')}.jsonl"


def _cleanup_old_files() -> None:
    """Delete audit files older than RETENTION_DAYS."""
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    if not AUDIT_DIR.exists():
        return
    for f in AUDIT_DIR.glob("audit-*.jsonl"):
        try:
            # Parse date from filename: audit-YYYY-MM-DD.jsonl
            file_date = datetime.strptime(f.stem.replace("audit-", ""), "%Y-%m-%d").replace(tzinfo=UTC)
            if file_date < cutoff:
                f.unlink()
                log.debug("audit.file_cleaned", file=f.name)
        except (ValueError, OSError):
            continue


def record(
    run_id: int,
    platform: str,
    job_id: str,
    title: str = "",
    company: str = "",
    outcome: str = "pending",
    reason: str = "",
    detail: str = "",
    score: float | None = None,
    location: str = "",
    experience_text: str = "",
    salary_text: str = "",
) -> None:
    """Append one audit record to today's JSONL file."""
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        _cleanup_old_files()
        record_obj: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "platform": platform,
            "job_id": job_id,
            "title": title,
            "company": company,
            "outcome": outcome,
            "reason": reason,
            "detail": detail,
            "score": score,
            "location": location,
            "experience_text": experience_text,
            "salary_text": salary_text,
        }
        with _audit_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(record_obj, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("audit.record_failed", error=str(exc)[:120])


def read_last_days(days: int = 3) -> list[dict[str, Any]]:
    """Read audit records from the last N days."""
    records: list[dict[str, Any]] = []
    cutoff = datetime.now(UTC) - timedelta(days=days)
    if not AUDIT_DIR.exists():
        return records
    for f in sorted(AUDIT_DIR.glob("audit-*.jsonl")):
        try:
            file_date = datetime.strptime(f.stem.replace("audit-", ""), "%Y-%m-%d").replace(tzinfo=UTC)
            if file_date < cutoff:
                continue
            with f.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except (ValueError, OSError):
            continue
    return records


def generate_report(days: int = 3) -> str:
    """Generate a human-readable quality report for the last N days."""
    records = read_last_days(days)
    if not records:
        return f"No audit records found in the last {days} days."

    # Group by run
    by_run: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_run[r["run_id"]].append(r)

    lines = [f"=== AGENT QUALITY REPORT (last {days} days) ===\n"]

    for run_id in sorted(by_run, reverse=True):
        run_records = by_run[run_id]
        outcomes = Counter(r["outcome"] for r in run_records)
        platforms = Counter(r["platform"] for r in run_records)

        lines.append(f"Run #{run_id}: {len(run_records)} jobs seen")
        for platform, count in platforms.most_common():
            lines.append(f"  {platform}: {count}")
        lines.append(f"  Outcomes: {dict(outcomes)}")

        # Applied
        applied = [r for r in run_records if r["outcome"] == "applied"]
        if applied:
            lines.append(f"\n  ✅ Applied ({len(applied)}):")
            for r in applied:
                lines.append(f"    [{r['platform'][:4]}] {r['title'][:50]} @ {r['company'][:25]}")

        # Failed
        failed = [r for r in run_records if r["outcome"] == "failed"]
        if failed:
            lines.append(f"\n  ❌ Failed ({len(failed)}):")
            for r in failed:
                lines.append(f"    [{r['platform'][:4]}] {r['title'][:50]} @ {r['company'][:25]} — {r['detail'][:80]}")

        # Needs review
        needs_review = [r for r in run_records if r["outcome"] == "needs_review"]
        if needs_review:
            lines.append(f"\n  ⚠️ Needs Review ({len(needs_review)}):")
            for r in needs_review:
                lines.append(f"    [{r['platform'][:4]}] {r['title'][:50]} @ {r['company'][:25]} — {r['detail'][:80]}")

        # Rejected (top reasons)
        rejected = [r for r in run_records if r["outcome"] in ("filtered", "skipped")]
        if rejected:
            reason_counts = Counter(r.get("reason", "unknown") for r in rejected)
            lines.append(f"\n  🧹 Rejected ({len(rejected)}):")
            for reason, count in reason_counts.most_common(5):
                lines.append(f"    {count}x {reason}")

        lines.append("")

    # Overall quality score
    total = len(records)
    total_applied = sum(1 for r in records if r["outcome"] == "applied")
    total_failed = sum(1 for r in records if r["outcome"] == "failed")
    total_needs_review = sum(1 for r in records if r["outcome"] == "needs_review")
    total_rejected = sum(1 for r in records if r["outcome"] in ("filtered", "skipped"))

    lines.append("=== QUALITY SCORE ===")
    lines.append(f"  Total jobs seen: {total}")
    lines.append(f"  Applied: {total_applied} ({total_applied/total*100:.1f}%)")
    lines.append(f"  Failed: {total_failed} ({total_failed/total*100:.1f}%)")
    lines.append(f"  Needs review: {total_needs_review} ({total_needs_review/total*100:.1f}%)")
    lines.append(f"  Rejected: {total_rejected} ({total_rejected/total*100:.1f}%)")

    return "\n".join(lines)
