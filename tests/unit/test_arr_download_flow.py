"""Download thread: indexer handoff, YouTube cooldown, bot checks vs age gates, file names."""

from unittest.mock import MagicMock, patch

import pytest

from src.database.models import Artist, Setting, Video, VideoStatus
from src.services import youtube_cooldown
from src.services.unified_download_service import (
    AntiDetectionLevel,
    DownloadContext,
    DownloadResult,
    UnifiedDownloadService,
    YouTubeDownloadStrategy,
)
from tests.unit._arr_db import arr_db, set_settings  # noqa: F401


def context(video_id, **overrides):
    values = dict(
        video_id=video_id,
        url="https://www.youtube.com/watch?v=abc12345678",
        title="Vogue",
        artist="Madonna",
        quality="best",
        output_path="/tmp",
        anti_detection=AntiDetectionLevel.MODERATE,
    )
    values.update(overrides)
    return DownloadContext(**values)


@pytest.fixture
def video_id(arr_db):
    session = arr_db()
    artist = Artist(name="Madonna")
    session.add(artist)
    session.flush()
    video = Video(
        artist_id=artist.id,
        title="Vogue",
        status=VideoStatus.DOWNLOADING,
        url="https://www.youtube.com/watch?v=abc12345678",
    )
    session.add(video)
    session.commit()
    vid = video.id
    session.close()
    return vid


def run(service, ctx, strategy=None):
    results = []
    service.download_callbacks[1] = [lambda _id, result: results.append(result)]
    if strategy is not None:
        service.strategies = [strategy]
    with patch("src.services.unified_download_service.trigger_video_downloaded"), patch(
        "src.services.unified_download_service.trigger_video_download_failed"
    ), patch("src.services.plex_music_video_service.notify_file_added"):
        service._execute_download(1, ctx)
    return results


def fake_strategy(result):
    strategy = MagicMock()
    strategy.can_handle.return_value = True
    strategy.download.return_value = result
    return strategy


def status(arr_db, vid):
    session = arr_db()
    value = session.get(Video, vid).status
    session.close()
    return value


def test_indexer_handoff_skips_youtube(arr_db, video_id):
    strategy = fake_strategy(DownloadResult(success=True))
    with patch(
        "src.services.indexers.indexer_service.try_indexer_first",
        return_value="Sent to sabnzbd: Madonna-Vogue-1080p",
    ):
        results = run(
            UnifiedDownloadService(), context(video_id, indexer_first=True), strategy
        )
    strategy.download.assert_not_called()
    assert results[0].handed_off is True
    assert (
        status(arr_db, video_id) == VideoStatus.DOWNLOADING
    )  # the poll task finishes it


def test_no_indexer_match_falls_through_to_youtube(arr_db, video_id, tmp_path):
    strategy = fake_strategy(
        DownloadResult(success=True, file_path=str(tmp_path / "a.mp4"))
    )
    with patch(
        "src.services.indexers.indexer_service.try_indexer_first", return_value=None
    ):
        results = run(
            UnifiedDownloadService(), context(video_id, indexer_first=True), strategy
        )
    strategy.download.assert_called_once()
    assert results[0].success is True
    assert status(arr_db, video_id) == VideoStatus.DOWNLOADED


def test_bot_check_pauses_youtube_and_requeues_the_video(arr_db, video_id):
    set_settings(youtube_cooldown_hours=6)
    strategy = fake_strategy(
        DownloadResult(
            success=False,
            error_message="All 3 download attempts failed: Sign in to confirm you’re not a bot",
        )
    )
    results = run(UnifiedDownloadService(), context(video_id), strategy)
    assert results[0].handed_off is True
    assert status(arr_db, video_id) == VideoStatus.WANTED
    assert youtube_cooldown.cooldown_remaining() > 5 * 3600

    # While cooling down, a new download is deferred without touching YouTube
    session = arr_db()
    session.get(Video, video_id).status = VideoStatus.DOWNLOADING
    session.commit()
    session.close()
    second = fake_strategy(DownloadResult(success=True))
    run(UnifiedDownloadService(), context(video_id), second)
    second.download.assert_not_called()
    assert status(arr_db, video_id) == VideoStatus.WANTED

    youtube_cooldown.clear_cooldown()
    assert youtube_cooldown.cooldown_remaining() == 0


