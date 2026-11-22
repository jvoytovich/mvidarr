"""
Artist/title matching helpers shared by the YouTube matcher, the indexer
release scorer, the in-place library importer and the chart lists.

The rules here started life in the homelab Billboard script (bb.py) and were
tuned against the Billboard Year-End lists 1956-2025: squash both sides to
lowercase ASCII, compare the main artist and the "core" title (A-side, no
bracketed parts), and reject audio/lyric/cover/reaction-style uploads unless
the word is part of the song title itself.
"""

import difflib
import re
import unicodedata
from typing import Iterable, List, Optional, Tuple

# "featuring" credits. Not "&"/"and": "Simon & Garfunkel" is one act.
_FEAT_SPLIT = re.compile(
    r"\s+(?:featuring|feat\.?|ft\.?|duet with)\s+|\s*[\(\[](?:featuring|feat\.?|ft\.?)\s+",
    re.I,
)
# Everything that can join two credited artists, used only for matching.
_ARTIST_SPLIT = re.compile(
    r"\s+(?:featuring|feat\.?|ft\.?|with|and|&|x|vs\.?|duet with)\s+|,\s*|\s*/\s*",
    re.I,
)

# Words that mark an upload as something other than the music video.
BAD_RE = re.compile(
    r"\b(audio|lyrics?|visuali[sz]er|karaoke|reaction|cover|remix|instrumental|8d|"
    r"slowed|sped up|nightcore|1 hour|10 hours|tutorial|lesson|drum|guitar|bass|"
    r"piano|teaser|trailer|snippet|behind the scenes|making of|interview|medley|"
    r"mashup|tribute|re-?recorded|taylor's version|remaster(ed)?|demo|version|"
    r"legendado|tradu[cç][aã]o|traducida|subtitulad[ao]|letra|paroles|перевод)\b"
    r"|\s-\s\d{1,2}$",
    re.I,
)
GOOD_RE = re.compile(
    r"official (music |hd |4k )?video|original video|music video|promo|\(video\)|"
    r"top ?pop|soul train|midnight special|american bandstand|top of the pops|"
    r"ed sullivan",
    re.I,
)
LIVE_RE = re.compile(r"\blive\b|\bconcert\b|\btour\b", re.I)

# Decorations YouTube uploaders put on titles. Removed for file names and
# Plex titles, never for matching decisions on their own.
_DECORATIONS = re.compile(
    r"\s*[\(\[]\s*(?:official\s*)?(?:hd\s*|4k\s*|hq\s*|uhd\s*)?"
    r"(?:music\s*)?(?:video|audio|visuali[sz]er|lyric\s*video|lyrics?)"
    r"(?:\s*(?:hd|4k|hq|remastered|remaster))?\s*[\)\]]"
    r"|\s*[\(\[]\s*(?:hd|hq|4k|uhd|1080p|720p|explicit|clean|dirty|official)\s*[\)\]]"
    r"|\s*[\(\[]\s*(?:4k\s*)?remaster(?:ed)?(?:\s*\d{4})?(?:\s*in\s*4k|\s*hd)?\s*[\)\]]"
    r"|\s*[\(\[]\s*from\s+[^\)\]]*[\)\]]"
    r"|\s*\|\s*(?:vevo|official|.*video).*$"
    r"|\s+-\s+official\s+(?:music\s+)?video\s*$",
    re.I,
)


