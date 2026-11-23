"""SABnzbd and qBittorrent clients, reduced to what a single-file grab needs."""

import re
from dataclasses import dataclass
from typing import Dict, Optional

import requests

from src.utils.logger import get_logger

logger = get_logger("mvidarr.indexers.clients")


class DownloadClientError(RuntimeError):
    pass


@dataclass
class ClientStatus:
    state: str  # queued / downloading / completed / failed / missing
    progress: float = 0.0
    path: Optional[str] = None
    message: Optional[str] = None


class SabnzbdClient:
    name = "sabnzbd"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        category: str = "musicvideos",
        http: Optional[requests.Session] = None,
        timeout: int = 60,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.category = category or ""
        self.http = http or requests.Session()
        self.timeout = timeout

    def _api(self, params: Dict, files=None, method: str = "GET") -> Dict:
        if not self.base_url or not self.api_key:
            raise DownloadClientError("SABnzbd URL and API key are required")
        query = {"apikey": self.api_key, "output": "json", **params}
        if files:
            resp = self.http.post(
                self.base_url + "/api", params=query, files=files, timeout=self.timeout
            )
        else:
            resp = self.http.get(
                self.base_url + "/api", params=query, timeout=self.timeout
            )
        if resp.status_code >= 400:
            raise DownloadClientError(f"SABnzbd HTTP {resp.status_code}")
        data = resp.json()
        if isinstance(data, dict) and data.get("status") is False:
            raise DownloadClientError(f"SABnzbd: {data.get('error')}")
        return data

    def test(self) -> Dict:
        version = self._api({"mode": "version"}).get("version")
        categories = self._api({"mode": "get_cats"}).get("categories") or []
        return {
            "version": version,
            "category": self.category,
            "category_exists": self.category in categories,
        }

    def add_nzb(self, nzb: bytes, name: str) -> str:
        filename = re.sub(r"[^\w.\- ]", "_", name)[:200] + ".nzb"
        data = self._api(
            {"mode": "addfile", "cat": self.category, "nzbname": name},
            files={"nzbfile": (filename, nzb, "application/x-nzb")},
        )
        ids = data.get("nzo_ids") or []
        if not ids:
            raise DownloadClientError("SABnzbd accepted the NZB but returned no id")
        logger.info(f"SABnzbd queued {name} as {ids[0]}")
        return ids[0]

    def status(self, nzo_id: str) -> ClientStatus:
        queue = self._api({"mode": "queue", "nzo_ids": nzo_id}).get("queue") or {}
        for slot in queue.get("slots") or []:
            if slot.get("nzo_id") == nzo_id:
                try:
                    progress = float(slot.get("percentage") or 0) / 100.0
                except ValueError:
                    progress = 0.0
                state = (
                    "queued"
                    if slot.get("status") in ("Queued", "Paused")
                    else "downloading"
                )
                return ClientStatus(state=state, progress=progress)
        history = self._api({"mode": "history", "nzo_ids": nzo_id}).get("history") or {}
        for slot in history.get("slots") or []:
            if slot.get("nzo_id") != nzo_id:
                continue
            status = slot.get("status")
            if status == "Completed":
                return ClientStatus(
                    state="completed", progress=1.0, path=slot.get("storage")
                )
            if status == "Failed":
                return ClientStatus(state="failed", message=slot.get("fail_message"))
            # Verifying / Repairing / Extracting / Moving / Running
            return ClientStatus(state="downloading", progress=1.0)
        return ClientStatus(state="missing")

    def remove(self, nzo_id: str, delete_files: bool = True) -> None:
        self._api(
            {
                "mode": "history",
                "name": "delete",
                "value": nzo_id,
                "del_files": int(delete_files),
            }
        )


_DONE_STATES = {
    "uploading",
    "stalledUP",
    "pausedUP",
    "stoppedUP",
    "queuedUP",
    "forcedUP",
    "checkingUP",
}
_FAILED_STATES = {"error", "missingFiles"}


