"""
Usenet first, torrents second, YouTube last.

When a WANTED video is dispatched, the download thread asks this service
first. It searches Prowlarr (category 3020, music videos), scores the
releases, and sends the best one to SABnzbd (or qBittorrent when only a
torrent fits). A poll task watches the job, imports the finished file into
the library as 'Artist/Artist - Title.ext' and tells Plex to scan. If the
job fails, the next-best release is tried; when none is left the video goes
back to WANTED and the next download run uses YouTube.

The same machinery searches for upgrades of videos that came from YouTube:
a release with a higher resolution (or any release for an unofficial clip)
replaces the YouTube file.

Searches are budgeted per day (indexer API limits) and a video is not
searched again for `indexer_research_days`.
"""

import os
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from sqlalchemy import func

from src.database.connection import get_db
from src.database.models import (
    Artist,
    Download,
    IndexerGrab,
    IndexerSearch,
    Video,
    VideoStatus,
)
from src.services.indexers.download_clients import (
    ClientStatus,
    DownloadClientError,
    QbittorrentClient,
    SabnzbdClient,
)
from src.services.indexers.prowlarr import (
    ProwlarrClient,
    ProwlarrError,
    ProwlarrLimitError,
)
from src.services.indexers.release_scoring import ScoredRelease, rank_releases
from src.services.plex_music_video_service import (
    map_path,
    notify_file_added,
    parse_path_mappings,
)
from src.services.settings_service import SettingsService
from src.utils.logger import get_logger
from src.utils.music_matching import (
    core_title,
    library_folder,
    library_stem,
    primary_artist,
)

logger = get_logger("mvidarr.indexers")

ACTIVE_STATES = ("grabbed", "downloading", "completed", "importing")
# Video types where "artist + title" would find the studio video instead.
_SKIP_TYPES = {
    "live_performance",
    "concert_footage",
    "acoustic_version",
    "lyric_video",
    "cover_version",
    "remix_version",
    "music_documentary",
}
_MEDIA_EXTS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2ts",
    ".vob",
    ".wmv",
    ".webm",
}
_SIDECAR_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".info.json", ".nfo")

# At most two concurrent Prowlarr searches from download threads.
_search_slots = threading.BoundedSemaphore(2)


class IndexerConfig:
    def __init__(self):
        get = SettingsService.get
        self.enabled = SettingsService.get_bool("indexer_enabled", False)
        self.prowlarr_url = get("prowlarr_url", "")
        self.prowlarr_api_key = get("prowlarr_api_key", "")
        self.sab_url = get("sabnzbd_url", "")
        self.sab_api_key = get("sabnzbd_api_key", "")
        self.sab_category = get("sabnzbd_category", "musicvideos")
        self.torrents_enabled = SettingsService.get_bool(
            "indexer_torrents_enabled", True
        )
        self.qbit_url = get("qbittorrent_url", "")
        self.qbit_username = get("qbittorrent_username", "")
        self.qbit_password = get("qbittorrent_password", "")
        self.qbit_category = get("qbittorrent_category", "musicvideos")
        self.daily_budget = SettingsService.get_int("indexer_max_searches_per_day", 150)
        self.research_days = SettingsService.get_int("indexer_research_days", 14)
        self.upgrade_per_day = SettingsService.get_int(
            "indexer_upgrade_searches_per_day", 25
        )
        self.upgrade_research_days = SettingsService.get_int(
            "indexer_upgrade_research_days", 60
        )
        self.min_resolution = SettingsService.get_int("indexer_min_resolution", 480)
        self.max_size_mb = SettingsService.get_int("indexer_max_size_mb", 4096)
        self.max_grabs = SettingsService.get_int("indexer_max_grabs_per_video", 2)
        self.stall_hours = SettingsService.get_int("indexer_stall_hours", 24)
        # A torrent that never found a peer (metaDL/stalledDL at 0%) is dead
        self.torrent_dead_hours = SettingsService.get_int(
            "indexer_torrent_dead_hours", 3
        )
        self.path_mappings = parse_path_mappings(get("indexer_path_mappings", ""))
        self.music_videos_path = get("music_videos_path", "data/musicvideos")

    @property
    def usable(self) -> bool:
        has_client = bool(self.sab_url and self.sab_api_key) or (
            self.torrents_enabled and bool(self.qbit_url)
        )
        return (
            self.enabled
            and bool(self.prowlarr_url and self.prowlarr_api_key)
            and has_client
        )

    def prowlarr(self) -> ProwlarrClient:
        return ProwlarrClient(self.prowlarr_url, self.prowlarr_api_key)

    def sabnzbd(self) -> Optional[SabnzbdClient]:
        if self.sab_url and self.sab_api_key:
            return SabnzbdClient(self.sab_url, self.sab_api_key, self.sab_category)
        return None

    def qbittorrent(self) -> Optional[QbittorrentClient]:
        if self.torrents_enabled and self.qbit_url:
            return QbittorrentClient(
                self.qbit_url,
                self.qbit_username,
                self.qbit_password,
                self.qbit_category,
            )
        return None


