"""In-place library import, Billboard list parsing/linking and the bb.py handover."""

import json
import os
import sqlite3

from src.database.models import Artist, MediaList, MediaListEntry, Video, VideoStatus
from src.services.library_import_service import import_directory
from src.services.media_list_service import (
    import_billboard_state,
    link_entries,
    load_billboard_entries,
    parse_year_end_html,
)
from tests.unit._arr_db import arr_db, set_settings  # noqa: F401

WIKI_HTML = (
    """
<table class="wikitable sortable">
<tr><th>No.</th><th>Title</th><th>Artist(s)</th></tr>
<tr><td>1</td><td>"When Doves Cry"</td><td>Prince</td></tr>
<tr><td rowspan="2">2</td><td>"What's Love Got to Do with It"<sup>[a]</sup></td><td rowspan="2">Tina Turner</td></tr>
<tr><td>"Better Be Good to Me"</td></tr>
<tr><td>4</td><td>"Say Say Say" / "Ode"</td><td>Paul McCartney and Michael Jackson</td></tr>
"""
    + "".join(
        f'<tr><td>{i}</td><td>"Song {i}"</td><td>Artist {i}</td></tr>'
        for i in range(5, 30)
    )
    + "</table>"
)


def touch(path, size=1024):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)


class TestLibraryImport:
    def test_imports_in_place_and_fills_wanted_videos(self, arr_db, tmp_path):
        root = tmp_path / "0 Music Videos"
        touch(
            str(
                root
                / "Billboard Year-End Hot 100"
                / "Madonna"
                / "Madonna - Like a Prayer.mp4"
            )
        )
        touch(str(root / "Madonna - Vogue.mkv"))
        touch(str(root / "Prince" / "When Doves Cry.mp4"))
        touch(str(root / "loose-file.mp4"))
        touch(str(root / ".staging" / "Hidden - Skip.mp4"))
        info = root / "Madonna - Vogue.info.json"
        info.write_text(
            json.dumps(
                {"id": "GuJQSAiODqI", "extractor_key": "Youtube", "duration": 290}
            )
        )

        session = arr_db()
        madonna = Artist(name="madonna", monitored=True)
        session.add(madonna)
        session.flush()
        wanted = Video(
            artist_id=madonna.id,
            title="Like a Prayer (Official Video)",
            status=VideoStatus.WANTED,
        )
        session.add(wanted)
        session.commit()
        wanted_id = wanted.id
        session.close()

        dry = import_directory(str(root), dry_run=True)
        assert (
            dry["files"] == 4 and dry["created"] == 2 and dry["linked_to_wanted"] == 1
        )

        stats = import_directory(str(root), dry_run=False)
        assert stats["linked_to_wanted"] == 1
        assert stats["created"] == 2
        assert stats["unparsed"] == [str(root / "loose-file.mp4")]

        session = arr_db()
        filled = session.get(Video, wanted_id)
        assert filled.status == VideoStatus.DOWNLOADED
        assert filled.local_path.endswith("Madonna - Like a Prayer.mp4")
        vogue = session.query(Video).filter(Video.title == "Vogue").one()
        assert vogue.youtube_id == "GuJQSAiODqI" and vogue.duration == 290
        assert vogue.artist_id == madonna.id  # exact case-insensitive artist match
        prince = session.query(Artist).filter(Artist.name == "Prince").one()
        assert prince.monitored is False and prince.auto_download is False
        session.close()

        again = import_directory(str(root), dry_run=False)
        assert again["already_known"] == 3 and again["created"] == 0


