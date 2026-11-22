"""Plex Music Videos library: client requests and the full sync against a fake Plex."""

import json
from urllib.parse import urlparse

import pytest

from src.database.models import Artist, MediaList, MediaListEntry, Video, VideoStatus
from src.services.plex_library_sync_service import broad_genres, sync_plex_library
from src.services.plex_music_video_service import (
    PlexMusicVideoClient,
    map_path,
    parse_path_mappings,
)
from tests.unit._arr_db import arr_db, set_settings  # noqa: F401


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        return json.loads(self.content)


class FakePlex:
    """Minimal in-memory Plex: one movie section, collections, playlists."""

    def __init__(self, files):
        self.calls = []
        self.items = {
            str(100 + i): {
                "ratingKey": str(100 + i),
                "title": f"file{i}",
                "Media": [{"Part": [{"file": f}]}],
            }
            for i, f in enumerate(files)
        }
        self.collections = {}
        self.playlists = {}
        self.next_key = 500

    def request(self, method, url, params=None, headers=None, timeout=None):
        path = urlparse(url).path
        params = dict(params or {})
        self.calls.append((method, path, params))
        mc = lambda **kw: FakeResponse({"MediaContainer": kw})  # noqa: E731
        if path == "/identity":
            return mc(machineIdentifier="abc")
        if path == "/library/sections":
            return mc(
                Directory=[
                    {
                        "key": "9",
                        "type": "movie",
                        "title": "Music Videos",
                        "Location": [{"path": "/mnt/blofeld/music-videos"}],
                    }
                ]
            )
        if path == "/library/sections/9/all" and method == "GET":
            return mc(Metadata=list(self.items.values()), totalSize=len(self.items))
        if path == "/library/sections/9/all" and method == "PUT":
            if params.get("type") == 1:
                item = self.items[str(params["id"])]
                if "title.value" in params:
                    item["title"] = params["title.value"]
                if "year.value" in params:
                    item["year"] = params["year.value"]
                if "summary.value" in params:
                    item["summary"] = params["summary.value"]
                if "titleSort.value" in params:
                    item["titleSort"] = params["titleSort.value"]
                genres = [
                    v
                    for k, v in params.items()
                    if k.startswith("genre[") and k.endswith("].tag.tag")
                ]
                if genres:
                    item["Genre"] = [{"tag": g} for g in genres]
            return FakeResponse()
        if path == "/library/sections/9/collections":
            return mc(
                Metadata=[
                    {"ratingKey": k, "title": c["title"]}
                    for k, c in self.collections.items()
                ]
            )
        if path == "/library/collections" and method == "POST":
            key = str(self.next_key)
            self.next_key += 1
            self.collections[key] = {
                "title": params["title"],
                "items": _uri_keys(params["uri"]),
            }
            return mc(Metadata=[{"ratingKey": key}])
        if (
            path.startswith("/library/collections/")
            and path.endswith("/children")
            and method == "GET"
        ):
            key = path.split("/")[3]
            return mc(
                Metadata=[{"ratingKey": k} for k in self.collections[key]["items"]]
            )
        if path.startswith("/library/collections/") and path.endswith("/items"):
            key = path.split("/")[3]
            self.collections[key]["items"] += _uri_keys(params["uri"])
            return FakeResponse()
        if (
            path.startswith("/library/collections/")
            and "/children/" in path
            and method == "DELETE"
        ):
            key, rk = path.split("/")[3], path.split("/")[5]
            self.collections[key]["items"].remove(rk)
            return FakeResponse()
        if path.startswith("/library/metadata/") and method == "DELETE":
            self.collections.pop(path.split("/")[3], None)
            return FakeResponse()
        if path.startswith("/library/metadata/") and path.endswith("/prefs"):
            return FakeResponse()
        if path == "/playlists" and method == "GET":
            return mc(
                Metadata=[
                    {"ratingKey": k, "title": p["title"]}
                    for k, p in self.playlists.items()
                ]
            )
        if path == "/playlists" and method == "POST":
            key = str(self.next_key)
            self.next_key += 1
            self.playlists[key] = {
                "title": params["title"],
                "items": _uri_keys(params["uri"]),
            }
            return mc(Metadata=[{"ratingKey": key}])
        if path.startswith("/playlists/") and path.endswith("/items"):
            key = path.split("/")[2]
            if method == "GET":
                return mc(
                    Metadata=[{"ratingKey": k} for k in self.playlists[key]["items"]]
                )
            if method == "DELETE":
                self.playlists[key]["items"] = []
            if method == "PUT":
                self.playlists[key]["items"] += _uri_keys(params["uri"])
            return FakeResponse()
        if path.endswith("/refresh"):
            return FakeResponse()
        raise AssertionError(f"unexpected {method} {path}")


def _uri_keys(uri):
    return uri.rsplit("/", 1)[1].split(",")


def client_for(fake):
    return PlexMusicVideoClient(
        "http://plex:32400",
        "token",
        section_id="9",
        path_mappings=parse_path_mappings(
            "/media/0 Music Videos=/mnt/blofeld/music-videos"
        ),
        http=fake,
    )


def test_path_mappings_longest_prefix_wins():
    maps = parse_path_mappings(
        "/media=/x;/media/0 Music Videos=/mnt/blofeld/music-videos"
    )
    assert (
        map_path("/media/0 Music Videos/A/A - B.mp4", maps)
        == "/mnt/blofeld/music-videos/A/A - B.mp4"
    )
    assert map_path("/media/other.mp4", maps) == "/x/other.mp4"
    assert map_path("/elsewhere/a.mp4", maps) == "/elsewhere/a.mp4"