def _day_start() -> datetime:
    now = datetime.utcnow()
    return datetime(now.year, now.month, now.day)


def searches_today(session) -> int:
    return (
        session.query(func.count(IndexerSearch.id))
        .filter(IndexerSearch.created_at >= _day_start())
        .filter(IndexerSearch.outcome != "budget")
        .scalar()
        or 0
    )


def _limited_until_tomorrow(session) -> bool:
    """An indexer limit hit today stops searching until the UTC day rolls over."""
    return (
        session.query(IndexerSearch.id)
        .filter(IndexerSearch.created_at >= _day_start())
        .filter(IndexerSearch.outcome == "limit")
        .first()
        is not None
    )


def should_search(
    session, video: Video, config: IndexerConfig, purpose: str = "wanted"
) -> Tuple[bool, str]:
    if not config.usable:
        return False, "indexers not configured"
    if (video.video_type or "") in _SKIP_TYPES:
        return False, f"video type {video.video_type}"
    if searches_today(session) >= config.daily_budget or _limited_until_tomorrow(
        session
    ):
        return False, "daily search budget used"
    active = (
        session.query(IndexerGrab.id)
        .filter(IndexerGrab.video_id == video.id)
        .filter(IndexerGrab.status.in_(ACTIVE_STATES))
        .first()
    )
    if active:
        return False, "already grabbed"
    days = config.research_days if purpose == "wanted" else config.upgrade_research_days
    recent = (
        session.query(IndexerSearch.id)
        .filter(IndexerSearch.video_id == video.id)
        .filter(IndexerSearch.purpose == purpose)
        .filter(IndexerSearch.created_at >= datetime.utcnow() - timedelta(days=days))
        .filter(IndexerSearch.outcome != "budget")
        .first()
    )
    if recent:
        return False, "searched recently"
    return True, ""


def search_query(artist: str, title: str) -> str:
    return f"{primary_artist(artist)} {core_title(title)}".strip()


def _tried_guids(session, video_id: int) -> List[str]:
    return [
        g
        for (g,) in session.query(IndexerGrab.release_guid).filter(
            IndexerGrab.video_id == video_id
        )
        if g
    ]


def search_video(
    video_id: int, purpose: str = "wanted", config: Optional[IndexerConfig] = None
) -> List[ScoredRelease]:
    """Search Prowlarr for one video and record the search. Returns all scored releases."""
    config = config or IndexerConfig()
    with get_db() as session:
        video = session.query(Video).filter(Video.id == video_id).first()
        if not video:
            return []
        artist = video.artist.name if video.artist else ""
        title = video.title or ""
        exclude = _tried_guids(session, video_id)
    query = search_query(artist, title)
    record = IndexerSearch(video_id=video_id, query=query[:500], purpose=purpose)
    scored: List[ScoredRelease] = []
    try:
        releases = config.prowlarr().search(query)
        scored = rank_releases(
            releases,
            artist,
            title,
            min_resolution=config.min_resolution,
            max_size_mb=config.max_size_mb,
            exclude_guids=exclude,
        )
        if not config.torrents_enabled or not config.qbittorrent():
            scored = [s for s in scored if s.release.protocol == "usenet"]
        if not config.sabnzbd():
            scored = [s for s in scored if s.release.protocol == "torrent"]
        record.results_count = len(releases)
        record.accepted_count = sum(1 for s in scored if s.accepted)
        record.outcome = "found" if record.accepted_count else "no_match"
    except ProwlarrLimitError as exc:
        record.outcome = "limit"
        record.error = str(exc)
    except (ProwlarrError, requests.RequestException) as exc:
        record.outcome = "error"
        record.error = str(exc)[:1000]
        logger.warning(f"Prowlarr search failed for {query!r}: {exc}")
    with get_db() as session:
        session.add(record)
        session.commit()
    return scored


