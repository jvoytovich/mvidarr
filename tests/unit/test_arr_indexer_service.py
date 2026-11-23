"""Usenet-first flow: search, grab, poll, import, retry, YouTube fallback, upgrades."""

import os

import pytest

from src.database.models import (
    Artist,
    Download,
    IndexerGrab,
    IndexerSearch,
    Video,
    VideoStatus,
)
from src.services.indexers import indexer_service as svc
from src.services.indexers.download_clients import ClientStatus
from src.services.indexers.prowlarr import Release
from tests.unit._arr_db import arr_db, set_settings  # noqa: F401

MB = 1024 * 1024


class FakeFetch:
    status_code = 200
    content = b"<nzb/>"
    headers = {}


class FakeProwlarr:
    def __init__(self, releases):
        self.releases = releases
        self.queries = []

    def search(self, query, **kwargs):
        self.queries.append(query)
        return list(self.releases)

    def fetch(self, url):
        return FakeFetch()


class FakeSab:
    name = "sabnzbd"

    def __init__(self):
        self.added = []
        self.states = {}

    def add_nzb(self, nzb, name):
        nzo = f"SABnzbd_nzo_{len(self.added)}"
        self.added.append((nzo, name))
        self.states[nzo] = ClientStatus(state="downloading", progress=0.5)
        return nzo

    def status(self, nzo_id):
        return self.states[nzo_id]

    def remove(self, nzo_id, delete_files=True):
        pass


class FakeQbit:
    name = "qbittorrent"

    def __init__(self):
        self.states = {}

    def add(self, tag, magnet=None, torrent=None):
        self.states[tag] = ClientStatus(state="downloading")
        return None

    def status(self, tag):
        return self.states[tag]

    def remove(self, tag, delete_files=False):
        pass


def rel(title, protocol="usenet", size=90 * MB, seeders=None):
    return Release(
        title=title,
        guid="guid:" + title,
        indexer="NZBgeek",
        protocol=protocol,
        size=size,
        download_url="http://prowlarr/dl/" + title,
        seeders=seeders,
        magnet_url="magnet:?xt=urn:btih:ABC" if protocol == "torrent" else None,
    )


@pytest.fixture
def env(arr_db, tmp_path, monkeypatch):
    library = tmp_path / "library"
    set_settings(
        indexer_enabled=True,
        prowlarr_url="http://prowlarr:9696",
        prowlarr_api_key="k",
        sabnzbd_url="http://sab:8080",
        sabnzbd_api_key="k",
        qbittorrent_url="http://qbit:8080",
        music_videos_path=str(library),
        indexer_max_searches_per_day=50,
    )
    prowlarr = FakeProwlarr(
        [
            rel("Madonna-Vogue-1080p-WEB-x264-1990-iUF"),
            rel("Madonna-Vogue-DVDRip-x264-1990-MiMP"),
            rel("Madonna-Vogue_Live-DVDRip-x264-MiMP"),
        ]
    )
    sab, qbit = FakeSab(), FakeQbit()
    monkeypatch.setattr(svc.IndexerConfig, "prowlarr", lambda self: prowlarr)
    monkeypatch.setattr(svc.IndexerConfig, "sabnzbd", lambda self: sab)
    monkeypatch.setattr(svc.IndexerConfig, "qbittorrent", lambda self: qbit)
    monkeypatch.setattr(svc, "probe_height", lambda path: None)
    monkeypatch.setattr(svc, "_write_poster", lambda *a, **k: None)
    monkeypatch.setattr(svc, "notify_file_added", lambda path: None)

    session = arr_db()
    artist = Artist(name="Madonna")
    session.add(artist)
    session.flush()
    video = Video(artist_id=artist.id, title="Vogue", status=VideoStatus.DOWNLOADING)
    session.add(video)
    session.commit()
    video_id = video.id
    session.close()
    return {
        "db": arr_db,
        "video_id": video_id,
        "prowlarr": prowlarr,
        "sab": sab,
        "qbit": qbit,
        "library": library,
        "tmp": tmp_path,
    }


def finish(sab, nzo, folder, name="vogue.mkv"):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "wb") as handle:
        handle.write(b"\0" * 4096)
    with open(os.path.join(folder, "vogue-sample.mkv"), "wb") as handle:
        handle.write(b"\0" * 10)
    sab.states[nzo] = ClientStatus(state="completed", progress=1.0, path=str(folder))


