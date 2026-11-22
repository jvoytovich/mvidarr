"""
Celery tasks for the *arr side of MVidarr: indexer grabs, quality upgrades,
the Plex Music Videos library and chart lists. All run on the scheduler queue.
"""

from typing import Any, Dict, List, Optional

from src.jobs.celery_app import celery_app
from src.services.settings_service import SettingsService
from src.utils.logger import get_logger

logger = get_logger("mvidarr.tasks.arr")


def _fresh_settings() -> None:
    # Settings are cached per process; pick up changes made in the web UI
    SettingsService.reload_cache()


@celery_app.task(
    name="src.tasks.arr_tasks.indexer_poll_task", soft_time_limit=1500, time_limit=1700
)
def indexer_poll_task() -> Dict[str, Any]:
    """Check SABnzbd/qBittorrent jobs; import finished downloads."""
    _fresh_settings()
    from src.services.indexers.indexer_service import IndexerConfig, poll_grabs

    config = IndexerConfig()
    if not config.enabled:
        return {"skipped": "indexers disabled"}
    return poll_grabs(config)


@celery_app.task(
    name="src.tasks.arr_tasks.indexer_upgrade_task",
    soft_time_limit=3300,
    time_limit=3500,
)
def indexer_upgrade_task() -> Dict[str, Any]:
    """Look for better copies of videos that came from YouTube."""
    _fresh_settings()
    if not SettingsService.get_bool("indexer_upgrades_enabled", True):
        return {"skipped": "upgrades disabled"}
    from src.services.indexers.indexer_service import search_upgrades

    return search_upgrades()


@celery_app.task(
    name="src.tasks.arr_tasks.plex_music_video_sync_task",
    soft_time_limit=3300,
    time_limit=3500,
)
def plex_music_video_sync_task(scan: bool = False) -> Dict[str, Any]:
    """Titles, years, genres, collections and playlists in the Plex Music Videos library."""
    _fresh_settings()
    from src.services.plex_library_sync_service import sync_plex_library
    from src.services.plex_music_video_service import PlexMusicVideoClient

    if not PlexMusicVideoClient.enabled():
        return {"skipped": "Plex music video library not enabled"}
    return sync_plex_library(scan=scan)


@celery_app.task(
    name="src.tasks.arr_tasks.media_lists_refresh_task",
    soft_time_limit=3300,
    time_limit=3500,
)
def media_lists_refresh_task(years: Optional[List[int]] = None) -> Dict[str, Any]:
    """Fetch Billboard Year-End lists (new years, last two re-checked) and link videos."""
    _fresh_settings()
    if not SettingsService.get_bool("billboard_lists_enabled", False):
        return {"skipped": "Billboard lists disabled"}
    from src.services.media_list_service import refresh_billboard

    return refresh_billboard(years=years)
