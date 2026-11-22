"""
Keep the Plex "Music Videos" library in step with MVidarr's database.

One pass (Celery task, every 30 minutes by default, or on demand):
  1. optionally scan the library folders in Plex,
  2. match Plex items to downloaded videos by file path,
  3. set title "Artist - Title", sort title, year, summary and genres,
  4. maintain collections: one per chart list (e.g. "Billboard Year-End 1984"),
     "Genre: Rock", "Decade: 1980s", "Artist: Madonna", plus an
     "Unofficial clips" review collection,
  5. maintain ordered playlists for ranked lists ("Billboard Year-End Hot 100: 1984").

Collections MVidarr manages are recognised by their prefix; anything else in
the library is left alone.
"""

import unicodedata
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from src.database.connection import get_db
from src.database.models import (
    Artist,
    MediaList,
    MediaListEntry,
    Video,
    VideoStatus,
)
from src.services.plex_music_video_service import (
    COLLECTION_MODE_HIDE,
    COLLECTION_MODES,
    PlexError,
    PlexMusicVideoClient,
    wait_for_scan,
)
from src.services.settings_service import SettingsService
from src.utils.logger import get_logger
from src.utils.music_matching import a_side, clean_video_title, split_featured

logger = get_logger("mvidarr.plex_library_sync")

UNOFFICIAL_TYPE = "unofficial_clip"

GENRE_PREFIX = "Genre: "
DECADE_PREFIX = "Decade: "
ARTIST_PREFIX = "Artist: "
MANAGED_PREFIXES = (GENRE_PREFIX, DECADE_PREFIX, ARTIST_PREFIX)

# Fine-grained tags (Last.fm/Spotify/MusicBrainz) -> a short list of browsable
# genres. Order matters: the first matching rule wins for each tag.
_GENRE_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("Hip-Hop", ("hip hop", "hip-hop", "rap", "trap", "drill", "grime")),
    ("R&B & Soul", ("r&b", "rnb", "rhythm and blues", "soul", "motown", "neo soul")),
    ("Funk & Disco", ("funk", "disco", "boogie")),
    ("Country", ("country", "bluegrass", "americana", "honky tonk")),
    (
        "Electronic & Dance",
        (
            "electronic",
            "edm",
            "house",
            "techno",
            "trance",
            "dance",
            "electro",
            "synthpop",
            "synth-pop",
            "dubstep",
            "drum and bass",
            "eurodance",
        ),
    ),
    ("Metal", ("metal", "grindcore", "metalcore")),
    ("Punk", ("punk", "hardcore", "emo", "pop punk")),
    (
        "Alternative & Indie",
        ("alternative", "indie", "grunge", "shoegaze", "britpop", "new wave"),
    ),
    ("Rock", ("rock", "glam", "psychedelic", "surf", "rockabilly")),
    ("Latin", ("latin", "reggaeton", "salsa", "bachata", "cumbia", "bossa nova")),
    ("Reggae", ("reggae", "dancehall", "ska", "dub")),
    ("Jazz", ("jazz", "swing", "big band", "bebop")),
    ("Blues", ("blues",)),
    ("Folk", ("folk", "singer-songwriter", "singer songwriter", "acoustic")),
    ("Gospel & Christian", ("gospel", "christian", "worship")),
    ("Pop", ("pop", "teen", "boy band", "girl group", "adult contemporary")),
]


def broad_genres(tags: Iterable[str], limit: int = 2) -> List[str]:
    """Map fine-grained tags to at most `limit` broad genres, keeping tag order."""
    found: List[str] = []
    for tag in tags or []:
        if isinstance(tag, dict):
            tag = tag.get("name") or tag.get("tag") or ""
        low = str(tag).strip().lower()
        if not low:
            continue
        for genre, needles in _GENRE_RULES:
            if any(n in low for n in needles):
                if genre not in found:
                    found.append(genre)
                break
        if len(found) >= limit:
            break
    return found


def _as_list(value) -> List:
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, dict):
        return list(value.keys())
    return list(value)


def _nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path or "")


