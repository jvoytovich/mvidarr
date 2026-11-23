"""
Chart lists (Billboard Year-End Hot 100, 1956 onward) as MVidarr lists.

Each chart year is a MediaList with ranked entries. Entries are linked to
videos by main artist + core title; a song with no video yet becomes a
WANTED video under an unmonitored artist, so the normal download pipeline
(Usenet first, then YouTube) fills it. The Plex sync turns each list into a
collection ("Billboard Year-End 1984") and a chart-order playlist
("Billboard Year-End Hot 100: 1984").

Wikipedia parsing is the homelab bb_lists.py parser: the first wikitable on
"Billboard Year-End Hot 100 singles of <year>" with a title and an artist
column, rowspans expanded, footnotes dropped. 1956-58 are top-50 pages.
"""

import html
import json
import re
import sqlite3
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional

import requests

from src.database.connection import get_db
from src.database.models import Artist, MediaList, MediaListEntry, Video, VideoStatus
from src.services.library_import_service import find_or_create_artist
from src.services.settings_service import SettingsService
from src.utils.logger import get_logger
from src.utils.music_matching import a_side, similarity, song_key, squash

logger = get_logger("mvidarr.media_lists")

BILLBOARD_SOURCE = "billboard_year_end"
BILLBOARD_FIRST_YEAR = 1956
WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "MVidarr/1.x (self-hosted music video manager; Billboard Year-End lists)"


# ---------------------------------------------------------------- Wikipedia
class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables: List[List[List[Dict]]] = []
        self.depth = 0
        self.row = None
        self.cell = None
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.depth += 1
            if self.depth == 1 and "wikitable" in (attrs.get("class") or ""):
                self.tables.append([])
        if self.depth != 1 or not self.tables:
            return
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            span = re.sub(r"\D", "", attrs.get("rowspan", "1") or "1")
            self.cell = {"text": "", "rowspan": int(span or 1)}
        elif tag in ("sup", "style"):
            self.skip += 1
        elif tag == "br" and self.cell is not None:
            self.cell["text"] += " "

    def handle_endtag(self, tag):
        if tag in ("sup", "style") and self.skip:
            self.skip -= 1
        if tag == "table":
            self.depth -= 1
        if self.depth != 1 or not self.tables:
            return
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.tables[-1].append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None and not self.skip:
            self.cell["text"] += data


def _expand_rowspans(rows: List[List[Dict]]) -> List[List[str]]:
    out, pending = [], {}
    for row in rows:
        line, col, k = [], 0, 0
        while k < len(row) or col in pending:
            if col in pending:
                text, left = pending[col]
                line.append(text)
                if left <= 1:
                    del pending[col]
                else:
                    pending[col] = (text, left - 1)
            else:
                cell = row[k]
                k += 1
                text = re.sub(r"\s+", " ", html.unescape(cell["text"])).strip()
                line.append(text)
                if cell["rowspan"] > 1:
                    pending[col] = (text, cell["rowspan"] - 1)
            col += 1
        out.append(line)
    return out


def parse_year_end_html(page_html: str, year: int) -> List[Dict]:
    parser = _Tables()
    parser.feed(page_html)
    for table in parser.tables:
        rows = _expand_rowspans(table)
        if not rows:
            continue
        head = [c.lower() for c in rows[0]]
        if not any("title" in c for c in head) or not any("artist" in c for c in head):
            continue
        ti = next(i for i, c in enumerate(head) if "title" in c)
        ai = next(i for i, c in enumerate(head) if "artist" in c)
        songs = []
        for row in rows[1:]:
            if len(row) <= max(ti, ai):
                continue
            match = re.match(r"\d+", row[0])
            if not match:
                continue
            title = row[ti].strip().strip('"“”').strip()
            title = re.sub(r'"\s*/\s*"', " / ", title)
            songs.append(
                {
                    "year": year,
                    "rank": int(match.group()),
                    "title": title,
                    "artist": row[ai].strip(),
                }
            )
        if len(songs) >= 25:
            return songs
    return []


def fetch_billboard_year(
    year: int, http: Optional[requests.Session] = None
) -> Optional[List[Dict]]:
    """Entries for one year, [] if the page has no usable table, None if no page yet."""
    http = http or requests.Session()
    params = {
        "action": "parse",
        "page": f"Billboard Year-End Hot 100 singles of {year}",
        "prop": "text",
        "redirects": 1,
        "format": "json",
        "formatversion": 2,
    }
    data = None
    for attempt in range(6):
        resp = http.get(
            WIKI_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=60
        )
        if resp.status_code == 429 and attempt < 5:
            time.sleep(int(resp.headers.get("Retry-After") or 0) or 30 * (attempt + 1))
            continue
        resp.raise_for_status()
        data = resp.json()
        break
    if not data or "error" in data:
        return None
    return parse_year_end_html(data["parse"]["text"], year)


