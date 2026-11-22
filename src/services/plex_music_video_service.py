"""
Plex client for a movie-type "Music Videos" library (Personal Media agent).

MVidarr's original Plex integration (plex_service.py) only talks to Plex
*music* libraries. Music videos live in a separate movie-type library where
each file is one item, so this client does the things that library needs:
targeted folder scans, title/year/genre edits, collections and ordered
playlists. Endpoints are the ones the homelab Billboard script has been
using against Plex since 2026-11.
"""

import os
import re
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

import requests

from src.services.settings_service import SettingsService
from src.utils.logger import get_logger

logger = get_logger("mvidarr.plex_music_videos")

# Plex collection modes (Collection.modeUpdate in python-plexapi)
COLLECTION_MODE_DEFAULT = -1
COLLECTION_MODE_HIDE = 0
COLLECTION_MODE_HIDE_ITEMS = 1
COLLECTION_MODE_SHOW_ITEMS = 2

# Keep URLs well under Plex's request-line limit when sending many keys.
_URI_CHUNK = 100


class PlexError(RuntimeError):
    pass


def parse_path_mappings(raw: Optional[str]) -> List[Tuple[str, str]]:
    """
    'local=plex' pairs separated by newlines or ';'. Longest local prefix wins.
    Example: '/media/0 Music Videos=/mnt/blofeld/music-videos'
    """
    pairs = []
    for chunk in re.split(r"[;\n]", raw or ""):
        if "=" not in chunk:
            continue
        local, remote = chunk.split("=", 1)
        local, remote = local.strip().rstrip("/"), remote.strip().rstrip("/")
        if local and remote:
            pairs.append((local, remote))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def map_path(path: str, mappings: Iterable[Tuple[str, str]]) -> str:
    """Translate a path with the first matching (from, to) prefix pair."""
    if not path:
        return path
    for src, dst in mappings:
        if path == src or path.startswith(src + "/"):
            return dst + path[len(src) :]
    return path