def test_scan_requests_only_the_files_folder():
    fake = FakePlex([])
    client_for(fake).scan_path(
        "/media/0 Music Videos/MVidarr/Madonna/Madonna - Vogue.mp4"
    )
    method, path, params = fake.calls[-1]
    assert path == "/library/sections/9/refresh"
    assert params["path"] == "/mnt/blofeld/music-videos/MVidarr/Madonna"


def test_broad_genres():
    assert broad_genres(["dance pop", "synthpop", "rock"]) == [
        "Electronic & Dance",
        "Rock",
    ]
    assert broad_genres(["hip hop", "pop rap"]) == ["Hip-Hop"]
    assert broad_genres([]) == []


@pytest.fixture
def library(arr_db):
    session = arr_db()
    madonna = Artist(name="Madonna", genres=["dance pop", "pop"])
    prince = Artist(name="Prince", genres=["funk", "rock"])
    session.add_all([madonna, prince])
    session.flush()
    v1 = Video(
        artist_id=madonna.id,
        title="Like a Prayer",
        status=VideoStatus.DOWNLOADED,
        year=1989,
        local_path="/media/0 Music Videos/Billboard Year-End Hot 100/Madonna/Madonna - Like a Prayer.mp4",
    )
    v2 = Video(
        artist_id=madonna.id,
        title="Vogue (Official Video)",
        status=VideoStatus.DOWNLOADED,
        local_path="/media/0 Music Videos/MVidarr/Madonna/Madonna - Vogue.mp4",
    )
    v3 = Video(
        artist_id=prince.id,
        title="When Doves Cry",
        status=VideoStatus.DOWNLOADED,
        video_type="unofficial_clip",
        local_path="/media/0 Music Videos/MVidarr/Prince/Prince - When Doves Cry.mkv",
    )
    session.add_all([v1, v2, v3])
    session.flush()
    for year, ranks in ((1984, [(1, v3.id)]), (1990, [(2, v1.id), (1, v2.id)])):
        lst = MediaList(
            name=f"Billboard Year-End Hot 100 {year}",
            source="billboard_year_end",
            year=year,
            collection_title=f"Billboard Year-End {year}",
            collection_sort_title=f"Billboard {year}",
            playlist_title=f"Billboard Year-End Hot 100: {year}",
        )
        session.add(lst)
        session.flush()
        for rank, vid in ranks:
            session.add(
                MediaListEntry(
                    list_id=lst.id,
                    rank=rank,
                    artist="x",
                    title="y",
                    match_key="k",
                    video_id=vid,
                )
            )
    session.commit()
    ids = (v1.id, v2.id, v3.id)
    session.close()
    set_settings(plex_mv_genre_min_items=1, plex_mv_artist_collection_min=2)
    return ids


def test_full_sync_sets_metadata_collections_and_playlists(library):
    fake = FakePlex(
        [
            "/mnt/blofeld/music-videos/Billboard Year-End Hot 100/Madonna/Madonna - Like a Prayer.mp4",
            "/mnt/blofeld/music-videos/MVidarr/Madonna/Madonna - Vogue.mp4",
            "/mnt/blofeld/music-videos/MVidarr/Prince/Prince - When Doves Cry.mkv",
            "/mnt/blofeld/music-videos/somebody else.mp4",
        ]
    )
    stats = sync_plex_library(client=client_for(fake))
    assert stats["matched"] == 3 and stats["unmatched"] == 0
    assert stats["edited"] == 3

    like_a_prayer, vogue, doves = (
        fake.items["100"],
        fake.items["101"],
        fake.items["102"],
    )
    assert like_a_prayer["title"] == "Madonna - Like a Prayer"
    assert like_a_prayer["year"] == 1989  # own year wins over the 1990 chart year
    assert vogue["title"] == "Madonna - Vogue"
    assert vogue["year"] == 1990  # no year: first chart year
    assert vogue["summary"] == "Billboard Year-End Hot 100: #1 in 1990"
    assert "Flagged for review" in doves["summary"]
    assert {g["tag"] for g in doves["Genre"]} == {"Funk & Disco", "Rock"}

    titles = {c["title"]: c["items"] for c in fake.collections.values()}
    assert titles["Billboard Year-End 1990"] == ["101", "100"]  # rank order
    assert titles["Unofficial clips (review)"] == ["102"]
    assert set(titles["Decade: 1980s"]) == {"100", "102"}
    assert titles["Artist: Madonna"] == ["100", "101"]
    assert "Artist: Prince" not in titles  # below the 2-video minimum

    playlists = {p["title"]: p["items"] for p in fake.playlists.values()}
    assert playlists["Billboard Year-End Hot 100: 1990"] == ["101", "100"]

    # Second run changes nothing
    again = sync_plex_library(client=client_for(fake))
    assert again["edited"] == 0
    assert again["collections_created"] == 0 and again["collections_updated"] == 0
    assert again["playlists"]["Billboard Year-End Hot 100: 1990"] == "unchanged"


def test_managed_collections_are_pruned_and_others_left_alone(library):
    fake = FakePlex(["/mnt/blofeld/music-videos/MVidarr/Madonna/Madonna - Vogue.mp4"])
    fake.collections["900"] = {"title": "Artist: Gone", "items": ["100"]}
    fake.collections["901"] = {"title": "Jacob's favourites", "items": ["100"]}
    sync_plex_library(client=client_for(fake))
    titles = {c["title"] for c in fake.collections.values()}
    assert "Artist: Gone" not in titles
    assert "Jacob's favourites" in titles