def test_age_gate_is_a_per_video_failure_not_a_bot_check(arr_db, video_id):
    assert not youtube_cooldown.is_bot_check_error(
        "ERROR: Sign in to confirm your age. This video may be inappropriate"
    )
    assert youtube_cooldown.is_bot_check_error("HTTP Error 429: Too Many Requests")
    service = UnifiedDownloadService()
    assert service._is_dead_youtube_error("Sign in to confirm your age")
    strategy = fake_strategy(
        DownloadResult(success=False, error_message="Sign in to confirm your age")
    )
    with patch.object(
        service, "_find_youtube_alternate", return_value=None
    ) as alternate:
        run(service, context(video_id), strategy)
    alternate.assert_called_once()
    assert status(arr_db, video_id) == VideoStatus.FAILED
    session = arr_db()
    assert (
        session.query(Setting).filter(Setting.key == "youtube_cooldown_until").first()
        is None
    )
    session.close()


def test_output_template_uses_artist_title_file_name(tmp_path):
    strategy = YouTubeDownloadStrategy(
        ytdlp_manager=MagicMock(current_executable="yt-dlp"), anti_detection=MagicMock()
    )
    strategy.anti_detection.get_anti_detection_args.return_value = []
    ctx = context(1, filename="Madonna - Vogue", output_path=str(tmp_path))
    with patch("subprocess.Popen") as popen:
        popen.return_value.stdout.readline.side_effect = [""]
        popen.return_value.returncode = 1
        strategy._attempt_download(ctx, AntiDetectionLevel.MODERATE)
    cmd = popen.call_args.args[0]
    assert cmd[cmd.index("-o") + 1] == str(tmp_path / "Madonna - Vogue.%(ext)s")
    assert "--write-thumbnail" in cmd


def test_adapter_builds_library_paths_and_indexer_flag(tmp_path):
    from src.services.download_service_adapter import DownloadServiceAdapter

    adapter = DownloadServiceAdapter.__new__(DownloadServiceAdapter)
    adapter.music_videos_path = str(tmp_path)
    adapter.active_downloads, adapter.download_queue, adapter.download_history = (
        {},
        [],
        [],
    )
    adapter.unified_service = MagicMock()
    adapter.unified_service.download_video.return_value = 42
    adapter._get_working_cookies_path = lambda: None
    with patch("src.services.download_service_adapter.settings") as settings:
        settings.get.return_value = "artist_title"
        settings.get_bool.return_value = False
        adapter.add_music_video_download(
            "Dua Lipa featuring DaBaby",
            "Dua Lipa - Levitating (Official Video)",
            "u",
            video_id=7,
        )
        ctx = adapter.unified_service.download_video.call_args.args[0]
        assert ctx.output_path == str(tmp_path / "Dua Lipa")
        assert ctx.filename == "Dua Lipa featuring DaBaby - Levitating"
        assert ctx.indexer_first is True
        adapter.add_music_video_download(
            "Madonna", "Vogue", "u", video_id=8, format_string="bv*[height<=1080]+ba"
        )
        assert (
            adapter.unified_service.download_video.call_args.args[0].indexer_first
            is False
        )


def test_scheduled_downloads_respect_the_on_off_setting(arr_db):
    from unittest.mock import patch

    from src.tasks import scheduled_tasks

    from src.services.settings_service import SettingsService

    # Stored in the DB: the task reloads the settings cache first
    SettingsService.set("auto_download_schedule_enabled", "false")
    with patch.object(scheduled_tasks, "get_wanted_videos_for_download") as wanted:
        result = scheduled_tasks.scheduled_downloads_task.run()
    assert result["queued"] == 0 and "turned off" in result["message"]
    wanted.assert_not_called()