class TestBillboardLists:
    def test_wikipedia_table_parsing(self):
        songs = parse_year_end_html(WIKI_HTML, 1984)
        assert songs[0] == {
            "year": 1984,
            "rank": 1,
            "title": "When Doves Cry",
            "artist": "Prince",
        }
        assert songs[1]["title"] == "What's Love Got to Do with It"
        assert songs[2] == {
            "year": 1984,
            "rank": 2,
            "title": "Better Be Good to Me",
            "artist": "Tina Turner",
        }
        assert songs[3]["title"] == "Say Say Say / Ode"
        assert len(songs) == 29

    def test_link_entries_creates_wanted_videos_once(self, arr_db):
        set_settings(lists_auto_add_wanted=True)
        session = arr_db()
        prince = Artist(name="Prince")
        session.add(prince)
        session.flush()
        have = Video(
            artist_id=prince.id,
            title="When Doves Cry (Official Music Video)",
            status=VideoStatus.DOWNLOADED,
            year=2009,
        )
        session.add(have)
        session.commit()
        have_id = have.id
        session.close()

        load_billboard_entries(
            [
                {
                    "year": 1984,
                    "rank": 1,
                    "title": "When Doves Cry",
                    "artist": "Prince and The Revolution",
                },
                {
                    "year": 1984,
                    "rank": 4,
                    "title": "Say Say Say / Ode",
                    "artist": "Paul McCartney and Michael Jackson",
                },
                {
                    "year": 1985,
                    "rank": 50,
                    "title": "Say Say Say",
                    "artist": "Paul McCartney and Michael Jackson",
                },
            ]
        )
        stats = link_entries()
        assert stats == {"linked": 2, "created": 1, "unlinked": 0}

        session = arr_db()
        doves = session.get(Video, have_id)
        assert doves.year == 1984  # chart year beats a later (upload) year
        say = session.query(Video).filter(Video.title == "Say Say Say").one()
        assert say.status == VideoStatus.WANTED and say.source == "billboard"
        artist = session.get(Artist, say.artist_id)
        assert (
            artist.name == "Paul McCartney and Michael Jackson"
            and artist.monitored is False
        )
        lists = {l.year: l for l in session.query(MediaList)}
        assert lists[1984].collection_title == "Billboard Year-End 1984"
        assert lists[1984].playlist_title == "Billboard Year-End Hot 100: 1984"
        assert (
            session.query(MediaListEntry)
            .filter(MediaListEntry.video_id == say.id)
            .count()
            == 2
        )
        session.close()

        assert link_entries() == {"linked": 0, "created": 0, "unlinked": 0}

    def test_handover_from_billboard_script_state(self, arr_db, tmp_path):
        set_settings(lists_auto_add_wanted=True)
        lists_json = tmp_path / "lists.json"
        lists_json.write_text(
            json.dumps(
                [
                    {
                        "year": 1984,
                        "rank": 3,
                        "title": "Cum On Feel the Noize",
                        "artist": "Quiet Riot",
                    },
                    {
                        "year": 1984,
                        "rank": 1,
                        "title": "When Doves Cry",
                        "artist": "Prince",
                    },
                    {"year": 1984, "rank": 9, "title": "Jump", "artist": "Van Halen"},
                    {
                        "year": 1984,
                        "rank": 20,
                        "title": "Missing Song",
                        "artist": "Nobody",
                    },
                ]
            )
        )
        state = tmp_path / "state.db"
        conn = sqlite3.connect(state)
        conn.execute(
            """create table songs (key text primary key, artist text, title text, charts text,
            first_year int, status text, kind text, video_id text, yt_title text, channel text,
            duration int, file text, note text, tries int, updated real)"""
        )
        conn.executemany(
            "insert into songs values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "prince|whendovescry",
                    "Prince",
                    "When Doves Cry",
                    "[[1984, 1]]",
                    1984,
                    "downloaded",
                    "official",
                    "UG3VcCAlUgE",
                    "Prince - When Doves Cry",
                    "Prince",
                    230,
                    "/mnt/media/0 Music Videos/Billboard Year-End Hot 100/Prince/Prince - When Doves Cry.mp4",
                    None,
                    0,
                    0,
                ),
                (
                    "vanhalen|jump",
                    "Van Halen",
                    "Jump",
                    "[[1984, 9]]",
                    1984,
                    "downloaded",
                    "unofficial",
                    "abcdefghijk",
                    "Van Halen Jump TV",
                    "oldtv",
                    240,
                    "/mnt/media/0 Music Videos/Billboard Year-End Hot 100/Van Halen/Van Halen - Jump.mp4",
                    None,
                    0,
                    0,
                ),
                (
                    "quietriot|cumonfeelthenoize",
                    "Quiet Riot",
                    "Cum On Feel the Noize",
                    "[[1984, 3]]",
                    1984,
                    "pending",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "bad id ZxgMGk9JPVA: Sign in to confirm your age",
                    3,
                    0,
                ),
            ],
        )
        conn.commit()
        conn.close()

        stats = import_billboard_state(str(state), str(lists_json))
        assert stats["downloaded"] == 2

        session = arr_db()
        doves = session.query(Video).filter(Video.title == "When Doves Cry").one()
        assert doves.status == VideoStatus.DOWNLOADED
        assert (
            doves.local_path
            == "/media/0 Music Videos/Billboard Year-End Hot 100/Prince/Prince - When Doves Cry.mp4"
        )
        assert (
            doves.youtube_id == "UG3VcCAlUgE"
            and doves.video_type == "official_music_video"
        )
        jump = session.query(Video).filter(Video.title == "Jump").one()
        assert (
            jump.video_type == "unofficial_clip"
            and jump.video_metadata["uploader"] == "oldtv"
        )
        riot = session.query(Video).filter(Video.title == "Cum On Feel the Noize").one()
        assert riot.status == VideoStatus.WANTED
        assert riot.video_metadata["rejected_youtube_ids"] == ["ZxgMGk9JPVA"]
        missing = session.query(Video).filter(Video.title == "Missing Song").one()
        assert missing.status == VideoStatus.WANTED
        assert (
            session.query(MediaListEntry)
            .filter(MediaListEntry.video_id.is_(None))
            .count()
            == 0
        )
        session.close()


def test_nas_snapshot_and_recycle_folders_are_skipped(tmp_path):
    from src.services.library_import_service import scan_video_files

    for folder in ("@Recently-Snapshot", "@Recycle", "#recycle", "@eaDir", ".hidden"):
        touch(str(tmp_path / folder / "Madonna - Vogue.mp4"))
    touch(str(tmp_path / "Madonna" / "Madonna - Vogue.mp4"))
    assert scan_video_files(str(tmp_path)) == [
        str(tmp_path / "Madonna" / "Madonna - Vogue.mp4")
    ]