def test_grab_poll_and_import(env):
    message = svc.try_indexer_first(env["video_id"])
    assert message == "Sent to sabnzbd: Madonna-Vogue-1080p-WEB-x264-1990-iUF"
    assert env["prowlarr"].queries == ["Madonna Vogue"]

    nzo = env["sab"].added[0][0]
    stats = svc.poll_grabs()
    assert stats["waiting"] == 1

    job = env["tmp"] / "complete" / "Madonna-Vogue-1080p"
    finish(env["sab"], nzo, job)
    stats = svc.poll_grabs()
    assert stats["imported"] == 1

    session = env["db"]()
    video = session.get(Video, env["video_id"])
    expected = env["library"] / "Madonna" / "Madonna - Vogue.mkv"
    assert video.status == VideoStatus.DOWNLOADED
    assert video.local_path == str(expected) and expected.exists()
    assert video.quality == "1080p"
    assert video.video_metadata["mvidarr_source"]["origin"] == "usenet"
    assert not job.exists()  # SAB job folder cleaned up
    grab = session.query(IndexerGrab).one()
    assert grab.status == "imported" and grab.imported_path == str(expected)
    assert session.query(Download).filter(Download.status == "completed").count() == 1
    assert session.query(IndexerSearch).one().outcome == "grabbed"
    session.close()


def test_failed_job_tries_next_release_then_falls_back_to_youtube(env):
    svc.try_indexer_first(env["video_id"])
    first = env["sab"].added[0][0]
    env["sab"].states[first] = ClientStatus(state="failed", message="Repair failed")
    svc.poll_grabs()
    assert (
        env["sab"].added[1][1] == "Madonna-Vogue-DVDRip-x264-1990-MiMP"
    )  # next best, live one never

    second = env["sab"].added[1][0]
    env["sab"].states[second] = ClientStatus(state="failed", message="Out of retention")
    svc.poll_grabs()

    session = env["db"]()
    video = session.get(Video, env["video_id"])
    assert video.status == VideoStatus.WANTED  # next download run uses YouTube
    ok, why = svc.should_search(session, video, svc.IndexerConfig())
    assert not ok and why == "searched recently"
    assert [g.status for g in session.query(IndexerGrab).order_by(IndexerGrab.id)] == [
        "failed",
        "failed",
    ]
    session.close()


def test_daily_budget_and_skipped_video_types(env):
    set_settings(indexer_max_searches_per_day=1)
    svc.search_video(env["video_id"])
    session = env["db"]()
    artist = session.query(Artist).one()
    other = Video(artist_id=artist.id, title="Frozen", status=VideoStatus.WANTED)
    live = Video(
        artist_id=artist.id,
        title="Frozen (Live)",
        status=VideoStatus.WANTED,
        video_type="live_performance",
    )
    session.add_all([other, live])
    session.commit()
    config = svc.IndexerConfig()
    assert svc.should_search(session, other, config) == (
        False,
        "daily search budget used",
    )
    set_settings(indexer_max_searches_per_day=10)
    assert svc.should_search(session, live, svc.IndexerConfig())[0] is False
    assert svc.should_search(session, other, svc.IndexerConfig()) == (True, "")
    session.close()


def test_torrent_only_release_goes_to_qbittorrent_and_is_hardlinked(env):
    env["prowlarr"].releases = [
        rel("Madonna.Vogue.1080p.WEB.x264", protocol="torrent", seeders=12)
    ]
    message = svc.try_indexer_first(env["video_id"])
    assert message.startswith("Sent to qbittorrent")
    tag = next(iter(env["qbit"].states))
    payload = env["tmp"] / "torrents" / "Madonna.Vogue.1080p.WEB.x264.mp4"
    os.makedirs(payload.parent)
    payload.write_bytes(b"\0" * 2048)
    env["qbit"].states[tag] = ClientStatus(
        state="completed", progress=1.0, path=str(payload)
    )
    assert svc.poll_grabs()["imported"] == 1
    assert payload.exists()  # still seeding
    dest = env["library"] / "Madonna" / "Madonna - Vogue.mp4"
    assert dest.exists() and os.stat(dest).st_ino == os.stat(payload).st_ino


def test_upgrade_replaces_a_low_resolution_youtube_copy(env):
    session = env["db"]()
    video = session.get(Video, env["video_id"])
    old = env["library"] / "Madonna" / "Madonna - Vogue.webm"
    os.makedirs(old.parent)
    old.write_bytes(b"old")
    (env["library"] / "Madonna" / "Madonna - Vogue.jpg").write_bytes(b"poster")
    (env["library"] / "Madonna" / "Madonna - Vogue.info.json").write_text("{}")
    video.status = VideoStatus.DOWNLOADED
    video.local_path = str(old)
    video.quality = "360p"
    session.commit()
    session.close()

    stats = svc.search_upgrades()
    assert stats == {"searched": 1, "grabbed": 1, "skipped": None}
    nzo = env["sab"].added[0][0]
    finish(env["sab"], nzo, env["tmp"] / "complete" / "up")
    svc.poll_grabs()

    folder = sorted(os.listdir(env["library"] / "Madonna"))
    assert folder == ["Madonna - Vogue.jpg", "Madonna - Vogue.mkv"]
    session = env["db"]()
    assert session.get(Video, env["video_id"]).quality == "1080p"
    session.close()
    assert (
        svc.search_upgrades()["searched"] == 0
    )  # imported from an indexer: not a candidate