def sort_name(name: str) -> str:
    name = (name or "").strip()
    if name.lower().startswith("the "):
        return name[4:]
    return name


def plex_title(artist: str, title: str) -> str:
    return f"{artist} - {a_side(clean_video_title(title, artist))}"


def chart_summary(charts: List[Tuple[str, int, int]]) -> str:
    """[(chart name, year, rank)] -> 'Billboard Year-End Hot 100: #5 in 1984, #40 in 1985'."""
    by_chart: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
    for chart, year, rank in charts:
        by_chart[chart].append((year, rank))
    lines = []
    for chart, hits in sorted(by_chart.items()):
        hits.sort()
        lines.append(f"{chart}: " + ", ".join(f"#{r} in {y}" for y, r in hits))
    return "\n".join(lines)


class LibraryState:
    """What the database says the Plex library should look like."""

    def __init__(self):
        self.videos: Dict[int, Dict] = {}
        self.charts: Dict[int, List[Tuple[str, int, int]]] = defaultdict(list)
        self.lists: List[Dict] = []

    @classmethod
    def load(cls) -> "LibraryState":
        state = cls()
        with get_db() as session:
            rows = (
                session.query(Video, Artist)
                .join(Artist, Video.artist_id == Artist.id)
                .filter(Video.status == VideoStatus.DOWNLOADED)
                .filter(Video.local_path.isnot(None))
                .all()
            )
            for video, artist in rows:
                metadata = video.video_metadata or {}
                state.videos[video.id] = {
                    "id": video.id,
                    "artist_id": artist.id,
                    "artist": artist.name,
                    "artist_sort": artist.sort_name or sort_name(artist.name),
                    "artist_monitored": bool(artist.monitored),
                    "title": video.title or "",
                    "year": video.year,
                    "path": _nfc(video.local_path),
                    "video_type": video.video_type,
                    "genres": _as_list(video.genres) + _as_list(artist.genres),
                    "uploader": (
                        metadata.get("uploader") or metadata.get("channel")
                        if isinstance(metadata, dict)
                        else None
                    ),
                }
            lists = session.query(MediaList).filter(MediaList.enabled.is_(True)).all()
            for media_list in lists:
                entries = (
                    session.query(MediaListEntry)
                    .filter(MediaListEntry.list_id == media_list.id)
                    .filter(MediaListEntry.video_id.isnot(None))
                    .order_by(MediaListEntry.rank)
                    .all()
                )
                chart_name = _chart_name(media_list)
                ranked = []
                for entry in entries:
                    ranked.append((entry.rank, entry.video_id))
                    if media_list.year:
                        state.charts[entry.video_id].append(
                            (chart_name, media_list.year, entry.rank)
                        )
                state.lists.append(
                    {
                        "id": media_list.id,
                        "name": media_list.name,
                        "year": media_list.year,
                        "collection_title": media_list.collection_title,
                        "collection_sort_title": media_list.collection_sort_title,
                        "playlist_title": media_list.playlist_title,
                        "ranked": ranked,
                    }
                )
        return state

    def year_for(self, video_id: int) -> Optional[int]:
        video = self.videos[video_id]
        if video["year"]:
            return int(video["year"])
        years = [year for _, year, _ in self.charts.get(video_id, [])]
        return min(years) if years else None

    def summary_for(self, video_id: int) -> str:
        video = self.videos[video_id]
        parts = []
        charts = self.charts.get(video_id)
        if charts:
            parts.append(chart_summary(charts))
        if video["video_type"] == UNOFFICIAL_TYPE:
            note = "Unofficial clip"
            if video.get("uploader"):
                note += f" (uploaded by {video['uploader']})"
            parts.append(note + ". Flagged for review.")
        return "\n".join(parts)

    def genres_for(self, video_id: int) -> List[str]:
        limit = SettingsService.get_int("plex_mv_genres_per_video", 2)
        return broad_genres(self.videos[video_id]["genres"], limit=limit)


def _chart_name(media_list: MediaList) -> str:
    if media_list.source == "billboard_year_end":
        return "Billboard Year-End Hot 100"
    return media_list.name


