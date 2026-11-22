"""Pure helpers: artist/title matching, release scoring, YouTube candidate scoring."""

from src.services.indexers.prowlarr import Release
from src.services.indexers.release_scoring import (
    detect_resolution,
    rank_releases,
    score_release,
)
from src.services.youtube_matcher import score_candidate
from src.utils.music_matching import (
    clean_video_title,
    library_folder,
    library_stem,
    parse_artist_title,
    song_key,
    split_featured,
)

MB = 1024 * 1024


def release(title, protocol="usenet", size=80 * MB, seeders=None):
    return Release(
        title=title,
        guid=title,
        indexer="NZBgeek",
        protocol=protocol,
        size=size,
        seeders=seeders,
    )


class TestMusicMatching:
    def test_library_names_drop_video_decorations(self):
        assert library_stem(
            "Taylor Swift", "Taylor Swift - Anti-Hero (Official Music Video)"
        ) == ("Taylor Swift - Anti-Hero")
        assert clean_video_title(
            "Rick Astley - Never Gonna Give You Up (Official Music Video) [4K Remaster]",
            "Rick Astley",
        ) == ("Never Gonna Give You Up")

    def test_featured_credits_go_to_the_main_artist_folder(self):
        assert split_featured("Dua Lipa featuring DaBaby") == ("Dua Lipa", ["DaBaby"])
        assert library_folder("Dua Lipa featuring DaBaby") == "Dua Lipa"
        assert library_folder("Simon & Garfunkel") == "Simon & Garfunkel"

    def test_song_key_ignores_band_suffixes_and_decorations(self):
        assert song_key("Prince and The Revolution", "When Doves Cry") == song_key(
            "Prince", "When Doves Cry (Official Video)"
        )
        assert song_key("The Weeknd", "Blinding Lights") == song_key(
            "Weeknd", "Blinding Lights"
        )
        assert song_key("Madonna", "Like a Prayer") != song_key("Madonna", "Vogue")

    def test_unsafe_characters_are_removed_from_file_names(self):
        assert library_stem("AC/DC", "Who Made Who?") == "ACDC - Who Made Who"

    def test_parse_artist_title_keeps_punctuation(self):
        assert parse_artist_title("Guns N' Roses - Sweet Child O' Mine") == (
            "Guns N' Roses",
            "Sweet Child O' Mine",
        )
        assert parse_artist_title("NoSeparatorHere") == (None, None)


class TestReleaseScoring:
    def test_scene_release_is_accepted(self):
        scored = score_release(
            release("Madonna-Like_A_Prayer-DVDRip-x264-2005-MiMP"),
            "Madonna",
            "Like a Prayer",
        )
        assert scored.accepted, scored.reasons
        assert scored.resolution == 480

    def test_hyphenated_title_and_featured_artist(self):
        scored = score_release(
            release("Dua_Lipa_ft_DaBaby-Levitating-1080p-WEB-x264-2020-ZMV"),
            "Dua Lipa featuring DaBaby",
            "Levitating",
        )
        assert scored.accepted, scored.reasons
        assert scored.resolution == 1080
        assert score_release(
            release("Taylor_Swift-Anti-Hero-1080p-x264-2022-BTSFHD"),
            "Taylor Swift",
            "Anti-Hero",
        ).accepted

    def test_live_and_compilation_releases_are_rejected(self):
        assert not score_release(
            release("Madonna-Like_A_Prayer_Live_In_Paris-DVDRip-x264-MiMP"),
            "Madonna",
            "Like a Prayer",
        ).accepted
        assert not score_release(
            release("Madonna-The_Immaculate_Collection-DVD9-2007"),
            "Madonna",
            "Like a Prayer",
        ).accepted

    def test_word_in_the_song_title_is_not_a_reject(self):
        scored = score_release(
            release("Paul_McCartney_And_Wings-Live_And_Let_Die-DVDRip-x264-1973-iUF"),
            "Paul McCartney & Wings",
            "Live and Let Die",
        )
        assert scored.accepted, scored.reasons

    def test_wrong_song_or_artist_is_rejected(self):
        assert not score_release(
            release("Madonna-Vogue-DVDRip-x264-1990-MiMP"), "Madonna", "Like a Prayer"
        ).accepted
        assert not score_release(
            release("Cher-Believe-DVDRip-x264-1998-MiMP"), "Madonna", "Believe"
        ).accepted

    def test_torrent_without_seeders_and_tiny_files_are_rejected(self):
        assert not score_release(
            release("Madonna-Vogue-1080p-x264", protocol="torrent", seeders=0),
            "Madonna",
            "Vogue",
        ).accepted
        assert not score_release(
            release("Madonna-Vogue-1080p-x264", size=1 * MB), "Madonna", "Vogue"
        ).accepted

    def test_usenet_outranks_torrent_and_resolution_counts(self):
        ranked = rank_releases(
            [
                release("Madonna-Vogue-DVDRip-x264-1990-MiMP"),
                release(
                    "Madonna-Vogue-1080p-WEB-x264-1990-ZMV",
                    protocol="torrent",
                    seeders=40,
                ),
                release("Madonna-Vogue-1080p-WEB-x264-1990-iUF"),
            ],
            "Madonna",
            "Vogue",
        )
        assert ranked[0].release.title == "Madonna-Vogue-1080p-WEB-x264-1990-iUF"
        assert ranked[1].release.protocol == "torrent"

    def test_minimum_resolution(self):
        scored = score_release(
            release("Madonna-Vogue-DVDRip-x264-1990-MiMP"),
            "Madonna",
            "Vogue",
            min_resolution=720,
        )
        assert not scored.accepted
        assert detect_resolution("Some.Artist.Song.2160p.WEB.H265") == 2160


class TestYouTubeCandidateScoring:
    def test_official_music_video_scores_as_official(self):
        cand = {
            "title": "Like a Prayer",
            "duration": 340,
            "names": ["Madonna"],
            "omv": True,
        }
        assert score_candidate("Madonna", "Like a Prayer", cand, 0) >= 60

    def test_lyric_and_cover_uploads_are_rejected(self):
        for title in (
            "Madonna - Like a Prayer (Lyrics)",
            "Like a Prayer - Madonna cover",
        ):
            cand = {"title": title, "duration": 300, "names": ["fan"], "omv": False}
            assert score_candidate("Madonna", "Like a Prayer", cand, 0) is None

    def test_fan_upload_is_unofficial(self):
        cand = {
            "title": "Madonna - Like a Prayer (music video)",
            "duration": 340,
            "names": ["oldtv"],
            "omv": False,
            "views": "2.1M",
        }
        score = score_candidate("Madonna", "Like a Prayer", cand, 2)
        assert score is not None and score < 60

    def test_plain_youtube_search_uses_the_channel(self):
        cand = {
            "title": "Madonna - Like A Prayer (Official Video)",
            "duration": 340,
            "names": ["Madonna"],
            "omv": None,
        }
        assert score_candidate("Madonna", "Like a Prayer", cand, 1) >= 60
        too_long = dict(cand, duration=3600)
        assert score_candidate("Madonna", "Like a Prayer", too_long, 1) is None
