"""
API for MVidarr's *arr features: Usenet/torrent indexers (Prowlarr,
SABnzbd, qBittorrent), the Plex Music Videos library, chart lists and the
in-place library import. Mounted at /api/arr.
"""

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from src.api.fastapi.auth_dependencies import require_admin, require_authentication
from src.database.connection import get_db
from src.database.models import Artist, IndexerGrab, Video
from src.services.settings_service import SettingsService
from src.utils.logger import get_logger

router = APIRouter(prefix="/api/arr", tags=["arr"])
logger = get_logger("mvidarr.api.arr")


class ImportRequest(BaseModel):
    path: str
    dry_run: bool = True
    monitor_new_artists: bool = False


class BillboardRefreshRequest(BaseModel):
    years: Optional[List[int]] = None


class BillboardStateRequest(BaseModel):
    state_db: str
    lists_json: Optional[str] = None
    path_from: str = "/mnt/media"
    path_to: str = "/media"


class SearchRequest(BaseModel):
    grab: bool = False


def _queue(task, *args, **kwargs) -> Dict[str, Any]:
    try:
        result = task.apply_async(args=args, kwargs=kwargs)
        return {"queued": True, "task_id": result.id}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Task queue unavailable: {exc}")


def _existing_path(path: str) -> str:
    path = os.path.realpath(path or "")
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail=f"Path not found: {path}")
    return path


# ---------------------------------------------------------------- status
@router.get("/status")
async def arr_status(current_user: dict = Depends(require_authentication)):
    from src.services.indexers.indexer_service import status_summary
    from src.services.plex_music_video_service import PlexMusicVideoClient
    from src.services.youtube_cooldown import cooldown_remaining

    def build():
        return {
            "plex_music_videos_enabled": PlexMusicVideoClient.enabled(),
            "indexers": status_summary(),
            "youtube_cooldown_seconds": int(cooldown_remaining()),
        }

    return await run_in_threadpool(build)


# ---------------------------------------------------------------- plex
@router.post("/plex/test")
async def plex_test(current_user: dict = Depends(require_authentication)):
    from src.services.plex_music_video_service import PlexMusicVideoClient

    SettingsService.reload_cache()
    try:
        info = await run_in_threadpool(PlexMusicVideoClient.from_settings().test)
        return {"success": True, **info}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


@router.post("/plex/sync")
async def plex_sync(
    scan: bool = False, current_user: dict = Depends(require_authentication)
):
    from src.tasks.arr_tasks import plex_music_video_sync_task

    return _queue(plex_music_video_sync_task, scan=scan)


# ---------------------------------------------------------------- indexers
@router.post("/indexer/test")
async def indexer_test(current_user: dict = Depends(require_authentication)):
    from src.services.indexers.indexer_service import IndexerConfig

    SettingsService.reload_cache()
    config = IndexerConfig()

    def check(name, factory):
        try:
            client = factory()
            if client is None:
                return {"configured": False}
            return {"configured": True, "ok": True, **client.test()}
        except Exception as exc:  # noqa: BLE001
            return {"configured": True, "ok": False, "error": str(exc)}

    def run():
        return {
            "prowlarr": check(
                "prowlarr",
                lambda: config.prowlarr() if config.prowlarr_url else None,
            ),
            "sabnzbd": check("sabnzbd", config.sabnzbd),
            "qbittorrent": check("qbittorrent", config.qbittorrent),
            "usable": config.usable,
        }

    return await run_in_threadpool(run)


@router.get("/indexer/grabs")
async def indexer_grabs(
    limit: int = 50, current_user: dict = Depends(require_authentication)
):
    def load():
        with get_db() as session:
            rows = (
                session.query(IndexerGrab, Video, Artist)
                .join(Video, IndexerGrab.video_id == Video.id)
                .join(Artist, Video.artist_id == Artist.id)
                .order_by(IndexerGrab.id.desc())
                .limit(max(1, min(limit, 500)))
                .all()
            )
            return [
                {**grab.to_dict(), "artist": artist.name, "video_title": video.title}
                for grab, video, artist in rows
            ]

    return {"grabs": await run_in_threadpool(load)}


@router.post("/indexer/search/{video_id}")
async def indexer_search(
    video_id: int,
    body: SearchRequest = SearchRequest(),
    current_user: dict = Depends(require_authentication),
):
    """Manual search for one video; optionally grab the best accepted release."""
    from src.services.indexers.indexer_service import (
        IndexerConfig,
        search_video,
        send_release,
    )

    def run():
        config = IndexerConfig()
        if not config.usable:
            raise HTTPException(status_code=400, detail="Indexers are not configured")
        with get_db() as session:
            video = session.query(Video).filter(Video.id == video_id).first()
            if not video:
                raise HTTPException(status_code=404, detail="Video not found")
            purpose = "upgrade" if video.local_path else "wanted"
        scored = search_video(video_id, purpose, config)
        grabbed = None
        if body.grab:
            accepted = [s for s in scored if s.accepted]
            if accepted:
                grabbed = send_release(video_id, accepted[0], purpose, config).to_dict()
        return {
            "releases": [s.to_dict() for s in scored[:50]],
            "grabbed": grabbed,
        }

    return await run_in_threadpool(run)


@router.post("/youtube/cooldown/clear")
async def clear_youtube_cooldown(current_user: dict = Depends(require_authentication)):
    from src.services.youtube_cooldown import clear_cooldown

    await run_in_threadpool(clear_cooldown)
    return {"success": True}


# ---------------------------------------------------------------- library import
@router.post("/library/import")
async def library_import(
    body: ImportRequest, current_user: dict = Depends(require_admin)
):
    """Register existing files in place (dry_run first to see what would happen)."""
    from src.services.library_import_service import import_directory

    path = _existing_path(body.path)
    return await run_in_threadpool(
        import_directory, path, body.dry_run, body.monitor_new_artists
    )


# ---------------------------------------------------------------- lists
@router.get("/lists")
async def lists(current_user: dict = Depends(require_authentication)):
    from src.services.media_list_service import list_summaries

    return {"lists": await run_in_threadpool(list_summaries)}


@router.get("/lists/{list_id}")
async def list_detail(
    list_id: int, current_user: dict = Depends(require_authentication)
):
    from src.services.media_list_service import list_entries

    found = await run_in_threadpool(list_entries, list_id)
    if not found:
        raise HTTPException(status_code=404, detail="List not found")
    return found


@router.post("/lists/billboard/refresh")
async def billboard_refresh(
    body: BillboardRefreshRequest = BillboardRefreshRequest(),
    current_user: dict = Depends(require_authentication),
):
    from src.tasks.arr_tasks import media_lists_refresh_task

    return _queue(media_lists_refresh_task, years=body.years)


@router.post("/lists/link")
async def lists_link(current_user: dict = Depends(require_authentication)):
    from src.services.media_list_service import link_entries

    return await run_in_threadpool(link_entries)


@router.post("/lists/billboard/import-state")
async def billboard_import_state(
    body: BillboardStateRequest, current_user: dict = Depends(require_admin)
):
    """One-time takeover from the homelab Billboard script (state.db + lists.json)."""
    from src.services.media_list_service import import_billboard_state

    state_db = _existing_path(body.state_db)
    lists_json = _existing_path(body.lists_json) if body.lists_json else None
    return await run_in_threadpool(
        import_billboard_state, state_db, lists_json, body.path_from, body.path_to
    )