def send_release(
    video_id: int,
    scored: ScoredRelease,
    purpose: str,
    config: Optional[IndexerConfig] = None,
) -> IndexerGrab:
    """Fetch the NZB/torrent and hand it to the download client. Records the grab."""
    config = config or IndexerConfig()
    release = scored.release
    tag = f"mvidarr-{uuid.uuid4().hex[:12]}"
    if release.protocol == "usenet":
        client = config.sabnzbd()
        if not client:
            raise DownloadClientError("SABnzbd is not configured")
        if not release.download_url:
            raise DownloadClientError("Release has no NZB link")
        nzb = config.prowlarr().fetch(release.download_url)
        if nzb.status_code in (301, 302, 303, 307, 308):
            nzb = config.prowlarr().http.get(nzb.headers["Location"], timeout=90)
        client_id = client.add_nzb(nzb.content, release.title)
    else:
        client = config.qbittorrent()
        if not client:
            raise DownloadClientError("qBittorrent is not configured")
        magnet = release.magnet_url
        torrent = None
        if not magnet and release.download_url:
            resp = config.prowlarr().fetch(release.download_url)
            location = resp.headers.get("Location", "")
            if location.startswith("magnet:"):
                magnet = location
            else:
                torrent = resp.content
        client.add(tag, magnet=magnet, torrent=torrent)
        client_id = tag

    grab = IndexerGrab(
        video_id=video_id,
        purpose=purpose,
        release_title=release.title[:500],
        release_guid=release.guid[:1000],
        indexer=release.indexer,
        protocol=release.protocol,
        client=client.name,
        client_id=client_id,
        size=release.size or None,
        resolution=scored.resolution,
        score=scored.score,
        status="grabbed",
    )
    with get_db() as session:
        session.add(grab)
        session.commit()
        session.refresh(grab)
        session.expunge(grab)
    logger.info(
        f"Grabbed {release.title} ({release.protocol}, {release.indexer}) for video {video_id}"
    )
    return grab


