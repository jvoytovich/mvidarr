"""Tests for resolve_video_url()'s timeout parameter (#380.1)."""

from unittest.mock import MagicMock, patch

from src.services.video_batch_service import resolve_video_url

# The YouTube Music search is skipped here (patched to fail) so the yt-dlp
# search fallback, which is what the timeout bounds, is the one that runs.
_NO_YTMUSIC = patch(
    "src.services.youtube_matcher._ytmusic_candidates",
    side_effect=RuntimeError("no network in tests"),
)


class TestResolveVideoUrlTimeout:
    def test_default_timeout_is_30_seconds(self):
        video = MagicMock(url=None, youtube_url=None, video_metadata=None)
        session = MagicMock()
        with _NO_YTMUSIC, patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="not found"
            )
            resolve_video_url(video, session)
            assert mock_run.call_args.kwargs["timeout"] == 30

    def test_custom_timeout_is_passed_through(self):
        video = MagicMock(url=None, youtube_url=None, video_metadata=None)
        session = MagicMock()
        with _NO_YTMUSIC, patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="not found"
            )
            resolve_video_url(video, session, timeout=10)
            assert mock_run.call_args.kwargs["timeout"] == 10

    def test_existing_url_short_circuits_without_calling_subprocess(self):
        video = MagicMock(url="https://youtube.com/watch?v=abc123")
        session = MagicMock()
        with patch("subprocess.run") as mock_run:
            result = resolve_video_url(video, session, timeout=5)
            assert result == "https://youtube.com/watch?v=abc123"
            mock_run.assert_not_called()