# ---------------------------------------------------------------- lists
def billboard_names(year: int) -> Dict[str, str]:
    """Names match the collections/playlists the homelab Billboard script made."""
    return {
        "name": f"Billboard Year-End Hot 100 {year}",
        "collection_title": f"Billboard Year-End {year}",
        "collection_sort_title": f"Billboard {year}",
        "playlist_title": f"Billboard Year-End Hot 100: {year}",
        "source_url": (
            "https://en.wikipedia.org/wiki/Billboard_Year-End_Hot_100_singles_of_"
            + str(year)
        ),
    }


def entry_key(artist: str, title: str) -> str:
    return song_key(artist, a_side(title))


def upsert_list(
    source: str, year: Optional[int], entries: List[Dict], names: Dict[str, str]
) -> int:
    """Create/update a list and replace its entries, keeping existing video links."""
    with get_db() as session:
        media_list = (
            session.query(MediaList).filter(MediaList.name == names["name"]).first()
        )
        if not media_list:
            media_list = MediaList(name=names["name"], source=source, year=year)
            session.add(media_list)
        for field in (
            "collection_title",
            "collection_sort_title",
            "playlist_title",
            "source_url",
        ):
            if names.get(field):
                setattr(media_list, field, names[field])
        session.flush()
        links = {
            e.match_key: e.video_id
            for e in session.query(MediaListEntry).filter(
                MediaListEntry.list_id == media_list.id
            )
            if e.video_id
        }
        session.query(MediaListEntry).filter(
            MediaListEntry.list_id == media_list.id
        ).delete()
        for item in sorted(entries, key=lambda e: e["rank"]):
            key = entry_key(item["artist"], item["title"])
            session.add(
                MediaListEntry(
                    list_id=media_list.id,
                    rank=int(item["rank"]),
                    artist=item["artist"][:500],
                    title=item["title"][:500],
                    match_key=key[:500],
                    video_id=links.get(key),
                )
            )
        media_list.last_refreshed = datetime.utcnow()
        session.commit()
        return media_list.id


def load_billboard_entries(entries: Iterable[Dict]) -> Dict:
    """Group flat {year, rank, title, artist} rows (lists.json format) into lists."""
    by_year: Dict[int, List[Dict]] = {}
    for item in entries:
        by_year.setdefault(int(item["year"]), []).append(item)
    for year, items in sorted(by_year.items()):
        upsert_list(BILLBOARD_SOURCE, year, items, billboard_names(year))
    return {"years": len(by_year), "entries": sum(len(v) for v in by_year.values())}


def refresh_billboard(
    years: Optional[List[int]] = None, pause_seconds: float = 4.0
) -> Dict:
    """Fetch missing years plus the last two (a new year appears in December)."""
    current = datetime.utcnow().year
    if years is None:
        with get_db() as session:
            have = {
                y
                for (y,) in session.query(MediaList.year).filter(
                    MediaList.source == BILLBOARD_SOURCE
                )
            }
        first = SettingsService.get_int("billboard_first_year", BILLBOARD_FIRST_YEAR)
        years = [
            y for y in range(first, current + 1) if y not in have or y >= current - 1
        ]
    stats = {"fetched": [], "no_page": [], "errors": []}
    http = requests.Session()
    for index, year in enumerate(sorted(years)):
        if index:
            time.sleep(pause_seconds)
        try:
            entries = fetch_billboard_year(year, http)
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"{year}: {exc}")
            continue
        if entries is None:
            stats["no_page"].append(year)
            continue
        if not entries:
            stats["errors"].append(f"{year}: no chart table found")
            continue
        upsert_list(BILLBOARD_SOURCE, year, entries, billboard_names(year))
        stats["fetched"].append(year)
    stats["linked"] = link_entries()
    return stats


def _video_index(session) -> Dict[str, List[Video]]:
    index: Dict[str, List[Video]] = {}
    for video, artist in session.query(Video, Artist).join(
        Artist, Video.artist_id == Artist.id
    ):
        index.setdefault(song_key(artist.name, video.title or ""), []).append(video)
    return index


def _pick(videos: List[Video]) -> Video:
    order = {
        VideoStatus.DOWNLOADED: 0,
        VideoStatus.DOWNLOADING: 1,
        VideoStatus.WANTED: 2,
    }
    return sorted(videos, key=lambda v: (order.get(v.status, 3), v.id))[0]


