"""Error monitoring.

* Every ERROR log (unhandled exceptions included) can be pushed to the operator's Telegram
  (OPERATOR_TELEGRAM_CHAT_ID + TELEGRAM_BOT_TOKEN), throttled to one message per minute
  with a count of what was skipped. Sent from a background thread, never blocks a request.
* Optional Sentry: set SENTRY_DSN and install `sentry-sdk`.
* Uptime: point any uptime monitor (UptimeRobot, Better Stack...) at GET /health.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


class OperatorAlertHandler(logging.Handler):
    def __init__(self, token: str, chat_id: str, min_interval: float = 60.0) -> None:
        super().__init__(level=logging.ERROR)
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.min_interval = min_interval
        self._last = 0.0
        self._skipped = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(("httpx", "httpcore", __name__)):
            return  # never alert about our own alert traffic
        with self._lock:
            now = time.monotonic()
            if now - self._last < self.min_interval:
                self._skipped += 1
                return
            skipped, self._skipped, self._last = self._skipped, 0, now
        text = f"🚨 COD Confirm – {record.levelname} {record.name}\n{record.getMessage()[:1500]}"
        if record.exc_info and record.exc_info[1] is not None:
            text += f"\n{type(record.exc_info[1]).__name__}: {str(record.exc_info[1])[:500]}"
        if skipped:
            text += f"\n(+{skipped} other error(s) in the last minute)"
        threading.Thread(target=self._send, args=(text,), daemon=True).start()

    def _send(self, text: str) -> None:
        try:
            httpx.post(self.url, json={"chat_id": self.chat_id, "text": text}, timeout=5)
        except httpx.HTTPError:
            pass


def setup_monitoring(settings: Settings) -> None:
    token = settings.telegram_bot_token.get_secret_value()
    if token and settings.operator_telegram_chat_id and settings.app_env != "test":
        logging.getLogger().addHandler(
            OperatorAlertHandler(token, settings.operator_telegram_chat_id)
        )

    dsn = settings.sentry_dsn.get_secret_value()
    if dsn:
        try:
            import sentry_sdk
        except ImportError:
            log.warning("monitoring: SENTRY_DSN set but sentry-sdk is not installed")
            return
        # send_default_pii=False: no IPs, cookies or bodies (customer data) sent to Sentry.
        sentry_sdk.init(dsn=dsn, environment=settings.app_env, release=settings.app_version,
                        send_default_pii=False, traces_sample_rate=0.0)  # fmt: skip