def magnet_hash(magnet: str) -> Optional[str]:
    match = re.search(r"xt=urn:btih:([A-Za-z0-9]+)", magnet or "")
    return match.group(1).lower() if match else None


class QbittorrentClient:
    name = "qbittorrent"

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        category: str = "musicvideos",
        http: Optional[requests.Session] = None,
        timeout: int = 60,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.category = category or ""
        self.http = http or requests.Session()
        self.timeout = timeout
        self._logged_in = False

    def _login(self) -> None:
        if self._logged_in:
            return
        if not self.base_url:
            raise DownloadClientError("qBittorrent URL is required")
        resp = self.http.post(
            self.base_url + "/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            headers={"Referer": self.base_url},
            timeout=self.timeout,
        )
        if resp.status_code >= 400 or resp.text.strip() not in ("Ok.", ""):
            raise DownloadClientError("qBittorrent login failed")
        self._logged_in = True

    def _call(self, method: str, path: str, **kwargs) -> requests.Response:
        self._login()
        resp = self.http.request(
            method,
            self.base_url + path,
            headers={"Referer": self.base_url},
            timeout=self.timeout,
            **kwargs,
        )
        if resp.status_code == 403:
            self._logged_in = False
            self._login()
            resp = self.http.request(
                method,
                self.base_url + path,
                headers={"Referer": self.base_url},
                timeout=self.timeout,
                **kwargs,
            )
        if resp.status_code >= 400:
            raise DownloadClientError(f"qBittorrent {path} -> HTTP {resp.status_code}")
        return resp

    def test(self) -> Dict:
        version = self._call("GET", "/api/v2/app/version").text.strip()
        categories = self._call("GET", "/api/v2/torrents/categories").json()
        return {
            "version": version,
            "category": self.category,
            "category_exists": self.category in (categories or {}),
        }

    def add(
        self, tag: str, magnet: Optional[str] = None, torrent: Optional[bytes] = None
    ) -> Optional[str]:
        data = {"category": self.category, "tags": tag}
        if magnet:
            data["urls"] = magnet
            self._call("POST", "/api/v2/torrents/add", data=data)
            return magnet_hash(magnet)
        if torrent:
            self._call(
                "POST",
                "/api/v2/torrents/add",
                data=data,
                files={
                    "torrents": ("release.torrent", torrent, "application/x-bittorrent")
                },
            )
            return None
        raise DownloadClientError("Nothing to add: no magnet link or torrent file")

    def _find(self, tag: str) -> Optional[Dict]:
        torrents = self._call(
            "GET", "/api/v2/torrents/info", params={"tag": tag}
        ).json()
        return torrents[0] if torrents else None

    def status(self, tag: str) -> ClientStatus:
        torrent = self._find(tag)
        if not torrent:
            return ClientStatus(state="missing")
        state = torrent.get("state") or ""
        progress = float(torrent.get("progress") or 0)
        if state in _FAILED_STATES:
            return ClientStatus(
                state="failed", progress=progress, message=f"qBittorrent state {state}"
            )
        if progress >= 1.0 and (state in _DONE_STATES or state.endswith("UP")):
            return ClientStatus(
                state="completed", progress=1.0, path=torrent.get("content_path")
            )
        return ClientStatus(
            state=(
                "queued"
                if state.startswith(("queued", "stalled", "meta")) and progress == 0
                else "downloading"
            ),
            progress=progress,
            message=state,  # raw qBittorrent state, e.g. metaDL, stalledDL
        )

    def torrent_hash(self, tag: str) -> Optional[str]:
        torrent = self._find(tag)
        return torrent.get("hash") if torrent else None

    def remove(self, tag: str, delete_files: bool = False) -> None:
        torrent_hash = self.torrent_hash(tag)
        if torrent_hash:
            self._call(
                "POST",
                "/api/v2/torrents/delete",
                data={"hashes": torrent_hash, "deleteFiles": str(delete_files).lower()},
            )
