"""
Find the official music video for a song on YouTube.

YouTube Music search (ytmusicapi, anonymous) is tried first because it marks
label uploads as MUSIC_VIDEO_TYPE_OMV, the most reliable "this is the official
video" signal there is. Without it, a plain yt-dlp search is scored on the
channel name and title words instead. Audio, lyric, cover, reaction and
similar uploads are rejected unless the song title itself has the word. When
no official upload exists, the best fan upload or TV clip that names the
artist and the song is returned as kind 'unofficial', for review.

Rules ported from the homelab Billboard script (bb.py), where they matched
367 of the first ~400 Billboard Year-End songs (259 official).
"""

import re
import subprocess
from typing import Dict, Iterable, List, Optional

from src.utils.logger import get_logger
from src.utils.music_matching import (
    GOOD_RE,
    artist_matches,
    core_title,
    is_bad_upload,
    is_live_upload,
    main_artists,
    norm,
    split_featured,
    title_matches,
)

logger = get_logger("mvidarr.youtube_matcher")

MIN_SECONDS = 60
MAX_SECONDS = 15 * 60
OFFICIAL_SCORE = 60

_ytm = None


def _ytmusic():
    global _ytm
    if _ytm is None:
        import requests
        from ytmusicapi import YTMusic  # optional dependency

        class _TimeoutSession(requests.Session):
            def request(self, *args, **kwargs):
                kwargs.setdefault("timeout", 30)
                return super().request(*args, **kwargs)

        _ytm = YTMusic(requests_session=_TimeoutSession())
    return _ytm


def _seconds(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    parts = [int(p) for p in str(value).split(":") if p.isdigit()]
    return sum(v * 60**i for i, v in enumerate(reversed(parts)))


def _views_bonus(views) -> int:
    if isinstance(views, (int, float)):
        count = float(views)
    else:
        match = re.match(r"([\d.]+)\s*([KMB]?)", str(views or "").upper())
        if not match:
            return 0
        count = (
            float(match.group(1))
            * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[match.group(2)]
        )
    digits = len(str(int(count))) if count >= 1 else 0
    return min(15, max(0, (digits - 3) * 3))


def score_candidate(
    artist: str, title: str, cand: Dict, position: int
) -> Optional[int]:
    """
    cand: {title, duration, names (artist/channel names), omv (bool or None), views}
    Returns a score (>= 60 means official) or None when the upload is not usable.
    """
    cand_title = cand.get("title") or ""
    duration = _seconds(cand.get("duration"))
    if duration and not (MIN_SECONDS <= duration <= MAX_SECONDS):
        return None
    if not title_matches(title, cand_title) or is_bad_upload(title, cand_title):
        return None
    names = cand.get("names") or []
    if not artist_matches(artist, cand_title, names):
        return None
    live = is_live_upload(title, cand_title)
    omv = cand.get("omv")
    if omv is None:
        # Plain YouTube search: an artist/VEVO channel posting "Official Video"
        channel = norm(" ".join(names))
        own_channel = (
            any(
                norm(a) and norm(a).replace(" ", "") in channel.replace(" ", "")
                for a in main_artists(artist)
            )
            or "vevo" in channel
        )
        omv = own_channel and bool(GOOD_RE.search(cand_title))
    if omv and not live:
        return 100 - position
    score = 30 - position
    score -= 10 if live else 0
    score += 10 if GOOD_RE.search(cand_title) else 0
    score += _views_bonus(cand.get("views"))
    return min(score, OFFICIAL_SCORE - 1)


def _queries(artist: str, title: str) -> List[str]:
    full = re.sub(r"\s+(featuring|feat\.?|ft\.?)\s+", " ", artist, flags=re.I)
    queries = [f"{full} {core_title(title)}"]
    main = split_featured(artist)[0]
    first = main_artists(artist)[0]
    for name in (main, first):
        query = f"{name} {core_title(title)}"
        if query not in queries:
            queries.append(query)
    return queries


def _ytmusic_candidates(query: str) -> List[Dict]:
    results = _ytmusic().search(query, filter="videos", limit=20)[:20]
    out = []
    for item in results:
        if not item.get("videoId"):
            continue
        out.append(
            {
                "id": item["videoId"],
                "title": item.get("title") or "",
                "duration": item.get("duration_seconds") or item.get("duration"),
                "names": [a.get("name", "") for a in item.get("artists") or []],
                "omv": item.get("videoType") == "MUSIC_VIDEO_TYPE_OMV",
                "views": item.get("views"),
            }
        )
    return out


def _ytdlp_candidates(query: str, ytdlp: str, timeout: int = 90) -> List[Dict]:
    proc = subprocess.run(
        [
            ytdlp,
            "--flat-playlist",
            "--no-warnings",
            "--print",
            "%(id)s\t%(title)s\t%(duration)s\t%(channel)s\t%(view_count)s",
            f"ytsearch12:{query}",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or len(parts[0]) != 11:
            continue
        out.append(
            {
                "id": parts[0],
                "title": parts[1],
                "duration": (
                    float(parts[2])
                    if len(parts) > 2 and parts[2] not in ("", "NA")
                    else None
                ),
                "names": [parts[3]] if len(parts) > 3 and parts[3] != "NA" else [],
                "omv": None,
                "views": (
                    int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
                ),
            }
        )
    return out


def find_music_video(
    artist: str,
    title: str,
    exclude_ids: Iterable[str] = (),
    ytdlp: Optional[str] = None,
    timeout: int = 90,
) -> Optional[Dict]:
    """
    Returns {url, video_id, kind ('official'/'unofficial'), title, channel, score}
    or None when nothing usable was found.
    """
    excluded = set(exclude_ids or ())
    best = None
    for index, query in enumerate(_queries(artist, title)):
        try:
            candidates = _ytmusic_candidates(query)
        except Exception as exc:  # noqa: BLE001 - fall back to yt-dlp search
            logger.debug(
                f"YouTube Music search unavailable ({exc}); using yt-dlp search"
            )
            if not ytdlp:
                from src.services.video_batch_service import get_ytdlp_path

                ytdlp = get_ytdlp_path()
            try:
                candidates = _ytdlp_candidates(query, ytdlp, timeout=timeout)
            except (OSError, subprocess.TimeoutExpired) as err:
                logger.warning(f"yt-dlp search failed for {query!r}: {err}")
                candidates = []
        for position, cand in enumerate(candidates):
            if cand["id"] in excluded:
                continue
            score = score_candidate(artist, title, cand, position)
            if score is None:
                continue
            if not best or score > best["score"]:
                best = {
                    "url": f"https://www.youtube.com/watch?v={cand['id']}",
                    "video_id": cand["id"],
                    "kind": "official" if score >= OFFICIAL_SCORE else "unofficial",
                    "title": cand["title"],
                    "channel": ", ".join(cand.get("names") or []),
                    "score": score,
                }
        if best and best["kind"] == "official":
            break
    if best:
        logger.info(
            f"YouTube match for {artist} - {title}: {best['kind']} {best['video_id']} "
            f"({best['title']!r}, {best['channel']})"
        )
    return best