def norm(text: Optional[str]) -> str:
    """Lowercase ASCII words separated by single spaces; '&' becomes 'and'."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def squash(text: Optional[str]) -> str:
    """norm() without spaces, for exact key comparisons."""
    return norm(text).replace(" ", "")


def similarity(a: str, b: str) -> float:
    """Fuzzy ratio between two strings after normalising both."""
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def a_side(title: str) -> str:
    """'A / B' double-A-side chart titles -> 'A'."""
    return (title or "").split(" / ")[0].strip()


def strip_brackets(title: str) -> str:
    stripped = re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "").strip()
    return stripped or (title or "").strip()


def core_title(title: str) -> str:
    """The part of a song title worth matching on: A-side, no bracketed parts."""
    return strip_brackets(a_side(title))


def split_featured(artist: str) -> Tuple[str, List[str]]:
    """'Dua Lipa featuring DaBaby' -> ('Dua Lipa', ['DaBaby'])."""
    parts = [p.strip(" )]") for p in _FEAT_SPLIT.split(artist or "") if p.strip()]
    if not parts:
        return (artist or "").strip(), []
    return parts[0], parts[1:]


def main_artists(artist: str) -> List[str]:
    """Every credited name, for matching: 'A & B feat. C' -> ['A', 'B', 'C']."""
    parts = [p.strip(" ()[]") for p in _ARTIST_SPLIT.split(artist or "")]
    parts = [p for p in parts if p]
    return parts or [(artist or "").strip()]


def primary_artist(artist: str) -> str:
    return main_artists(artist)[0]


def _drop_the(name: str) -> str:
    return re.sub(r"^the\s+", "", name or "", flags=re.I)


def artist_key(artist: str) -> str:
    """Key for 'is this the same act': primary artist, no leading 'The'."""
    return squash(_drop_the(primary_artist(artist)))


def clean_video_title(title: str, artist: Optional[str] = None) -> str:
    """
    Turn an upload title into a song title for file names and Plex:
    'Madonna - Like A Prayer (Official Video) [HD]' -> 'Like A Prayer'.
    Featured credits in the title are dropped; bracketed version notes that
    are part of the song (e.g. '(Remix)') stay.
    """
    text = (title or "").strip()
    text = (
        text.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&").strip()
    )
    if artist:
        for name in {artist, split_featured(artist)[0], primary_artist(artist)}:
            if not name:
                continue
            prefix = re.compile(
                r"^\s*" + re.escape(name) + r"\s*[-–—:|]\s*", flags=re.I
            )
            if prefix.match(text):
                text = prefix.sub("", text, count=1)
                break
    previous = None
    while previous != text:
        previous = text
        text = _DECORATIONS.sub("", text).strip()
    text = re.sub(
        r"\s*[\(\[]?\s*\b(?:featuring|feat\.?|ft\.?)\s+[^\)\]]*[\)\]]?\s*$",
        "",
        text,
        flags=re.I,
    ).strip()
    text = _strip_wrapping_quotes(text.strip(" -–—|:"))
    return text or (title or "").strip()


def _strip_wrapping_quotes(text: str) -> str:
    """
    Drop quote marks around a title but keep apostrophes that belong to it
    ("Somebody's Knockin'", "'65 Love Affair").
    """
    text = re.sub(r'^["“”]+|["“”]+$', "", text).strip()
    if text.startswith("‘") and text.endswith("’"):
        text = text[1:-1].strip()
    return text.strip(" -–—|:")


def title_key(title: str, artist: Optional[str] = None) -> str:
    return squash(core_title(clean_video_title(title, artist)))


def song_key(artist: str, title: str) -> str:
    """Identity of a song across sources: primary artist + core title."""
    return f"{artist_key(artist)}|{title_key(title, artist)}"


def title_matches(song_title: str, candidate_title: str) -> bool:
    """Does an upload/release title name this song?"""
    want = norm(core_title(song_title))
    got = norm(candidate_title)
    if not want:
        return False
    if f" {want} " in f" {got} ":
        return True
    trimmed = norm(re.sub(r"\s*[\(\[].*?[\)\]]", "", candidate_title or ""))
    if " - " in (candidate_title or ""):
        trimmed = norm(strip_brackets(candidate_title.split(" - ")[-1]))
    return difflib.SequenceMatcher(None, want, trimmed).ratio() > 0.75


def artist_matches(
    song_artist: str, candidate_text: str, candidate_names: Iterable[str] = ()
) -> bool:
    """Does an upload (title + channel/artist names) credit one of the song's artists?"""
    names = [n for n in candidate_names if n]
    hay = f" {norm(' '.join(names) + ' ' + (candidate_text or ''))} "
    for name in main_artists(song_artist):
        wanted = norm(_drop_the(name))
        if not wanted:
            continue
        if f" {wanted} " in hay or squash(wanted) in hay.replace(" ", ""):
            return True
        if any(similarity(name, n) > 0.8 for n in names):
            return True
    return False


