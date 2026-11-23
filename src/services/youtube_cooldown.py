"""
Shared YouTube cooldown after a bot check or rate limit.

When YouTube answers "Sign in to confirm you're not a bot" (or HTTP 429),
every further request makes it worse, so all YouTube downloads pause for
`youtube_cooldown_hours` (default 6) and the affected videos go back to
WANTED. The deadline is stored in the settings table (read fresh, not from
the per-process settings cache) so the API, Celery workers and download
threads all see it.

Age-restricted videos ("Sign in to confirm your age") are deliberately not a
bot check: they are a per-video problem and must not pause everything.
"""

import re
import time
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("mvidarr.youtube_cooldown")

COOLDOWN_KEY = "youtube_cooldown_until"
_BOT_CHECK = re.compile(
    r"not a bot|HTTP Error 429|Too Many Requests|rate.?limit", re.IGNORECASE
)
_AGE_GATE = re.compile(
    r"confirm your age|age.restricted|inappropriate for some users", re.I
)


def is_bot_check_error(message: str) -> bool:
    if not message or _AGE_GATE.search(message):
        return False
    return bool(_BOT_CHECK.search(message))


def _read() -> Optional[float]:
    try:
        from src.database.connection import get_db
        from src.database.models import Setting

        with get_db() as session:
            row = session.query(Setting).filter(Setting.key == COOLDOWN_KEY).first()
            return float(row.value) if row and row.value else None
    except Exception:  # noqa: BLE001 - no DB: no cooldown
        return None


def cooldown_remaining() -> float:
    """Seconds left in the cooldown, 0 when YouTube downloads may run."""
    until = _read()
    if not until:
        return 0.0
    return max(0.0, until - time.time())


def start_cooldown(reason: str, hours: Optional[float] = None) -> None:
    try:
        from src.services.settings_service import SettingsService

        if hours is None:
            hours = SettingsService.get_float("youtube_cooldown_hours", 6.0)
        if hours <= 0:
            return
        until = time.time() + hours * 3600
        SettingsService.set(
            COOLDOWN_KEY,
            str(until),
            "YouTube downloads paused until this unix time (bot check)",
        )
        logger.warning(f"YouTube cooldown for {hours:g} h: {reason[:200]}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Could not start YouTube cooldown: {exc}")


def clear_cooldown() -> None:
    from src.services.settings_service import SettingsService

    SettingsService.set(
        COOLDOWN_KEY, "", "YouTube downloads paused until this unix time"
    )
