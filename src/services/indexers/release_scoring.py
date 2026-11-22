"""
Decide whether an indexer release is *this* music video, and how good it is.

Scene music video releases look like
    Madonna-Like_A_Prayer-DVDRip-x264-2005-MiMP
    Taylor_Swift-Anti-Hero-1080p-WEB-x264-2022-BTSFHD
and P2P ones like
    Queen - Bohemian Rhapsody (Official Video Remastered) [1080p]
A release is accepted only when the main artist and the core title appear as
whole words, nothing marks it as a live/compilation/audio release, and
little else is left once the artist, title and technical tags are removed.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

from src.services.indexers.prowlarr import Release
from src.utils.music_matching import core_title, main_artists, norm, squash

_TECH_TAGS = {
    "official",
    "music",
    "video",
    "videos",
    "mv",
    "mvid",
    "hd",
    "hq",
    "uhd",
    "sd",
    "4k",
    "2160p",
    "1440p",
    "1080p",
    "1080i",
    "720p",
    "576p",
    "576i",
    "480p",
    "480i",
    "360p",
    "web",
    "webrip",
    "webdl",
    "dl",
    "hdtv",
    "dvdrip",
    "dvd",
    "dvdscr",
    "bdrip",
    "brrip",
    "bluray",
    "blu",
    "ray",
    "vhsrip",
    "vhs",
    "tvrip",
    "satrip",
    "dsr",
    "pdtv",
    "x264",
    "x265",
    "h264",
    "h265",
    "264",
    "265",
    "hevc",
    "avc",
    "xvid",
    "divx",
    "mpeg2",
    "mpeg",
    "vp9",
    "av1",
    "aac",
    "ac3",
    "dts",
    "flac",
    "mp4",
    "mkv",
    "avi",
    "pal",
    "ntsc",
    "proper",
    "repack",
    "rerip",
    "internal",
    "int",
    "promo",
    "retail",
    "remastered",
    "remaster",
    "upscale",
    "upscaled",
    "restored",
    "uncensored",
    "explicit",
    "clean",
    "dirty",
    "version",
    "ft",
    "feat",
    "featuring",
    "and",
    "vs",
    "with",
    "x",
    "the",
    "a",
    "dirfix",
    "nfofix",
    "sample",
    "fixed",
    "vevo",
    "60fps",
    "50fps",
    "30fps",
    "25fps",
    "hdr",
    "sdr",
    "10bit",
    "8bit",
}

# Words that mean "not the single music video" unless the song title has them.
_REJECT_WORDS = [
    "live",
    "concert",
    "tour",
    "unplugged",
    "karaoke",
    "lyric",
    "lyrics",
    "audio",
    "mp3",
    "album",
    "discography",
    "videography",
    "collection",
    "compilation",
    "greatest hits",
    "best of",
    "anthology",
    "boxset",
    "box set",
    "documentary",
    "interview",
    "making of",
    "behind the scenes",
    "reaction",
    "cover",
    "tribute",
    "medley",
    "megamix",
    "mashup",
    "remix",
    "instrumental",
    "dvd5",
    "dvd9",
    "dvdr",
    "iso",
    "bdmv",
    "complete",
    "va",
    "various",
    "festival",
    "awards",
    "rehearsal",
    "acoustic",
    "session",
    "sessions",
    "reaction",
]

_RESOLUTIONS = [
    (2160, re.compile(r"\b(2160p|4k|uhd)\b")),
    (1440, re.compile(r"\b1440p\b")),
    (1080, re.compile(r"\b1080[pi]\b")),
    (720, re.compile(r"\b720p\b")),
    (576, re.compile(r"\b576[pi]\b|\bpal\b")),
    (480, re.compile(r"\b480[pi]\b|\bdvdrip\b|\bdvd\b|\bntsc\b")),
    (360, re.compile(r"\b360p\b|\bvhsrip\b|\bvhs\b")),
]

_RESOLUTION_POINTS = {2160: 45, 1440: 42, 1080: 40, 720: 30, 576: 22, 480: 20, 360: 8}
_MB = 1024 * 1024


@dataclass
class ScoredRelease:
    release: Release
    accepted: bool
    score: int = 0
    resolution: Optional[int] = None
    reasons: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "title": self.release.title,
            "indexer": self.release.indexer,
            "protocol": self.release.protocol,
            "size": self.release.size,
            "seeders": self.release.seeders,
            "accepted": self.accepted,
            "score": self.score,
            "resolution": self.resolution,
            "reasons": self.reasons,
        }


def release_words(title: str) -> str:
    """'Madonna-Like_A_Prayer-DVDRip-x264-2005-MiMP' -> 'madonna like a prayer dvdrip x264 2005 mimp'."""
    text = re.sub(r"[._]+", " ", title or "")
    return norm(text)


def detect_resolution(title: str) -> Optional[int]:
    words = release_words(title)
    for height, pattern in _RESOLUTIONS:
        if pattern.search(words):
            return height
    return None


def _contains_words(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def _group_token(title: str) -> str:
    """Scene group: the text after the last '-' (e.g. 'MiMP')."""
    if "-" not in (title or ""):
        return ""
    return norm(title.rsplit("-", 1)[1])


def score_release(
    release: Release,
    artist: str,
    title: str,
    min_resolution: int = 0,
    max_size_mb: int = 4096,
) -> ScoredRelease:
    words = release_words(release.title)
    result = ScoredRelease(release=release, accepted=False)
    song_core = norm(core_title(title))
    song_words = f" {norm(title)} {norm(artist)} "

    artists = [
        norm(re.sub(r"^the\s+", "", a, flags=re.I)) for a in main_artists(artist)
    ]
    artist_hit = next((a for a in artists if _contains_words(words, a)), None)
    if not artist_hit and artists and squash(artists[0]) in words.replace(" ", ""):
        artist_hit = artists[0]
    if not artist_hit:
        result.reasons.append("artist not in release name")
        return result
    if not _contains_words(words, song_core):
        result.reasons.append("title not in release name")
        return result

    for bad in _REJECT_WORDS:
        if _contains_words(words, bad) and f" {bad} " not in song_words:
            result.reasons.append(f"'{bad}' release")
            return result

    # A phrase in brackets names a different source: "(Truth Or Dare 1991)",
    # "[MTV Unplugged]". One word is allowed for group/site tags like "[rarbg]".
    for segment in re.findall(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]", release.title or ""):
        extra_words = [
            w
            for w in norm(segment).split()
            if w not in _TECH_TAGS
            and not re.fullmatch(r"(19|20)\d\d", w)
            and f" {w} " not in song_words
        ]
        if len(extra_words) >= 2:
            result.reasons.append(f"bracketed extra: {' '.join(extra_words[:4])}")
            return result

    if release.protocol == "torrent" and not (release.seeders or 0) > 0:
        result.reasons.append("no seeders")
        return result
    size_mb = (release.size or 0) / _MB
    if release.size and size_mb < 5:
        result.reasons.append("too small")
        return result
    if size_mb > max_size_mb:
        result.reasons.append("too large for a single video")
        return result

    resolution = detect_resolution(release.title)
    result.resolution = resolution
    if min_resolution and resolution and resolution < min_resolution:
        result.reasons.append(f"{resolution}p is below the minimum")
        return result

    # What is left after removing artist, title, featured names, tags, years, group
    leftover = f" {words} "
    for name in artists:
        leftover = leftover.replace(f" {name} ", " ")
    leftover = leftover.replace(f" {song_core} ", " ")
    group = _group_token(release.title)
    extra = [
        w
        for w in leftover.split()
        if w not in _TECH_TAGS
        and not re.fullmatch(r"(19|20)\d\d", w)
        and w != group
        and f" {w} " not in song_words
    ]
    if len(extra) > 4:
        result.reasons.append(f"extra words: {' '.join(extra[:6])}")
        return result

    score = 100
    score += _RESOLUTION_POINTS.get(resolution or 0, 12)
    score -= 6 * len(extra)
    if release.protocol == "usenet":
        score += 25
    else:
        score += min(10, (release.seeders or 0))
    if size_mb > 1536 and (resolution or 0) < 2160:
        score -= 10
    result.score = score
    result.accepted = True
    return result


def rank_releases(
    releases: List[Release],
    artist: str,
    title: str,
    min_resolution: int = 0,
    max_size_mb: int = 4096,
    exclude_guids=(),
) -> List[ScoredRelease]:
    """All releases scored; accepted ones first, best first."""
    excluded = set(exclude_guids or ())
    scored = [
        score_release(r, artist, title, min_resolution, max_size_mb)
        for r in releases
        if r.guid not in excluded
    ]
    scored.sort(key=lambda s: (s.accepted, s.score), reverse=True)
    return scored
