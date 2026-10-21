"""
Notifications.

Design decisions:

- **Interface + composite.** `Notifier` is an ABC; `CompositeNotifier` fans out
  to every configured channel and swallows individual channel errors. A dead
  Telegram token must never fail a successful run.
- **Console channel always on.** It guarantees the run summary lands in the
  Docker logs even with no external channel configured.
- **Telegram over raw HTTP.** The Bot API is two endpoints; pulling in a bot
  framework for `sendMessage` would add a heavy dependency and a background
  polling loop we do not want inside a batch job.
- Messages are HTML-escaped and hard-truncated to Telegram's 4096-char limit,
  because a run that applies to 40 jobs will otherwise silently fail to send.
"""

from __future__ import annotations

import html
from abc import ABC, abstractmethod

import httpx

from ..core.models import RunStats
from ..logging_setup import get_logger

log = get_logger(__name__)

TELEGRAM_LIMIT = 4_000


_LEVEL_PREFIXES = {
    "failed": "🚨 FAILED",
    "partial": "⚠️ PARTIAL",
    "success": "🚀 SUCCESS",
}


def header_prefix(level: str | None, *, is_error: bool = False) -> str:
    """Run-status-aware notification header.

    ``is_error`` still decides *whether* to notify (config gates), but the
    prefix must describe the run: a partial run with a few job-level
    failures is not a failed run (Q#589 was paged as "FAILED ... partial").
    Unknown/missing levels fall back to the binary behaviour.
    """
    if level in _LEVEL_PREFIXES:
        return _LEVEL_PREFIXES[level]
    return _LEVEL_PREFIXES["failed" if is_error else "success"]


class Notifier(ABC):
    @abstractmethod
    async def send(
        self,
        title: str,
        body: str,
        *,
        is_error: bool = False,
        level: str | None = None,
    ) -> None: ...

    async def close(self) -> None:  # pragma: no cover - optional hook
        return None


class ConsoleNotifier(Notifier):
    async def send(
        self,
        title: str,
        body: str,
        *,
        is_error: bool = False,
        level: str | None = None,
    ) -> None:
        level = level or ("failed" if is_error else "success")
        log_fn = log.error if level == "failed" else (log.warning if level == "partial" else log.info)
        log_fn("notify.console", title=title, body=body)


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str, timeout_s: float = 15.0) -> None:
        # Token is never embedded in a logged structure: the URL is built
        # per request and redacted from any error text before logging.
        self._bot_token = bot_token
        self.chat_id = chat_id
        self.timeout_s = timeout_s

    @property
    def _url(self) -> str:
        return f"https://api.telegram.org/bot{self._bot_token}/sendMessage"

    def _redacted(self, text: str) -> str:
        token = self._bot_token or ""
        return text.replace(token, "[redacted]") if token else text

    async def send(
        self,
        title: str,
        body: str,
        *,
        is_error: bool = False,
        level: str | None = None,
    ) -> None:
        prefix = header_prefix(level, is_error=is_error)
        title_esc = html.escape(title)
        header = f"<b>{prefix}: {title_esc}</b>\n\n<pre>"
        footer = "</pre>"
        max_esc_len = TELEGRAM_LIMIT - len(header) - len(footer) - 10

        escaped_body = html.escape(body)
        if len(escaped_body) > max_esc_len:
            cut = max_esc_len
            last_amp = escaped_body.rfind("&", max(0, cut - 8), cut)
            if last_amp != -1 and ";" not in escaped_body[last_amp:cut]:
                cut = last_amp
            escaped_body = escaped_body[:cut] + "\n…"

        text = f"{header}{escaped_body}{footer}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(
                    self._url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
            if response.status_code >= 400:
                log.warning(
                    "notify.telegram_rejected",
                    status=response.status_code,
                    body=response.text[:200],
                )
            else:
                log.info("notify.telegram_sent", chars=len(text))
        except Exception as exc:
            log.warning("notify.telegram_failed", error=self._redacted(str(exc))[:200])