def test_library_path_is_read_when_downloading_not_at_import(arr_db, tmp_path):
    """The module-level adapter is built before the DB is up; it must not keep
    the data/musicvideos fallback."""
    from src.services.download_service_adapter import DownloadServiceAdapter

    adapter = DownloadServiceAdapter.__new__(DownloadServiceAdapter)
    set_settings(music_videos_path=str(tmp_path / "library"))
    assert adapter.music_videos_path == str(tmp_path / "library")
    set_settings(music_videos_path="")
    assert adapter.music_videos_path == "data/musicvideos"


def test_failure_is_recorded_for_a_video_with_no_url(arr_db, video_id):
    """No YouTube upload found: the video has no URL, and must still be FAILED."""
    from src.database.models import Download

    session = arr_db()
    video = session.get(Video, video_id)
    video.url = None
    video.youtube_url = None
    session.commit()
    session.close()

    assert UnifiedDownloadService()._update_database_failure(
        video_id, "No YouTube upload found for this video"
    )
    assert status(arr_db, video_id) == VideoStatus.FAILED
    session = arr_db()
    assert session.query(Download).one().original_url == ""
    session.close()


def test_videos_stuck_downloading_go_back_to_wanted(arr_db, video_id):
    """A worker process replaced mid-download leaves the video in DOWNLOADING."""
    from datetime import datetime, timedelta

    from src.database.models import IndexerGrab
    from src.services.video_batch_service import reset_stuck_downloads

    session = arr_db()
    artist_id = session.get(Video, video_id).artist_id
    old = datetime.utcnow() - timedelta(hours=5)
    fresh = Video(artist_id=artist_id, title="Frozen", status=VideoStatus.DOWNLOADING)
    grabbed = Video(artist_id=artist_id, title="Music", status=VideoStatus.DOWNLOADING)
    session.add_all([fresh, grabbed])
    session.flush()
    session.add(
        IndexerGrab(
            video_id=grabbed.id,
            release_title="Madonna-Music-1080p",
            protocol="torrent",
            client="qbittorrent",
            status="downloading",
        )
    )
    session.commit()
    # Backdate after the commit so onupdate doesn't overwrite it
    for vid in (video_id, grabbed.id):
        session.get(Video, vid).updated_at = old
    session.commit()
    fresh_id, grabbed_id = fresh.id, grabbed.id
    session.close()

    assert reset_stuck_downloads(hours=3) == [video_id]
    assert status(arr_db, video_id) == VideoStatus.WANTED
    assert status(arr_db, fresh_id) == VideoStatus.DOWNLOADING  # still running
    assert status(arr_db, grabbed_id) == VideoStatus.DOWNLOADING  # with qBittorrent


def test_leftover_format_pieces_are_never_taken_as_the_video(tmp_path):
    """A killed download leaves "Song.f270.mp4" (video only) next to Song.mp4."""
    import os
    import time

    from src.services.youtube_download_engine import YouTubeDownloadEngine
    from src.utils.filename_cleanup import is_ytdlp_piece

    assert is_ytdlp_piece("Bobby Rydell - Little Bitty Girl.f270.mp4")
    assert is_ytdlp_piece("Song.f140.m4a") and is_ytdlp_piece("Song.mp4.part")
    assert is_ytdlp_piece("Song.fhls-1080p.mp4") and is_ytdlp_piece("Song.mp4.ytdl")
    assert not is_ytdlp_piece("Bobby Rydell - Little Bitty Girl.mp4")
    assert not is_ytdlp_piece("Mr. Brightside.final.mp4")

    stem = "Bobby Rydell - Little Bitty Girl"
    merged = tmp_path / f"{stem}.mp4"
    merged.write_bytes(b"\0" * 2048)
    time.sleep(0.01)
    piece = tmp_path / f"{stem}.f270.mp4"
    piece.write_bytes(b"\0" * 4096)  # newer and larger than the real file
    template = str(tmp_path / f"{stem}.%(ext)s")

    strategy = YouTubeDownloadStrategy(
        ytdlp_manager=MagicMock(current_executable="yt-dlp"), anti_detection=MagicMock()
    )
    assert strategy._find_downloaded_file(template, []) == str(merged)
    line = f"[download] {merged} has already been downloaded"
    assert strategy._find_downloaded_file(template, [line]) == str(merged)

    engine = YouTubeDownloadEngine.__new__(YouTubeDownloadEngine)
    assert os.path.basename(engine._find_downloaded_file(template, [])) == merged.name
    piece_line = f"[download] Destination: {piece}"
    assert engine._find_downloaded_file(template, [piece_line]) == str(merged)
    assert engine._find_downloaded_file(template, [line]) == str(merged)


