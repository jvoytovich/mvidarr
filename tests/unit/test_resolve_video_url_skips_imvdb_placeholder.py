"""Live-reported: a batch of auto-download attempts failed with

    No strategy available for URL: https://imvdb.com/video/197047016632

Root cause: video_discovery_service.py's _search_imvdb_for_artist()
stores an IMVDb *metadata page* URL in video.url as an explicit
"placeholder URL" fallback when IMVDb has no linked YouTube source --
not something yt-dlp can ever download. resolve_video_url() (the
Celery/batch auto-download path's URL resolver, separate from
_stored_video_url() in videos_downloads.py, which #459 already fixed
for the FastAPI dispatch paths) short-circuited on `if video.url:
return video.url` -- returning the unusable placeholder immediately
and never reaching its own, more capable fallback: a live YouTube
search (`ytsearch1:{artist} {title}`) that could otherwise have
resolved a genuinely downloadable URL for these videos.

Fix: resolve_video_url() now treats an imvdb.com/video/<id> placeholder
the same as no stored URL at all, letting it fall through to the
youtube_url check and then the live YouTube search -- giving these
videos a real chance to resolve, not just a clearer failure.
"""

from unittest.mock import MagicMock, patch

from src.services.video_batch_service import resolve_video_url


class TestResolveVideoUrlSkipsImvdbPlaceholder:
    # The live search is now the official-video matcher
    # (src/services/youtube_matcher.py) instead of a bare ytsearch1.
    def test_imvdb_placeholder_does_not_short_circuit(self):
        video = MagicMock(
            url="https://imvdb.com/video/197047016632",
            youtube_url=None,
            video_metadata=None,
            title="Rats",
        )
        video.artist.name = "Ghost"
        session = MagicMock()
        with patch(
            "src.services.youtube_matcher.find_music_video", return_value=None
        ) as mock_find:
            resolve_video_url(video, session, timeout=5)

        mock_find.assert_called_once()

    def test_imvdb_placeholder_falls_through_to_a_live_youtube_search_result(self):
        video = MagicMock(
            url="https://imvdb.com/video/197047016632",
            youtube_url=None,
            youtube_id=None,
            video_metadata=None,
            video_type=None,
        )
        video.artist.name = "Ghost"
        video.title = "Rats"
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = None

        match = {
            "url": "https://www.youtube.com/watch?v=xyz789abcde",
            "video_id": "xyz789abcde",
            "kind": "official",
            "title": "Ghost - Rats (Official Music Video)",
            "channel": "Ghost",
            "score": 100,
        }
        with patch("src.services.youtube_matcher.find_music_video", return_value=match):
            result = resolve_video_url(video, session, timeout=5)

        assert result == "https://www.youtube.com/watch?v=xyz789abcde"
        assert video.url == "https://www.youtube.com/watch?v=xyz789abcde"
        assert video.youtube_id == "xyz789abcde"
        assert video.video_type == "official_music_video"

    def test_unofficial_pick_is_flagged_with_its_uploader(self):
        video = MagicMock(
            url=None,
            youtube_url=None,
            youtube_id=None,
            video_metadata=None,
            video_type=None,
        )
        video.artist.name = "Van Halen"
        video.title = "Jump"
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = None
        match = {
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "video_id": "abcdefghijk",
            "kind": "unofficial",
            "title": "Van Halen - Jump (TV)",
            "channel": "oldtv",
            "score": 40,
        }
        with patch("src.services.youtube_matcher.find_music_video", return_value=match):
            resolve_video_url(video, session, timeout=5)
        assert video.video_type == "unofficial_clip"
        assert video.video_metadata == {"uploader": "oldtv"}

    def test_imvdb_placeholder_falls_through_to_youtube_url_before_searching(self):
        video = MagicMock(
            url="https://imvdb.com/video/197047016632",
            youtube_url="https://youtube.com/watch?v=already-known-good-id",
        )
        session = MagicMock()
        with patch("subprocess.run") as mock_run:
            result = resolve_video_url(video, session, timeout=5)

        assert result == "https://youtube.com/watch?v=already-known-good-id"
        mock_run.assert_not_called()

    def test_a_real_downloadable_url_still_short_circuits(self):
        # Not a regression on the common case: a genuine URL is still
        # returned immediately, no subprocess call.
        video = MagicMock(url="https://youtube.com/watch?v=abc123")
        session = MagicMock()
        with patch("subprocess.run") as mock_run:
            result = resolve_video_url(video, session, timeout=5)

        assert result == "https://youtube.com/watch?v=abc123"
        mock_run.assert_not_called()