def _by_artist(index: Dict[str, List[Video]]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for key in index:
        grouped.setdefault(key.split("|", 1)[0], []).append(key)
    return grouped


def _fuzzy(
    index: Dict[str, List[Video]], by_artist: Dict[str, List[str]], key: str
) -> Optional[Video]:
    """Same main artist, nearly the same title (e.g. spelling of a parenthetical)."""
    artist_part, title_part = key.split("|", 1)
    best, best_ratio = None, 0.0
    for other in by_artist.get(artist_part, []):
        other_title = other.split("|", 1)[1]
        if not other_title:
            continue
        ratio = similarity(title_part, other_title)
        if ratio > best_ratio:
            best, best_ratio = index[other], ratio
    return _pick(best) if best and best_ratio >= 0.9 else None


def link_entries(list_ids: Optional[List[int]] = None) -> Dict:
    """Link unlinked entries to videos; create WANTED videos for the rest."""
    create = SettingsService.get_bool("lists_auto_add_wanted", True)
    stats = {"linked": 0, "created": 0, "unlinked": 0}
    with get_db() as session:
        query = (
            session.query(MediaListEntry, MediaList)
            .join(MediaList, MediaListEntry.list_id == MediaList.id)
            .filter(MediaListEntry.video_id.is_(None))
            .filter(MediaList.enabled.is_(True))
        )
        if list_ids:
            query = query.filter(MediaList.id.in_(list_ids))
        rows = query.order_by(MediaList.year, MediaListEntry.rank).all()
        if not rows:
            return stats
        index = _video_index(session)
        by_artist = _by_artist(index)
        artists: Dict[str, Artist] = {}
        for entry, media_list in rows:
            key = entry.match_key or entry_key(entry.artist, entry.title)
            videos = index.get(key)
            video = _pick(videos) if videos else _fuzzy(index, by_artist, key)
            if not video and create and media_list.auto_add_wanted:
                artist = find_or_create_artist(session, entry.artist, cache=artists)
                video = Video(
                    artist_id=artist.id,
                    title=a_side(entry.title),
                    status=VideoStatus.WANTED,
                    source=(
                        "billboard" if media_list.source == BILLBOARD_SOURCE else "list"
                    ),
                    year=media_list.year,
                    discovered_date=datetime.utcnow(),
                )
                session.add(video)
                session.flush()
                index.setdefault(key, []).append(video)
                by_artist.setdefault(key.split("|", 1)[0], []).append(key)
                stats["created"] += 1
            elif video:
                stats["linked"] += 1
            if video:
                entry.video_id = video.id
                # A chart year beats a later year (often a YouTube upload date)
                if media_list.year and (not video.year or video.year > media_list.year):
                    video.year = media_list.year
            else:
                stats["unlinked"] += 1
        session.commit()
    logger.info(f"List linking: {stats}")
    return stats


def list_summaries() -> List[Dict]:
    out = []
    with get_db() as session:
        for media_list in session.query(MediaList).order_by(
            MediaList.source, MediaList.year
        ):
            counts = {
                "entries": 0,
                "downloaded": 0,
                "wanted": 0,
                "downloading": 0,
                "failed": 0,
            }
            rows = (
                session.query(MediaListEntry, Video)
                .outerjoin(Video, MediaListEntry.video_id == Video.id)
                .filter(MediaListEntry.list_id == media_list.id)
                .all()
            )
            for _, video in rows:
                counts["entries"] += 1
                if video is None:
                    continue
                status = (
                    video.status.value
                    if hasattr(video.status, "value")
                    else str(video.status)
                ).lower()
                if status in counts:
                    counts[status] += 1
            out.append(
                {
                    "id": media_list.id,
                    "name": media_list.name,
                    "source": media_list.source,
                    "year": media_list.year,
                    "enabled": media_list.enabled,
                    "collection_title": media_list.collection_title,
                    "playlist_title": media_list.playlist_title,
                    "last_refreshed": (
                        media_list.last_refreshed.isoformat()
                        if media_list.last_refreshed
                        else None
                    ),
                    **counts,
                }
            )
    return out


def list_entries(list_id: int) -> Dict:
    with get_db() as session:
        media_list = session.query(MediaList).filter(MediaList.id == list_id).first()
        if not media_list:
            return {}
        rows = (
            session.query(MediaListEntry, Video)
            .outerjoin(Video, MediaListEntry.video_id == Video.id)
            .filter(MediaListEntry.list_id == list_id)
            .order_by(MediaListEntry.rank)
            .all()
        )
        return {
            "id": media_list.id,
            "name": media_list.name,
            "year": media_list.year,
            "entries": [
                {
                    "rank": entry.rank,
                    "artist": entry.artist,
                    "title": entry.title,
                    "video_id": entry.video_id,
                    "status": (
                        (
                            video.status.value
                            if hasattr(video.status, "value")
                            else str(video.status)
                        )
                        if video
                        else None
                    ),
                    "video_type": video.video_type if video else None,
                    "quality": video.quality if video else None,
                }
                for entry, video in rows
            ],
        }


# ---------------------------------------------------------------- handover
def _bb_key(artist: str, title: str) -> str:
    """Key the homelab Billboard script used: squashed artist | squashed A-side."""
    return squash(artist) + "|" + squash(a_side(title))


def import_billboard_state(
    state_db: str,
    lists_json: Optional[str] = None,
    path_from: str = "/mnt/media",
    path_to: str = "/media",
) -> Dict:
    """
    Take over from the homelab Billboard script (bb.py): load its lists,
    register its downloaded files in place, carry over matched YouTube ids,
    unofficial flags and rejected ids, then create WANTED videos for the rest.
    """
    stats = {"lists": None, "downloaded": 0, "matched": 0, "wanted": 0, "skipped": 0}
    if lists_json:
        with open(lists_json, "r", encoding="utf-8") as handle:
            stats["lists"] = load_billboard_entries(json.load(handle))

    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    songs = conn.execute("select * from songs").fetchall()
    conn.close()

    with get_db() as session:
        entries_by_bb: Dict[str, List[MediaListEntry]] = {}
        for entry in (
            session.query(MediaListEntry)
            .join(MediaList, MediaListEntry.list_id == MediaList.id)
            .filter(MediaList.source == BILLBOARD_SOURCE)
        ):
            entries_by_bb.setdefault(_bb_key(entry.artist, entry.title), []).append(
                entry
            )
        index = _video_index(session)
        taken_ids = {
            y
            for (y,) in session.query(Video.youtube_id).filter(
                Video.youtube_id.isnot(None)
            )
        }
        artists: Dict[str, Artist] = {}

        for song in songs:
            status = song["status"]
            if status not in ("downloaded", "matched", "none", "pending"):
                stats["skipped"] += 1
                continue
            title = a_side(song["title"])
            key = song_key(song["artist"], title)
            videos = index.get(key)
            video = _pick(videos) if videos else None
            if not video:
                artist = find_or_create_artist(session, song["artist"], cache=artists)
                video = Video(
                    artist_id=artist.id,
                    title=title,
                    status=VideoStatus.WANTED,
                    source="billboard",
                    year=song["first_year"],
                    discovered_date=datetime.utcnow(),
                )
                session.add(video)
                session.flush()
                index.setdefault(key, []).append(video)
            metadata = (
                dict(video.video_metadata or {})
                if isinstance(video.video_metadata, dict)
                else {}
            )
            rejected = re.findall(r"bad id (\S+):", song["note"] or "")
            if rejected:
                metadata["rejected_youtube_ids"] = sorted(
                    set(metadata.get("rejected_youtube_ids", []) + rejected)
                )
            if song["channel"]:
                metadata.setdefault("uploader", song["channel"])
            video.video_metadata = metadata or None
            if song["kind"]:
                video.video_type = (
                    "official_music_video"
                    if song["kind"] == "official"
                    else "unofficial_clip"
                )
            youtube_id = song["video_id"]
            if (
                youtube_id
                and status in ("downloaded", "matched")
                and youtube_id not in taken_ids
                and not video.youtube_id
            ):
                video.youtube_id = youtube_id
                video.youtube_url = f"https://www.youtube.com/watch?v={youtube_id}"
                video.url = video.youtube_url
                taken_ids.add(youtube_id)
            if song["duration"] and not video.duration:
                video.duration = int(song["duration"])
            if (
                status == "downloaded"
                and song["file"]
                and video.status != VideoStatus.DOWNLOADED
            ):
                path = song["file"]
                if path.startswith(path_from + "/"):
                    path = path_to + path[len(path_from) :]
                video.status = VideoStatus.DOWNLOADED
                video.local_path = path
                stats["downloaded"] += 1
            elif status == "matched":
                stats["matched"] += 1
            elif video.status != VideoStatus.DOWNLOADED:
                stats["wanted"] += 1
            for entry in entries_by_bb.get(_bb_key(song["artist"], song["title"]), []):
                entry.video_id = video.id
        session.commit()
    stats["linked"] = link_entries()
    logger.info(f"Billboard handover: {stats}")
    return stats