def test_no_youtube_upload_rests_as_failed_then_is_searched_again(arr_db, video_id):
    from datetime import datetime, timedelta

    from src.services.video_batch_service import (
        YOUTUBE_MISS_KEY,
        resolve_video_url,
        retry_youtube_misses,
    )

    session = arr_db()
    video = session.get(Video, video_id)
    video.url = None
    video.youtube_url = None
    session.commit()
    with patch("src.services.youtube_matcher.find_music_video", return_value=None):
        assert resolve_video_url(video, session) is None
    session.close()

    session = arr_db()
    video = session.get(Video, video_id)
    assert video.video_metadata[YOUTUBE_MISS_KEY]
    video.status = VideoStatus.FAILED
    session.commit()
    session.close()
    assert retry_youtube_misses(days=7) == []  # too soon

    session = arr_db()
    video = session.get(Video, video_id)
    week_ago = (datetime.utcnow() - timedelta(days=8)).isoformat()
    video.video_metadata = {**video.video_metadata, YOUTUBE_MISS_KEY: week_ago}
    session.commit()
    session.close()
    assert retry_youtube_misses(days=7) == [video_id]
    assert status(arr_db, video_id) == VideoStatus.WANTED


def _fake_attempts(tmp_path, heights):
    """_attempt_download stand-in: writes <filename>.mp4 (+ .jpg) and reports it."""
    calls = []

    def attempt(ctx, level, force_player_client=None):
        calls.append((ctx.filename, level))
        path = tmp_path / f"{ctx.filename}.mp4"
        path.write_bytes(f"{level.value}-{ctx.filename}".encode())
        (tmp_path / f"{ctx.filename}.jpg").write_bytes(b"jpg")
        heights[str(path)] = heights.pop(level, None)
        return DownloadResult(success=True, file_path=str(path), file_size=1)

    return attempt, calls


@pytest.mark.parametrize("retry_height,keeps", [(720, "retry"), (240, "original")])
def test_low_res_retry_never_deletes_the_file_it_keeps(tmp_path, retry_height, keeps):
    """The retry used the same name, so dropping the 'other' copy deleted the kept one."""
    strategy = YouTubeDownloadStrategy(
        ytdlp_manager=MagicMock(current_executable="yt-dlp"), anti_detection=MagicMock()
    )
    heights = {
        AntiDetectionLevel.AGGRESSIVE: 360,
        AntiDetectionLevel.MODERATE: retry_height,
    }
    attempt, calls = _fake_attempts(tmp_path, heights)
    strategy._attempt_download = attempt
    strategy._probe_video_height = lambda path: heights.get(path)
    ctx = context(
        1,
        filename="Buddy Knox - Party Doll",
        output_path=str(tmp_path),
        anti_detection=AntiDetectionLevel.AGGRESSIVE,
    )

    result = strategy.download(ctx)

    final = tmp_path / "Buddy Knox - Party Doll.mp4"
    assert result.success and result.file_path == str(final)
    assert final.exists()  # the bug: this file was gone
    assert calls[1][0] == "Buddy Knox - Party Doll.mvidarr-retry"
    expected = "moderate" if keeps == "retry" else "aggressive"
    assert final.read_bytes().startswith(expected.encode())
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "Buddy Knox - Party Doll.jpg",
        "Buddy Knox - Party Doll.mp4",
    ]
