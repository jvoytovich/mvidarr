"""
In-place import of music video files that already exist on disk.

Files are never moved or renamed: each one becomes (or is linked to) a
DOWNLOADED video whose local_path points at the file where it is. That keeps
the Plex items (and their watch history and collections) intact and stops
MVidarr from downloading the same song a second time.

Artist and title come from 'Artist - Title.ext'; when the name has no ' - '
the parent folder is taken as the artist. A yt-dlp '.info.json' next to the
file supplies the YouTube id. A file that matches a WANTED video (same main
artist and core title) fills that video instead of creating a new one.
"""

import json
import os
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func

from src.database.connection import get_db
from src.database.models import Artist, Video, VideoStatus
from src.utils.logger import get_logger
from src.utils.music_matching import (
    a_side,
    clean_video_title,
    parse_artist_title,
    song_key,
    split_featured,
)

logger = get_logger("mvidarr.library_import")

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".m4v",
    ".mov",
    ".avi",
    ".webm",
    ".mpg",
    ".mpeg",
    ".ts",
    ".wmv",
    ".flv",
    ".vob",
}
_SKIP_DIRS = {"@eaDir", "#recycle", ".Trash", "lost+found", "Sample", "sample"}


def scan_video_files(root: str) -> List[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS
        )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
                if "sample" in name.lower() and os.path.getsize(
                    os.path.join(dirpath, name)
                ) < (50 * 1024 * 1024):
                    continue
                found.append(os.path.join(dirpath, name))
    return found


def parse_library_file(path: str, root: str) -> Tuple[Optional[str], Optional[str]]:
    stem = os.path.splitext(os.path.basename(path))[0]
    artist, title = parse_artist_title(stem)
    if artist and title:
        return artist, title
    parent = os.path.basename(os.path.dirname(path))
    if os.path.normpath(os.path.dirname(path)) != os.path.normpath(root) and parent:
        return parent, stem
    return None, None


def _sidecar_info(path: str) -> Dict:
    info_path = os.path.splitext(path)[0] + ".info.json"
    if not os.path.exists(info_path):
        return {}
    try:
        with open(info_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def find_or_create_artist(
    session,
    name: str,
    monitored: bool = False,
    cache: Optional[Dict[str, Artist]] = None,
) -> Artist:
    """
    Exact (case-insensitive) artist lookup on the main credited name.
    New artists start unmonitored with auto-download off, so importing a
    library or a chart list never triggers discovery for hundreds of acts.
    """
    main = split_featured(name)[0] or name
    key = main.strip().lower()
    if cache is not None and key in cache:
        return cache[key]
    artist = (
        session.query(Artist).filter(func.lower(Artist.name) == key).first()
        if key
        else None
    )
    if not artist:
        artist = Artist(
            name=main.strip(),
            monitored=monitored,
            auto_download=False,
            download_enabled=True,
            discovery_enabled=False,
        )
        session.add(artist)
        session.flush()
    if cache is not None:
        cache[key] = artist
    return artist


def build_song_index(session) -> Dict[str, Video]:
    """song_key -> video for every video that has no file yet."""
    index: Dict[str, Video] = {}
    rows = (
        session.query(Video, Artist)
        .join(Artist, Video.artist_id == Artist.id)
        .filter(Video.status != VideoStatus.DOWNLOADED)
        .all()
    )
    for video, artist in rows:
        index.setdefault(song_key(artist.name, video.title or ""), video)
    return index


def import_directory(
    root: str,
    dry_run: bool = False,
    monitor_new_artists: bool = False,
    limit: Optional[int] = None,
) -> Dict:
    """Register every video file under root. Safe to run repeatedly."""
    stats = {
        "root": root,
        "files": 0,
        "already_known": 0,
        "linked_to_wanted": 0,
        "created": 0,
        "unparsed": [],
        "errors": [],
        "dry_run": dry_run,
    }
    if not os.path.isdir(root):
        stats["errors"].append(f"Not a directory: {root}")
        return stats

    files = scan_video_files(root)
    if limit:
        files = files[:limit]
    stats["files"] = len(files)

    with get_db() as session:
        known_paths = {
            p
            for (p,) in session.query(Video.local_path).filter(
                Video.local_path.isnot(None)
            )
        }
        known_youtube = {
            y
            for (y,) in session.query(Video.youtube_id).filter(
                Video.youtube_id.isnot(None)
            )
        }
        index = build_song_index(session)
        artists: Dict[str, Artist] = {}

        for path in files:
            if path in known_paths:
                stats["already_known"] += 1
                continue
            artist_name, raw_title = parse_library_file(path, root)
            if not artist_name or not raw_title:
                stats["unparsed"].append(path)
                continue
            title = a_side(clean_video_title(raw_title, artist_name))
            info = _sidecar_info(path)
            youtube_id = None
            if info.get("extractor_key", "Youtube") == "Youtube":
                youtube_id = info.get("id")
            key = song_key(artist_name, title)
            if dry_run:
                stats["linked_to_wanted" if index.pop(key, None) else "created"] += 1
                continue
            try:
                savepoint = session.begin_nested()
                video = index.pop(key, None)
                if video:
                    stats["linked_to_wanted"] += 1
                else:
                    artist = find_or_create_artist(
                        session,
                        artist_name,
                        monitored=monitor_new_artists,
                        cache=artists,
                    )
                    video = Video(
                        artist_id=artist.id,
                        title=title,
                        source="import",
                    )
                    session.add(video)
                    stats["created"] += 1
                video.status = VideoStatus.DOWNLOADED
                video.local_path = path
                if (
                    youtube_id
                    and youtube_id not in known_youtube
                    and not video.youtube_id
                ):
                    video.youtube_id = youtube_id
                    video.youtube_url = f"https://www.youtube.com/watch?v={youtube_id}"
                    known_youtube.add(youtube_id)
                if info.get("duration") and not video.duration:
                    video.duration = int(info["duration"])
                session.flush()
                savepoint.commit()
                known_paths.add(path)
            except (
                Exception
            ) as exc:  # noqa: BLE001 - one bad file must not stop the import
                savepoint.rollback()
                stats["errors"].append(f"{path}: {exc}")
                logger.warning(f"Import failed for {path}: {exc}")
        if not dry_run:
            session.commit()

    if len(stats["unparsed"]) > 50:
        stats["unparsed"] = stats["unparsed"][:50] + ["..."]
    logger.info(
        f"Library import of {root}: {stats['files']} files, "
        f"{stats['created']} new, {stats['linked_to_wanted']} filled wanted, "
        f"{stats['already_known']} already known"
    )
    return stats