def is_bad_upload(song_title: str, candidate_title: str) -> bool:
    """Audio/lyric/cover/remix/etc. uploads, unless the word is in the song title."""
    text = re.sub(
        r"[\(\[]official (?:music |hd |4k )?video[\)\]]",
        "",
        candidate_title or "",
        flags=re.I,
    )
    match = BAD_RE.search(text)
    if not match:
        return False
    return not re.search(re.escape(match.group(0)), song_title or "", re.I)


def is_live_upload(song_title: str, candidate_title: str) -> bool:
    return bool(LIVE_RE.search(candidate_title or "")) and not LIVE_RE.search(
        song_title or ""
    )


def safe_filename(text: str, limit: int = 120) -> str:
    """Strip characters that are illegal on Windows/SMB/NFS shares."""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", text or "")
    # Leading dots would make the file or folder hidden (".38 Special")
    text = re.sub(r"\s+", " ", text).strip().strip(".").strip()
    return text[:limit].strip() or "Unknown"


def library_folder(artist: str) -> str:
    """Folder for an artist: featured credits dropped, so 'A feat. B' lives under 'A'."""
    return safe_filename(split_featured(artist)[0] or artist)


def library_stem(artist: str, title: str) -> str:
    """File name without extension: 'Artist - Title'."""
    clean = clean_video_title(title, artist)
    return safe_filename(f"{artist} - {a_side(clean)}", limit=180)


_TRACK_NUMBER = re.compile(r"^\s*\d{1,3}\s*[.)-]\s+")
_RELEASE_TAIL = re.compile(
    r"(?:[._](?:\d{3,4}[pi]|4k|uhd|[hx]\.?26[45]|hevc|avc|xvid|divx"
    r"|web(?:-?dl|rip)?|dvd(?:rip|5|9)?|blu-?ray|bdrip|brrip|hdtv|vhs(?:rip)?)\b"
    r"|\s(?:\d{3,4}p|[hx]\.?26[45]|hevc)\b).*$",
    flags=re.I,
)


def strip_release_tail(stem: str) -> str:
    """'Take On Me.540p.h264-rage' -> 'Take On Me'."""
    stem = stem or ""
    trimmed = _RELEASE_TAIL.sub("", stem)
    if trimmed == stem:
        return stem
    return trimmed.strip(" ._-") or stem


def strip_track_number(stem: str, require_separator: bool = True) -> str:
    """
    '37. Samantha Fox - Touch Me' -> 'Samantha Fox - Touch Me'. With
    require_separator, the number is only dropped when an 'Artist - Title'
    is left, so '311 - Amber' keeps its artist.
    """
    stripped = _TRACK_NUMBER.sub("", stem or "", count=1)
    if stripped == stem or not stripped:
        return stem
    if require_separator and not re.search(r"\s[-–—]\s", stripped):
        return stem
    return stripped


def parse_artist_title(stem: str) -> Tuple[Optional[str], Optional[str]]:
    """'Artist - Title' (also en/em dash) -> (artist, title); keeps punctuation."""
    stem = (stem or "").strip()
    match = re.match(r"^(.+?)\s+[-–—]\s+(.+)$", stem)
    if not match:
        return None, None
    artist, title = match.group(1).strip(), match.group(2).strip()
    if not artist or not title:
        return None, None
    return artist, title
