"""Prowlarr search client (one query fans out to every configured indexer)."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from src.utils.logger import get_logger

logger = get_logger("mvidarr.indexers.prowlarr")

# Newznab/Torznab categories: 3000 Audio, 3020 Audio/Video (music videos)
MUSIC_VIDEO_CATEGORIES = (3020,)


class ProwlarrError(RuntimeError):
    pass


class ProwlarrLimitError(ProwlarrError):
    """An indexer or Prowlarr itself reported a query/grab limit."""


@dataclass
class Release:
    title: str
    guid: str
    indexer: str
    protocol: str  # "usenet" or "torrent"
    size: int = 0
    download_url: Optional[str] = None
    magnet_url: Optional[str] = None
    info_url: Optional[str] = None
    seeders: Optional[int] = None
    indexer_id: Optional[int] = None
    publish_date: Optional[str] = None
    categories: List[int] = field(default_factory=list)

    @classmethod
    def from_api(cls, item: Dict) -> "Release":
        cats = []
        for cat in item.get("categories") or []:
            if isinstance(cat, dict) and cat.get("id") is not None:
                cats.append(int(cat["id"]))
        return cls(
            title=item.get("title") or "",
            guid=str(item.get("guid") or item.get("downloadUrl") or item.get("title")),
            indexer=item.get("indexer") or "",
            indexer_id=item.get("indexerId"),
            protocol=(item.get("protocol") or "").lower(),
            size=int(item.get("size") or 0),
            download_url=item.get("downloadUrl"),
            magnet_url=item.get("magnetUrl"),
            info_url=item.get("infoUrl"),
            seeders=item.get("seeders"),
            publish_date=item.get("publishDate"),
            categories=cats,
        )


class ProwlarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        http: Optional[requests.Session] = None,
        timeout: int = 90,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.http = http or requests.Session()
        self.timeout = timeout

    def _get(self, path: str, params=None):
        if not self.base_url or not self.api_key:
            raise ProwlarrError("Prowlarr URL and API key are required")
        resp = self.http.get(
            self.base_url + path,
            params=params,
            headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
            timeout=self.timeout,
        )
        if resp.status_code == 429:
            raise ProwlarrLimitError("Prowlarr returned 429 (rate limited)")
        if resp.status_code >= 400:
            raise ProwlarrError(f"Prowlarr GET {path} -> HTTP {resp.status_code}")
        return resp.json()

    def test(self) -> Dict:
        status = self._get("/api/v1/system/status")
        indexers = self._get("/api/v1/indexer")
        enabled = [i for i in indexers if i.get("enable")]
        return {
            "version": status.get("version"),
            "indexers": len(enabled),
            "usenet": sum(1 for i in enabled if i.get("protocol") == "usenet"),
            "torrent": sum(1 for i in enabled if i.get("protocol") == "torrent"),
        }

    def search(
        self,
        query: str,
        categories=MUSIC_VIDEO_CATEGORIES,
        indexer_ids: Optional[List[int]] = None,
    ) -> List[Release]:
        params = [("query", query), ("type", "search"), ("limit", 100)]
        params += [("categories", c) for c in categories]
        params += [("indexerIds", i) for i in indexer_ids or []]
        items = self._get("/api/v1/search", params=params)
        if not isinstance(items, list):
            raise ProwlarrError("Unexpected Prowlarr search response")
        releases = [Release.from_api(i) for i in items if i.get("title")]
        logger.info(f"Prowlarr search {query!r}: {len(releases)} releases")
        return releases

    def fetch(self, url: str) -> requests.Response:
        """Download an NZB/torrent through Prowlarr's proxy link (no redirects)."""
        resp = self.http.get(
            url,
            headers={"X-Api-Key": self.api_key},
            timeout=self.timeout,
            allow_redirects=False,
        )
        if resp.status_code == 429:
            raise ProwlarrLimitError("Indexer grab limit reached (HTTP 429)")
        if resp.status_code >= 400:
            raise ProwlarrError(f"Release download failed: HTTP {resp.status_code}")
        return resp