class CompositeNotifier(Notifier):
    def __init__(self, channels: list[Notifier]) -> None:
        self.channels = channels

    async def send(
        self,
        title: str,
        body: str,
        *,
        is_error: bool = False,
        level: str | None = None,
    ) -> None:
        for channel in self.channels:
            try:
                await channel.send(title, body, is_error=is_error, level=level)
            except Exception as exc:  # a broken channel must not break the run
                log.warning(
                    "notify.channel_failed",
                    channel=type(channel).__name__,
                    error=str(exc)[:200],
                )


def build_notifier(
    *, telegram_enabled: bool, bot_token: str, chat_id: str
) -> CompositeNotifier:
    channels: list[Notifier] = [ConsoleNotifier()]
    if telegram_enabled and bot_token and chat_id:
        channels.append(TelegramNotifier(bot_token, chat_id))
    else:
        log.info("notify.telegram_disabled")
    return CompositeNotifier(channels)


def _walkin_radar_title() -> str:
    """Walk-in radar label uses the configured home city, never a literal."""
    try:
        from ..config import AgentConfig

        location = (AgentConfig.load().candidate.location or "").strip()
    except Exception:
        location = ""
    city = location.split(",")[0].strip()
    return f"📍 {city.upper()} WALK-IN RADAR" if city else "📍 WALK-IN RADAR"


def format_run_summary(
    stats: RunStats,
    *,
    duration_s: float,
    run_id: int | None,
    error: str | None = None,
    dry_run: bool = False,
) -> str:
    """Compact run summary: counts only, no job lists.

    Owner directive: applied/external/failed job lists are noise — what
    matters is the metrics, the per-platform breakdown, walk-in
    opportunities, and every distinct error. Identical error lines collapse
    to one row with a ×N count so repeated failures stay visible instead
    of eating the message cap.
    """
    mins = int(duration_s // 60)
    secs = int(duration_s % 60)
    mode_tag = " [DRY-RUN]" if dry_run else ""
    lines = [
        f"📊 RUN METRICS{mode_tag} (ID: #{run_id if run_id is not None else 'N/A'} | ⏱️ {mins}m {secs}s):",
        f"  • Total Scraped     : {stats.scraped}",
        f"  • Jobs Considered   : {stats.considered}",
        f"  • Filtered Out      : {stats.filtered_out}",
        f"  • 🧹 Plan Rejected (pre-apply): {stats.plan_rejected}",
        f"  • ✅ APPLIED        : {stats.applied}",
        f"  • 🔄 Already Applied: {stats.already_applied}",
        f"  • 🔗 External Links : {stats.external}",
        f"  • 📤 Forwarded to Sidekick: {getattr(stats, 'forwarded', 0)}",
        f"  • ⚠️ Needs Review   : {stats.needs_review}",
        f"  • ❌ Failed         : {stats.failed}",
    ]
    if dry_run:
        lines.insert(1, "  ℹ️ SIMULATION ONLY — No live applications submitted.")

    if stats.per_platform:
        lines.append("")
        lines.append("🌐 PER-PLATFORM BREAKDOWN:")
        for platform, counters in stats.per_platform.items():
            applied = counters.get("applied", 0)
            failed = counters.get("failed", 0)
            external = counters.get("external", 0)
            scraped = counters.get("scraped", 0)
            rejected = counters.get("plan_rejected", 0)
            lines.append(
                f"  • {platform.capitalize():<10}: {applied} applied | {failed} failed | "
                f"{external} external | {scraped} scraped | {rejected} plan-rejected"
            )

    if stats.walkin_alerts:
        lines.append("")
        lines.append(f"{_walkin_radar_title()} ({len(stats.walkin_alerts)}):")
        for job in stats.walkin_alerts[:10]:
            title = job.get("title", "?")
            company = job.get("company", "?")
            location = job.get("location", "")
            url = job.get("url", "")
            lines.append(f"  • {title} @ {company} ({location})")
            if url:
                lines.append(f"    🌐 URL: {url}")

    if stats.errors:
        lines.append("")
        lines.append("⚠️ ERRORS / WARNINGS:")
        seen: dict[str, int] = {}
        for message in stats.errors:
            key = message[:160]
            seen[key] = seen.get(key, 0) + 1
        for message, count in seen.items():
            suffix = f" (×{count})" if count > 1 else ""
            lines.append(f"  ! {message}{suffix}")

    if error:
        lines.append("")
        lines.append(f"🚨 FATAL: {error[:300]}")

    return "\n".join(lines)