def try_indexer_first(video_id: int) -> Optional[str]:
    """
    Called from the download thread before YouTube. Returns a short message
    when the video was handed to SABnzbd/qBittorrent, None to carry on with
    YouTube. Never raises.
    """
    try:
        config = IndexerConfig()
        if not config.usable or not video_id:
            return None
        with _search_slots:
            with get_db() as session:
                video = session.query(Video).filter(Video.id == video_id).first()
                if not video:
                    return None
                ok, why = should_search(session, video, config, "wanted")
            if not ok:
                logger.debug(f"Indexer search skipped for video {video_id}: {why}")
                return None
            scored = search_video(video_id, "wanted", config)
            for candidate in [s for s in scored if s.accepted][:3]:
                try:
                    grab = send_release(video_id, candidate, "wanted", config)
                    _mark_search_grabbed(video_id)
                    return f"Sent to {grab.client}: {grab.release_title}"
                except (
                    DownloadClientError,
                    ProwlarrError,
                    requests.RequestException,
                ) as exc:
                    logger.warning(f"Could not grab {candidate.release.title}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - YouTube must still run if this breaks
        logger.error(f"Indexer step failed for video {video_id}: {exc}", exc_info=True)
        return None


def _mark_search_grabbed(video_id: int) -> None:
    with get_db() as session:
        record = (
            session.query(IndexerSearch)
            .filter(IndexerSearch.video_id == video_id)
            .order_by(IndexerSearch.id.desc())
            .first()
        )
        if record:
            record.outcome = "grabbed"
            session.commit()


# ---------------------------------------------------------------- polling
def _client_for(grab: IndexerGrab, config: IndexerConfig):
    return config.sabnzbd() if grab.client == "sabnzbd" else config.qbittorrent()


def poll_grabs(config: Optional[IndexerConfig] = None) -> Dict:
    """Check every active grab; import finished ones, fail stuck ones."""
    config = config or IndexerConfig()
    stats = {"checked": 0, "imported": 0, "failed": 0, "waiting": 0}
    _release_stale_imports()
    with get_db() as session:
        grabs = (
            session.query(IndexerGrab)
            .filter(IndexerGrab.status.in_(("grabbed", "downloading", "completed")))
            .all()
        )
        for grab in grabs:
            session.expunge(grab)
    for grab in grabs:
        stats["checked"] += 1
        client = _client_for(grab, config)
        if not client:
            _fail_grab(grab.id, f"{grab.client} is no longer configured", config)
            stats["failed"] += 1
            continue
        try:
            status: ClientStatus = client.status(grab.client_id)
        except (DownloadClientError, requests.RequestException) as exc:
            logger.warning(f"Could not poll {grab.client} for grab {grab.id}: {exc}")
            stats["waiting"] += 1
            continue
        if status.state == "completed":
            if not _claim_import(grab.id):
                stats["waiting"] += 1
                continue
            try:
                import_grab(grab.id, status.path, config)
                stats["imported"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Import failed for grab {grab.id}: {exc}", exc_info=True)
                _fail_grab(grab.id, f"import failed: {exc}", config)
                stats["failed"] += 1
        elif status.state in ("failed", "missing"):
            _fail_grab(
                grab.id,
                status.message or f"job {status.state} in {grab.client}",
                config,
            )
            stats["failed"] += 1
        else:
            age = datetime.utcnow() - (grab.created_at or datetime.utcnow())
            no_peers = (
                grab.protocol == "torrent"
                and not status.progress
                and (status.message or "").startswith(("metaDL", "stalledDL"))
            )
            if no_peers and age > timedelta(hours=config.torrent_dead_hours):
                try:
                    client.remove(grab.client_id, delete_files=True)
                except Exception:  # noqa: BLE001
                    pass
                _fail_grab(
                    grab.id,
                    f"no peers after {config.torrent_dead_hours} h ({status.message})",
                    config,
                )
                stats["failed"] += 1
            elif age > timedelta(hours=config.stall_hours):
                try:
                    client.remove(
                        grab.client_id, delete_files=grab.protocol == "usenet"
                    )
                except Exception:  # noqa: BLE001
                    pass
                _fail_grab(grab.id, f"no progress after {config.stall_hours} h", config)
                stats["failed"] += 1
            else:
                _set_grab_status(grab.id, "downloading")
                stats["waiting"] += 1
    return stats


def _claim_import(grab_id: int) -> bool:
    """Atomically move a grab to 'importing' so overlapping polls import it once."""
    with get_db() as session:
        updated = (
            session.query(IndexerGrab)
            .filter(IndexerGrab.id == grab_id)
            .filter(IndexerGrab.status.in_(("grabbed", "downloading", "completed")))
            .update({IndexerGrab.status: "importing"}, synchronize_session=False)
        )
        session.commit()
        return updated == 1


def _release_stale_imports() -> None:
    """An import that died mid-way (worker restart) is retried after an hour."""
    with get_db() as session:
        session.query(IndexerGrab).filter(IndexerGrab.status == "importing").filter(
            IndexerGrab.updated_at < datetime.utcnow() - timedelta(hours=1)
        ).update({IndexerGrab.status: "completed"}, synchronize_session=False)
        session.commit()


def _set_grab_status(grab_id: int, status: str, **fields) -> None:
    with get_db() as session:
        grab = session.query(IndexerGrab).filter(IndexerGrab.id == grab_id).first()
        if grab:
            grab.status = status
            for key, value in fields.items():
                setattr(grab, key, value)
            session.commit()


def _fail_grab(grab_id: int, reason: str, config: IndexerConfig) -> None:
    """Mark failed; try the next release, or let YouTube have the video."""
    with get_db() as session:
        grab = session.query(IndexerGrab).filter(IndexerGrab.id == grab_id).first()
        if not grab:
            return
        grab.status = "failed"
        grab.error = (reason or "")[:2000]
        video_id, purpose = grab.video_id, grab.purpose
        attempts = (
            session.query(func.count(IndexerGrab.id))
            .filter(IndexerGrab.video_id == video_id)
            .filter(IndexerGrab.purpose == purpose)
            .scalar()
        )
        session.commit()
    logger.warning(f"Grab {grab_id} for video {video_id} failed: {reason}")

    if attempts < config.max_grabs:
        scored = search_video(video_id, purpose, config)
        for candidate in [s for s in scored if s.accepted][:2]:
            if purpose == "upgrade" and not _is_upgrade(video_id, candidate):
                continue
            try:
                send_release(video_id, candidate, purpose, config)
                _mark_search_grabbed(video_id)
                return
            except (
                DownloadClientError,
                ProwlarrError,
                requests.RequestException,
            ) as exc:
                logger.warning(f"Retry grab failed for video {video_id}: {exc}")

    if purpose == "wanted":
        # Back to WANTED: the next download run skips the indexers for this
        # video (searched recently) and uses YouTube.
        with get_db() as session:
            video = session.query(Video).filter(Video.id == video_id).first()
            if video and video.status != VideoStatus.DOWNLOADED:
                video.status = VideoStatus.WANTED
                session.commit()


# ---------------------------------------------------------------- import
def _largest_media_file(path: str) -> Optional[str]:
    if not path:
        return None
    if os.path.isfile(path):
        return path if os.path.splitext(path)[1].lower() in _MEDIA_EXTS else None
    best, best_size = None, -1
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in _MEDIA_EXTS:
                continue
            if "sample" in name.lower():
                continue
            full = os.path.join(dirpath, name)
            size = os.path.getsize(full)
            if size > best_size:
                best, best_size = full, size
    return best


def _sidecars(video_path: str) -> List[str]:
    stem = os.path.splitext(video_path)[0]
    return [stem + ext for ext in _SIDECAR_EXTS if os.path.exists(stem + ext)]


def _place_file(src: str, dest: str, protocol: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".mvidarr-part"
    if protocol == "torrent":
        # Keep seeding: hard link when on the same filesystem, else copy.
        try:
            os.link(src, tmp)
        except OSError:
            shutil.copy2(src, tmp)
    else:
        shutil.move(src, tmp)
    os.replace(tmp, dest)


def _write_poster(
    video_path: str, youtube_id: Optional[str], old_poster: Optional[str]
) -> None:
    poster = os.path.splitext(video_path)[0] + ".jpg"
    if os.path.exists(poster):
        return
    if old_poster and os.path.exists(old_poster):
        shutil.move(old_poster, poster)
        return
    if youtube_id:
        for size in ("maxresdefault", "hqdefault"):
            try:
                resp = requests.get(
                    f"https://i.ytimg.com/vi/{youtube_id}/{size}.jpg", timeout=30
                )
                if resp.status_code == 200 and len(resp.content) > 5000:
                    with open(poster, "wb") as handle:
                        handle.write(resp.content)
                    return
            except requests.RequestException:
                pass
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                "30",
                "-i",
                video_path,
                "-frames:v",
                "1",
                "-q:v",
                "3",
                poster,
            ],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def probe_height(path: str) -> Optional[int]:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=height",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return int((proc.stdout or "").strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


def import_grab(
    grab_id: int, client_path: Optional[str], config: Optional[IndexerConfig] = None
) -> str:
    """Move/link a finished download into the library and mark the video DOWNLOADED."""
    config = config or IndexerConfig()
    with get_db() as session:
        grab = session.query(IndexerGrab).filter(IndexerGrab.id == grab_id).first()
        video = session.query(Video).filter(Video.id == grab.video_id).first()
        artist = session.query(Artist).filter(Artist.id == video.artist_id).first()
        protocol, purpose = grab.protocol, grab.purpose
        from src.services.library_import_service import video_credit

        artist_name, title = video_credit(session, video, artist.name), video.title
        old_path = video.local_path if video.status == VideoStatus.DOWNLOADED else None
        youtube_id = video.youtube_id

    local = map_path(client_path or "", config.path_mappings)
    source = _largest_media_file(local)
    if not source:
        raise FileNotFoundError(f"No video file in {local or client_path!r}")

    ext = os.path.splitext(source)[1].lower()
    dest = os.path.join(
        config.music_videos_path,
        library_folder(artist_name),
        library_stem(artist_name, title) + ext,
    )
    old_poster = None
    if old_path and old_path != dest and os.path.exists(old_path):
        posters = [p for p in _sidecars(old_path) if p.endswith(".jpg")]
        old_poster = posters[0] if posters else None
    _place_file(source, dest, protocol)
    _write_poster(dest, youtube_id, old_poster)

    if old_path and old_path != dest:
        for path in [old_path] + _sidecars(old_path):
            try:
                if os.path.exists(path) and path != os.path.splitext(dest)[0] + ".jpg":
                    os.remove(path)
            except OSError as exc:
                logger.warning(f"Could not remove replaced file {path}: {exc}")

    if protocol == "usenet" and os.path.isdir(local) and not _largest_media_file(local):
        shutil.rmtree(local, ignore_errors=True)

    height = probe_height(dest)
    with get_db() as session:
        grab = session.query(IndexerGrab).filter(IndexerGrab.id == grab_id).first()
        video = session.query(Video).filter(Video.id == grab.video_id).first()
        video.status = VideoStatus.DOWNLOADED
        video.local_path = dest
        resolution = height or grab.resolution
        if resolution:
            video.quality = f"{resolution}p"
        if video.video_type == "unofficial_clip":
            video.video_type = "official_music_video"
        metadata = (
            dict(video.video_metadata or {})
            if isinstance(video.video_metadata, dict)
            else {}
        )
        metadata["mvidarr_source"] = {
            "origin": protocol,
            "release": grab.release_title,
            "indexer": grab.indexer,
            "imported_at": datetime.utcnow().isoformat(),
        }
        video.video_metadata = metadata
        size = os.path.getsize(dest)
        session.add(
            Download(
                artist_id=video.artist_id,
                video_id=video.id,
                title=video.title,
                original_url=f"{protocol}:{grab.release_title}"[:500],
                status="completed",
                file_path=dest,
                file_size=size,
                quality=video.quality,
                format=ext.lstrip("."),
                download_date=datetime.utcnow(),
                progress=100,
            )
        )
        grab.status = "imported"
        grab.download_path = client_path
        grab.imported_path = dest
        grab.completed_at = datetime.utcnow()
        session.commit()
    logger.info(f"Imported {grab_id} ({purpose}) to {dest}")
    notify_file_added(dest)
    return dest


# ---------------------------------------------------------------- upgrades
def _current_height(video_id: int) -> Tuple[Optional[int], Optional[str]]:
    with get_db() as session:
        video = session.query(Video).filter(Video.id == video_id).first()
        if not video:
            return None, None
        quality, path, video_type = video.quality, video.local_path, video.video_type
    if quality and quality.rstrip("p").isdigit():
        return int(quality.rstrip("p")), video_type
    return (probe_height(path) if path else None), video_type


def _is_upgrade(video_id: int, candidate: ScoredRelease) -> bool:
    height, video_type = _current_height(video_id)
    new = candidate.resolution or 0
    if video_type == "unofficial_clip":
        return new >= int((height or 0) * 0.75)
    return height is not None and new > height


def upgrade_candidates(session, config: IndexerConfig, limit: int) -> List[int]:
    since = datetime.utcnow() - timedelta(days=config.upgrade_research_days)
    recently = session.query(IndexerSearch.video_id).filter(
        IndexerSearch.purpose == "upgrade", IndexerSearch.created_at >= since
    )
    from_indexer = session.query(IndexerGrab.video_id).filter(
        IndexerGrab.status.in_(("imported",) + ACTIVE_STATES)
    )
    query = (
        session.query(Video.id)
        .filter(Video.status == VideoStatus.DOWNLOADED)
        .filter(Video.local_path.isnot(None))
        .filter(~Video.id.in_(recently))
        .filter(~Video.id.in_(from_indexer))
        .filter(
            (Video.video_type.is_(None)) | (~Video.video_type.in_(list(_SKIP_TYPES)))
        )
        .order_by((Video.video_type == "unofficial_clip").desc(), Video.id)
        .limit(limit)
    )
    return [vid for (vid,) in query]


def search_upgrades(config: Optional[IndexerConfig] = None) -> Dict:
    config = config or IndexerConfig()
    stats = {"searched": 0, "grabbed": 0, "skipped": None}
    if not config.usable:
        stats["skipped"] = "indexers not configured"
        return stats
    with get_db() as session:
        room = config.daily_budget - searches_today(session)
        if room <= 0 or _limited_until_tomorrow(session):
            stats["skipped"] = "daily search budget used"
            return stats
        ids = upgrade_candidates(session, config, min(config.upgrade_per_day, room))
    for video_id in ids:
        stats["searched"] += 1
        scored = search_video(video_id, "upgrade", config)
        for candidate in [s for s in scored if s.accepted][:2]:
            if not _is_upgrade(video_id, candidate):
                continue
            try:
                send_release(video_id, candidate, "upgrade", config)
                _mark_search_grabbed(video_id)
                stats["grabbed"] += 1
                break
            except (
                DownloadClientError,
                ProwlarrError,
                requests.RequestException,
            ) as exc:
                logger.warning(f"Upgrade grab failed for video {video_id}: {exc}")
    return stats


def status_summary() -> Dict:
    config = IndexerConfig()
    with get_db() as session:
        by_status = dict(
            session.query(IndexerGrab.status, func.count(IndexerGrab.id))
            .group_by(IndexerGrab.status)
            .all()
        )
        return {
            "enabled": config.enabled,
            "usable": config.usable,
            "searches_today": searches_today(session),
            "daily_budget": config.daily_budget,
            "limit_hit_today": _limited_until_tomorrow(session),
            "grabs": by_status,
        }