def reverse_mappings(mappings: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
    pairs = [(b, a) for a, b in mappings]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


class PlexMusicVideoClient:
    """Thin JSON client for one Plex movie-type music video section."""

    def __init__(
        self,
        base_url: str,
        token: str,
        section_id: Optional[str] = None,
        section_name: str = "Music Videos",
        path_mappings: Optional[List[Tuple[str, str]]] = None,
        http: Optional[requests.Session] = None,
        timeout: int = 120,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self._section_id = str(section_id) if section_id else None
        self.section_name = section_name or "Music Videos"
        self.path_mappings = path_mappings or []
        self.http = http or requests.Session()
        self.timeout = timeout
        self._machine_id: Optional[str] = None

    # ------------------------------------------------------------ settings
    @classmethod
    def from_settings(cls) -> "PlexMusicVideoClient":
        return cls(
            base_url=SettingsService.get("plex_server_url", ""),
            token=SettingsService.get("plex_token", ""),
            section_id=SettingsService.get("plex_mv_section_id", ""),
            section_name=SettingsService.get("plex_mv_section_name", "Music Videos"),
            path_mappings=parse_path_mappings(
                SettingsService.get("plex_mv_path_mappings", "")
            ),
        )

    @staticmethod
    def enabled() -> bool:
        return (
            SettingsService.get_bool("plex_mv_enabled", False)
            and bool(SettingsService.get("plex_server_url", ""))
            and bool(SettingsService.get("plex_token", ""))
        )

    # ------------------------------------------------------------ http
    def _request(
        self,
        path: str,
        method: str = "GET",
        params: Optional[Dict] = None,
    ) -> Dict:
        if not self.base_url or not self.token:
            raise PlexError("Plex server URL and token are required")
        url = self.base_url + path
        headers = {"X-Plex-Token": self.token, "Accept": "application/json"}
        resp = self.http.request(
            method, url, params=params or {}, headers=headers, timeout=self.timeout
        )
        if resp.status_code >= 400:
            raise PlexError(f"Plex {method} {path} -> HTTP {resp.status_code}")
        body = resp.content or b""
        if body.strip().startswith(b"{"):
            return resp.json()
        return {}

    @staticmethod
    def _metadata(payload: Dict) -> List[Dict]:
        return (payload.get("MediaContainer") or {}).get("Metadata") or []

    def machine_id(self) -> str:
        if not self._machine_id:
            payload = self._request("/identity")
            self._machine_id = (payload.get("MediaContainer") or {}).get(
                "machineIdentifier"
            )
            if not self._machine_id:
                raise PlexError("Plex /identity returned no machineIdentifier")
        return self._machine_id

    def metadata_uri(self, rating_keys: Iterable[str]) -> str:
        keys = ",".join(str(k) for k in rating_keys)
        return (
            f"server://{self.machine_id()}/com.plexapp.plugins.library"
            f"/library/metadata/{keys}"
        )

    # ------------------------------------------------------------ section
    def sections(self) -> List[Dict]:
        payload = self._request("/library/sections")
        return (payload.get("MediaContainer") or {}).get("Directory") or []

    def section_id(self) -> str:
        if self._section_id:
            return self._section_id
        for section in self.sections():
            if (
                section.get("type") == "movie"
                and section.get("title", "").lower() == self.section_name.lower()
            ):
                self._section_id = str(section.get("key"))
                return self._section_id
        raise PlexError(
            f"No movie-type Plex library named {self.section_name!r}; "
            "set plex_mv_section_id"
        )

    def section_locations(self) -> List[str]:
        sid = self.section_id()
        for section in self.sections():
            if str(section.get("key")) == sid:
                return [loc.get("path") for loc in section.get("Location") or []]
        return []

    def test(self) -> Dict:
        """Connection check used by the settings page."""
        mid = self.machine_id()
        sid = self.section_id()
        total = self._request(
            f"/library/sections/{sid}/all",
            params={"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0},
        )
        size = (total.get("MediaContainer") or {}).get("totalSize")
        return {
            "machine_id": mid,
            "section_id": sid,
            "locations": self.section_locations(),
            "items": size,
        }

    # ------------------------------------------------------------ paths
    def to_plex_path(self, local_path: str) -> str:
        return map_path(local_path, self.path_mappings)

    def to_local_path(self, plex_path: str) -> str:
        return map_path(plex_path, reverse_mappings(self.path_mappings))

    def scan_path(self, local_path: str) -> str:
        """Partial scan of one folder (the file's folder if given a file)."""
        folder = local_path
        if os.path.splitext(local_path)[1]:
            folder = os.path.dirname(local_path)
        plex_path = self.to_plex_path(folder)
        self._request(
            f"/library/sections/{self.section_id()}/refresh",
            params={"path": plex_path},
        )
        logger.info(f"Plex scan requested for {plex_path}")
        return plex_path

    # ------------------------------------------------------------ items
    def items(self) -> List[Dict]:
        return self._metadata(
            self._request(
                f"/library/sections/{self.section_id()}/all", params={"type": 1}
            )
        )

    def items_by_file(self) -> Dict[str, Dict]:
        """{local file path: item}; Plex paths are mapped back to local ones."""
        reverse = reverse_mappings(self.path_mappings)
        by_file = {}
        for item in self.items():
            for media in item.get("Media") or []:
                for part in media.get("Part") or []:
                    path = part.get("file")
                    if path:
                        by_file[map_path(path, reverse)] = item
        return by_file

    def item(self, rating_key: str) -> Optional[Dict]:
        found = self._metadata(self._request(f"/library/metadata/{rating_key}"))
        return found[0] if found else None

    def edit_item(
        self,
        rating_key: str,
        title: Optional[str] = None,
        sort_title: Optional[str] = None,
        year: Optional[int] = None,
        summary: Optional[str] = None,
        genres: Optional[List[str]] = None,
        current_genres: Optional[List[str]] = None,
        lock: bool = True,
    ) -> None:
        """Set fields on one movie-type item; genres replace the current set."""
        locked = 1 if lock else 0
        params: Dict = {"type": 1, "id": rating_key}
        if title is not None:
            params.update({"title.value": title, "title.locked": locked})
        if sort_title is not None:
            params.update({"titleSort.value": sort_title, "titleSort.locked": locked})
        if year is not None:
            params.update({"year.value": int(year), "year.locked": locked})
        if summary is not None:
            params.update({"summary.value": summary, "summary.locked": locked})
        if genres is not None:
            for index, genre in enumerate(genres):
                params[f"genre[{index}].tag.tag"] = genre
            stale = [g for g in (current_genres or []) if g not in genres]
            if stale:
                params["genre[].tag.tag-"] = ",".join(quote(g) for g in stale)
            params["genre.locked"] = locked
        self._request(
            f"/library/sections/{self.section_id()}/all", method="PUT", params=params
        )

    # ------------------------------------------------------------ collections
    def collections(self) -> Dict[str, Dict]:
        payload = self._request(f"/library/sections/{self.section_id()}/collections")
        return {c.get("title"): c for c in self._metadata(payload)}

    def collection_members(self, collection_key: str) -> List[str]:
        payload = self._request(f"/library/collections/{collection_key}/children")
        return [str(m.get("ratingKey")) for m in self._metadata(payload)]

    def _create_collection(self, title: str, rating_keys: List[str]) -> str:
        payload = self._request(
            "/library/collections",
            method="POST",
            params={
                "type": 1,
                "title": title,
                "smart": 0,
                "sectionId": self.section_id(),
                "uri": self.metadata_uri(rating_keys[:_URI_CHUNK]),
            },
        )
        created = self._metadata(payload)
        if not created:
            raise PlexError(f"Plex did not return the new collection {title!r}")
        key = str(created[0]["ratingKey"])
        self._add_to_collection(key, rating_keys[_URI_CHUNK:])
        return key

    def _add_to_collection(self, collection_key: str, rating_keys: List[str]) -> None:
        for start in range(0, len(rating_keys), _URI_CHUNK):
            chunk = rating_keys[start : start + _URI_CHUNK]
            self._request(
                f"/library/collections/{collection_key}/items",
                method="PUT",
                params={"uri": self.metadata_uri(chunk)},
            )

    def _remove_from_collection(
        self, collection_key: str, rating_keys: Iterable[str]
    ) -> None:
        for rating_key in rating_keys:
            self._request(
                f"/library/collections/{collection_key}/children/{rating_key}",
                method="DELETE",
            )

    def set_collection_mode(self, collection_key: str, mode: int) -> None:
        self._request(
            f"/library/metadata/{collection_key}/prefs",
            method="PUT",
            params={"collectionMode": mode},
        )

    def set_collection_sort_title(self, collection_key: str, sort_title: str) -> None:
        self._request(
            f"/library/sections/{self.section_id()}/all",
            method="PUT",
            params={
                "type": 18,
                "id": collection_key,
                "titleSort.value": sort_title,
                "titleSort.locked": 1,
            },
        )

    def ensure_collection(
        self,
        title: str,
        rating_keys: List[str],
        sort_title: Optional[str] = None,
        mode: Optional[int] = COLLECTION_MODE_SHOW_ITEMS,
        exclusive: bool = True,
        existing: Optional[Dict[str, Dict]] = None,
    ) -> Dict:
        """
        Make a collection hold exactly rating_keys (exclusive) or at least
        them. Returns counts of what changed.
        """
        rating_keys = [str(k) for k in dict.fromkeys(rating_keys)]
        existing = existing if existing is not None else self.collections()
        found = existing.get(title)
        if not found:
            if not rating_keys:
                return {"created": False, "added": 0, "removed": 0}
            key = self._create_collection(title, rating_keys)
            if sort_title:
                self.set_collection_sort_title(key, sort_title)
            if mode is not None:
                self.set_collection_mode(key, mode)
            existing[title] = {"ratingKey": key, "title": title}
            return {"created": True, "added": len(rating_keys), "removed": 0}

        key = str(found["ratingKey"])
        have = self.collection_members(key)
        have_set: Set[str] = set(have)
        add = [k for k in rating_keys if k not in have_set]
        remove = [k for k in have if k not in set(rating_keys)] if exclusive else []
        if add:
            self._add_to_collection(key, add)
        if remove:
            self._remove_from_collection(key, remove)
        if sort_title and found.get("titleSort") not in (None, sort_title):
            self.set_collection_sort_title(key, sort_title)
        if mode is not None and str(found.get("collectionMode", "")) != str(mode):
            self.set_collection_mode(key, mode)
        return {"created": False, "added": len(add), "removed": len(remove)}

    # ------------------------------------------------------------ playlists
    def playlists(self) -> Dict[str, Dict]:
        payload = self._request("/playlists", params={"playlistType": "video"})
        return {p.get("title"): p for p in self._metadata(payload)}

    def playlist_items(self, playlist_key: str) -> List[str]:
        payload = self._request(f"/playlists/{playlist_key}/items")
        return [str(m.get("ratingKey")) for m in self._metadata(payload)]

    def ensure_playlist(
        self,
        title: str,
        ordered_keys: List[str],
        existing: Optional[Dict[str, Dict]] = None,
    ) -> str:
        """Create or rebuild a playlist so it holds ordered_keys in that order."""
        ordered_keys = [str(k) for k in dict.fromkeys(ordered_keys)]
        existing = existing if existing is not None else self.playlists()
        found = existing.get(title)
        if not ordered_keys:
            return "empty"
        if found:
            key = str(found["ratingKey"])
            if self.playlist_items(key) == ordered_keys:
                return "unchanged"
            self._request(f"/playlists/{key}/items", method="DELETE")
            self._add_to_playlist(key, ordered_keys)
            return "rebuilt"
        payload = self._request(
            "/playlists",
            method="POST",
            params={
                "type": "video",
                "title": title,
                "smart": 0,
                "uri": self.metadata_uri(ordered_keys[:_URI_CHUNK]),
            },
        )
        created = self._metadata(payload)
        if created:
            key = str(created[0]["ratingKey"])
            self._add_to_playlist(key, ordered_keys[_URI_CHUNK:])
            existing[title] = {"ratingKey": key, "title": title}
        return "created"

    def _add_to_playlist(self, playlist_key: str, ordered_keys: List[str]) -> None:
        for start in range(0, len(ordered_keys), _URI_CHUNK):
            chunk = ordered_keys[start : start + _URI_CHUNK]
            self._request(
                f"/playlists/{playlist_key}/items",
                method="PUT",
                params={"uri": self.metadata_uri(chunk)},
            )


def notify_file_added(local_path: str) -> None:
    """Ask Plex to scan the folder a new file landed in. Never raises."""
    try:
        if not local_path or not PlexMusicVideoClient.enabled():
            return
        PlexMusicVideoClient.from_settings().scan_path(local_path)
    except Exception as exc:  # noqa: BLE001 - a failed scan must not fail a download
        logger.warning(f"Plex scan request failed for {local_path}: {exc}")


def wait_for_scan(seconds: int) -> None:
    """Plex scans asynchronously; the sync task waits a little after a scan."""
    if seconds > 0:
        time.sleep(seconds)