def desired_collections(
    state: LibraryState, key_for: Dict[int, str]
) -> Dict[str, Tuple[List[str], str]]:
    """{collection title: (rating keys, sort title)} for everything MVidarr manages."""
    wanted: Dict[str, Tuple[List[str], str]] = {}

    if SettingsService.get_bool("plex_mv_collections_lists", True):
        for media_list in state.lists:
            title = media_list["collection_title"]
            if not title:
                continue
            keys = [key_for[v] for _, v in media_list["ranked"] if v in key_for]
            sort = media_list["collection_sort_title"] or title
            wanted[title] = (keys, sort)

    unofficial = SettingsService.get(
        "plex_mv_unofficial_collection", "Unofficial clips (review)"
    )
    if unofficial:
        keys = [
            key_for[v]
            for v, info in state.videos.items()
            if v in key_for and info["video_type"] == UNOFFICIAL_TYPE
        ]
        wanted[unofficial] = (keys, "~" + unofficial)

    if SettingsService.get_bool("plex_mv_collections_genre", True):
        minimum = SettingsService.get_int("plex_mv_genre_min_items", 3)
        by_genre: Dict[str, List[str]] = defaultdict(list)
        for video_id in state.videos:
            if video_id in key_for:
                for genre in state.genres_for(video_id):
                    by_genre[genre].append(key_for[video_id])
        for genre, keys in by_genre.items():
            if len(keys) >= minimum:
                wanted[GENRE_PREFIX + genre] = (keys, f"Genre {genre}")

    if SettingsService.get_bool("plex_mv_collections_decade", True):
        by_decade: Dict[int, List[str]] = defaultdict(list)
        for video_id in state.videos:
            year = state.year_for(video_id)
            if video_id in key_for and year:
                by_decade[year // 10 * 10].append(key_for[video_id])
        for decade, keys in by_decade.items():
            wanted[f"{DECADE_PREFIX}{decade}s"] = (keys, f"Decade {decade}s")

    if SettingsService.get_bool("plex_mv_collections_artist", True):
        minimum = SettingsService.get_int("plex_mv_artist_collection_min", 2)
        by_artist: Dict[int, List[str]] = defaultdict(list)
        names: Dict[int, Tuple[str, str]] = {}
        for video_id, info in state.videos.items():
            if video_id in key_for:
                by_artist[info["artist_id"]].append(key_for[video_id])
                names[info["artist_id"]] = (info["artist"], info["artist_sort"])
        for artist_id, keys in by_artist.items():
            name, sort = names[artist_id]
            if len(keys) >= minimum:
                display = split_featured(name)[0] or name
                wanted[ARTIST_PREFIX + display] = (keys, f"Artist {sort}")
    return wanted


def sync_plex_library(
    scan: bool = False,
    scan_wait_seconds: int = 60,
    client: Optional[PlexMusicVideoClient] = None,
    dry_run: bool = False,
) -> Dict:
    """
    Run one full sync. Returns counts for logs and the API.

    With dry_run, Plex is only read: the stats say what would change, with
    per-collection counts and a sample of title edits, and nothing is written.
    """
    client = client or PlexMusicVideoClient.from_settings()
    client.dry_run = dry_run
    stats = {
        "dry_run": dry_run,
        "items_in_plex": 0,
        "videos_downloaded": 0,
        "matched": 0,
        "unmatched": 0,
        "edited": 0,
        "collections_created": 0,
        "collections_updated": 0,
        "collections_deleted": 0,
        "playlists": {},
        "errors": [],
    }
    if dry_run:
        stats["collection_changes"] = {}
        stats["edit_samples"] = []
    if scan and not dry_run:
        for location in client.section_locations():
            try:
                client._request(
                    f"/library/sections/{client.section_id()}/refresh",
                    params={"path": location},
                )
            except PlexError as exc:
                stats["errors"].append(str(exc))
        wait_for_scan(scan_wait_seconds)

    state = LibraryState.load()
    stats["videos_downloaded"] = len(state.videos)
    by_file = {_nfc(path): item for path, item in client.items_by_file().items()}
    stats["items_in_plex"] = len({i.get("ratingKey") for i in by_file.values()})

    key_for: Dict[int, str] = {}
    for video_id, info in state.videos.items():
        item = by_file.get(info["path"])
        if not item:
            stats["unmatched"] += 1
            continue
        stats["matched"] += 1
        rating_key = str(item["ratingKey"])
        key_for[video_id] = rating_key
        try:
            changes = _edit_if_needed(client, state, video_id, item)
            if changes:
                stats["edited"] += 1
                if dry_run and len(stats["edit_samples"]) < 25:
                    stats["edit_samples"].append(
                        {
                            "file": info["path"],
                            "title": [item.get("title"), changes.get("title")],
                            "year": [item.get("year"), changes.get("year")],
                            "genres": changes.get("genres"),
                        }
                    )
        except PlexError as exc:
            stats["errors"].append(f"edit {rating_key}: {exc}")

    existing = client.collections()
    wanted = desired_collections(state, key_for)
    mode = COLLECTION_MODES.get(
        (SettingsService.get("plex_mv_collection_mode", "hide") or "hide").lower(),
        COLLECTION_MODE_HIDE,
    )
    for title, (keys, sort_title) in sorted(wanted.items()):
        try:
            result = client.ensure_collection(
                title,
                keys,
                sort_title=sort_title,
                mode=mode,
                exclusive=True,
                existing=existing,
            )
            if result["created"]:
                stats["collections_created"] += 1
            elif result["added"] or result["removed"]:
                stats["collections_updated"] += 1
            if dry_run and (result["created"] or result["added"] or result["removed"]):
                stats["collection_changes"][title] = result
        except PlexError as exc:
            stats["errors"].append(f"collection {title}: {exc}")

    for title, collection in list(existing.items()):
        if title and title.startswith(MANAGED_PREFIXES) and title not in wanted:
            try:
                client._request(
                    f"/library/metadata/{collection['ratingKey']}", method="DELETE"
                )
                stats["collections_deleted"] += 1
                if dry_run:
                    stats["collection_changes"][title] = "delete"
            except PlexError as exc:
                stats["errors"].append(f"delete collection {title}: {exc}")

    playlists = client.playlists()
    for media_list in state.lists:
        title = media_list["playlist_title"]
        if not title:
            continue
        ordered = [key_for[v] for _, v in sorted(media_list["ranked"]) if v in key_for]
        try:
            stats["playlists"][title] = client.ensure_playlist(
                title, ordered, existing=playlists
            )
        except PlexError as exc:
            stats["errors"].append(f"playlist {title}: {exc}")

    if dry_run:
        stats["planned_writes"] = len(client.planned)
    logger.info(
        ("Plex sync (dry run): " if dry_run else "Plex sync: ")
        + "{matched} of {videos_downloaded} downloaded videos in Plex, "
        "{edited} edited, {collections_created} collections created, "
        "{collections_updated} updated, {collections_deleted} deleted".format(**stats)
    )
    return stats


def _edit_if_needed(
    client: PlexMusicVideoClient, state: LibraryState, video_id: int, item: Dict
) -> Dict:
    info = state.videos[video_id]
    title = plex_title(info["artist"], info["title"])
    sort = f"{info['artist_sort']} {a_side(clean_video_title(info['title'], info['artist']))}"
    year = state.year_for(video_id)
    summary = state.summary_for(video_id)
    genres = state.genres_for(video_id)
    current_genres = [g.get("tag") for g in item.get("Genre") or [] if g.get("tag")]

    changes = {}
    if item.get("title") != title:
        changes["title"] = title
    if (item.get("titleSort") or item.get("title")) != sort:
        changes["sort_title"] = sort
    if year and item.get("year") != year:
        changes["year"] = year
    if (item.get("summary") or "") != summary:
        changes["summary"] = summary
    if set(current_genres) != set(genres):
        changes["genres"] = genres
        changes["current_genres"] = current_genres
    if not changes:
        return {}
    client.edit_item(str(item["ratingKey"]), **changes)
    return changes
