"""
Fill in missing artist genres from MusicBrainz, so the Plex sync can build
genre collections. Artists imported from disk or created for chart lists
have no genres; one MusicBrainz search per artist (no API key, 1 request a
second) usually finds the community tags.
"""

from typing import Callable, Dict, List, Optional

from src.database.connection import get_db
from src.database.models import Artist, Video, VideoStatus
from src.utils.logger import get_logger
from src.utils.music_matching import artist_key, primary_artist

logger = get_logger("mvidarr.artist_genres")

MIN_CONFIDENCE = 0.9
MAX_TAGS = 8


def _pick(results: List[Dict], name: str) -> Optional[Dict]:
    """Best search result whose name is the artist's (ignoring a leading 'The')."""
    wanted = artist_key(name)
    for result in results or []:
        if (result.get("confidence") or 0) < MIN_CONFIDENCE:
            continue
        if artist_key(result.get("name") or "") == wanted:
            return result
    return None


def _tags(result: Dict) -> List[str]:
    names = []
    for value in (result.get("genres") or []) + (result.get("tags") or []):
        tag = (value.get("name") if isinstance(value, dict) else value) or ""
        tag = tag.strip().lower()
        if tag and tag not in names:
            names.append(tag)
    return names[:MAX_TAGS]


def fill_artist_genres(
    limit: int = 200, search: Optional[Callable[[str], List[Dict]]] = None
) -> Dict:
    """
    Look up genres for up to `limit` artists that have downloaded videos and
    no genres yet. An artist with no usable result gets an empty list, so it
    is not searched again on every run.
    """
    if search is None:
        from src.services.musicbrainz_service import musicbrainz_service

        search = musicbrainz_service.search_artist

    stats = {"checked": 0, "filled": 0, "not_found": 0, "errors": 0}
    with get_db() as session:
        candidates = (
            session.query(Artist)
            .join(Video, Video.artist_id == Artist.id)
            .filter(Video.status == VideoStatus.DOWNLOADED)
            .distinct()
            .order_by(Artist.id)
            .all()
        )
        todo = [a for a in candidates if a.genres is None or a.genres == ""][:limit]
        for index, artist in enumerate(todo, start=1):
            stats["checked"] += 1
            name = primary_artist(artist.name) or artist.name
            try:
                result = _pick(search(name), name)
            except Exception as exc:  # noqa: BLE001 - network errors: try next run
                logger.warning(f"MusicBrainz lookup failed for {artist.name}: {exc}")
                stats["errors"] += 1
                continue
            tags = _tags(result) if result else []
            artist.genres = tags
            if result and result.get("mbid") and not artist.musicbrainz_id:
                artist.musicbrainz_id = result["mbid"]
            stats["filled" if tags else "not_found"] += 1
            if index % 20 == 0:
                session.commit()
        session.commit()
    logger.info(
        "Artist genres: {checked} checked, {filled} filled, {not_found} not found, "
        "{errors} errors".format(**stats)
    )
    return stats