def test_torrent_without_peers_is_dropped_after_a_few_hours(env):
    from datetime import datetime, timedelta

    env["prowlarr"].releases = [
        rel("Madonna.Vogue.1080p.WEB.x264", protocol="torrent", seeders=1),
        rel("Madonna.Vogue.720p.WEB.x264", protocol="torrent", seeders=1),
    ]
    svc.try_indexer_first(env["video_id"])
    tag = next(iter(env["qbit"].states))
    env["qbit"].states[tag] = ClientStatus(state="queued", progress=0, message="metaDL")
    assert svc.poll_grabs()["failed"] == 0  # too early to give up

    session = env["db"]()
    grab = session.query(IndexerGrab).one()
    grab.created_at = datetime.utcnow() - timedelta(hours=4)
    session.commit()
    session.close()
    assert svc.poll_grabs()["failed"] == 1

    session = env["db"]()
    grabs = session.query(IndexerGrab).order_by(IndexerGrab.id).all()
    assert grabs[0].status == "failed" and "no peers" in (grabs[0].error or "")
    assert grabs[1].release_title == "Madonna.Vogue.720p.WEB.x264"
    session.close()


def test_failed_release_is_not_grabbed_again_under_a_new_guid(env):
    """The Pirate Bay hands out a new guid on every search; match the title too."""
    searches = []

    def search(query, **kwargs):
        searches.append(query)
        n = len(searches)
        return [
            Release(
                title="Madonna - Vogue (1990) [1080p]",
                guid=f"https://tpb/description.php?id=1&s={n}",
                indexer="The Pirate Bay",
                protocol="torrent",
                size=90 * MB,
                seeders=1,
                magnet_url=f"magnet:?xt=urn:btih:AAA&tr=tracker{n}",
            ),
            Release(
                title="Madonna.Vogue.720p.WEB.x264",
                guid="https://knaben/2",
                indexer="Knaben",
                protocol="torrent",
                size=90 * MB,
                seeders=1,
                magnet_url="magnet:?xt=urn:btih:BBB",
            ),
        ]

    env["prowlarr"].search = search
    svc.try_indexer_first(env["video_id"])
    tag = next(iter(env["qbit"].states))
    env["qbit"].states[tag] = ClientStatus(state="failed", message="error")
    svc.poll_grabs()

    session = env["db"]()
    grabs = session.query(IndexerGrab).order_by(IndexerGrab.id).all()
    assert len(searches) == 2  # the retry really searched again
    assert [g.release_title for g in grabs] == [
        "Madonna - Vogue (1990) [1080p]",
        "Madonna.Vogue.720p.WEB.x264",
    ]
    session.close()


def test_release_key_ignores_case_and_punctuation():
    from src.services.indexers.release_scoring import release_key

    assert release_key("torrent", "Madonna - Vogue (1990) [1080p]") == release_key(
        "Torrent", "madonna.vogue.1990.1080p"
    )
    assert release_key("usenet", "Madonna-Vogue") != release_key(
        "torrent", "Madonna-Vogue"
    )


def test_budget_holds_when_workers_check_before_anyone_has_searched(env):
    """Parallel workers all pass should_search; only the budget's worth search."""
    set_settings(indexer_max_searches_per_day=2)
    session = env["db"]()
    artist = session.query(Artist).one()
    others = [
        Video(artist_id=artist.id, title=t, status=VideoStatus.WANTED)
        for t in ("Frozen", "Music", "Hung Up")
    ]
    session.add_all(others)
    session.commit()
    ids = [env["video_id"]] + [v.id for v in others]
    config = svc.IndexerConfig()
    assert all(
        svc.should_search(session, session.get(Video, i), config)[0] for i in ids
    )
    session.close()

    for video_id in ids:  # each worker searches after its own check
        svc.search_video(video_id, "wanted", config)

    assert len(env["prowlarr"].queries) == 2
    session = env["db"]()
    outcomes = [
        s.outcome for s in session.query(IndexerSearch).order_by(IndexerSearch.id)
    ]
    assert outcomes.count("budget") == 2
    assert svc.searches_today(session) == 2
    # A video turned away by the budget is not "searched recently"
    assert svc.should_search(session, session.get(Video, ids[-1]), config) == (
        False,
        "daily search budget used",
    )
    session.close()

    # A search started by hand still runs
    svc.search_video(ids[-1], "wanted", config, enforce_budget=False)
    assert len(env["prowlarr"].queries) == 3


def test_upgrade_run_stops_at_the_budget(env, monkeypatch):
    set_settings(indexer_max_searches_per_day=3, indexer_upgrades_per_day=10)
    monkeypatch.setattr(svc, "upgrade_candidates", lambda s, c, limit: [1, 2, 3, 4, 5])
    monkeypatch.setattr(svc, "search_video", lambda *a, **k: [])
    calls = []

    def fake_budget_left(config):
        calls.append(1)
        return len(calls) <= 2

    monkeypatch.setattr(svc, "budget_left", fake_budget_left)
    stats = svc.search_upgrades()
    assert stats["searched"] == 2 and stats["skipped"] == "daily search budget used"
