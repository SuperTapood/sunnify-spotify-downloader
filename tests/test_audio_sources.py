"""Hermetic provider searches and end-to-end fallback through the real engine."""

from __future__ import annotations

import concurrent.futures
import threading
from unittest.mock import MagicMock

import pytest
import requests

import audio_sources as sources
import Spotify_Downloader as app
import sunnify_cli as cli
from spotifydown_api import TrackInfo


def music_result(video_id="catalog0001", **overrides):
    result = {
        "videoId": video_id,
        "resultType": "song",
        "title": "Song",
        "artists": [{"name": "Artist"}],
        "album": {"name": "Album"},
        "duration": "3:00",
    }
    result.update(overrides)
    return result


@pytest.fixture
def music_client(monkeypatch):
    client = MagicMock()
    client.search.return_value = []
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(sources, "YTMusic", factory)
    return client


@pytest.fixture
def scraper(music_client, tmp_path, monkeypatch):
    engine = app.MusicScraper()
    monkeypatch.setattr(app, "get_ffmpeg_path", lambda: str(tmp_path))
    # One attempt per video makes cross-source retry assertions independent of
    # the yt-dlp version's current player-client table.
    monkeypatch.setattr(app, "_retry_player_clients", lambda: ())
    yield engine
    engine.close()


def download(engine, destination, **kwargs):
    return engine.download_track_audio(
        "ytsearch1:Song Artist audio",
        str(destination),
        expected_title="Song",
        expected_artists="Artist",
        expected_duration_s=180,
        expected_album="Album",
        **kwargs,
    )


def playback(monkeypatch, destination, successful_ids=()):
    calls = []
    options = []

    class FakeYDL:
        def __init__(self, opts):
            options.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, *, download=False):
            assert download is True
            calls.append(url.rsplit("=", 1)[-1])
            if calls[-1] in successful_ids:
                destination.write_bytes(b"audio")

    monkeypatch.setattr(app, "YoutubeDL", FakeYDL)
    return calls, options


def test_search_is_lazy_isrc_then_songs_then_videos(music_client):
    music_client.search.return_value = [music_result()]
    results = sources.iter_youtube_music_results("Song", "Artist", isrc="us-abc-24-12345")
    assert next(results)[0]["id"] == "catalog0001"
    assert music_client.search.call_count == 1
    assert music_client.search.call_args.args == ("USABC2412345",)
    assert music_client.search.call_args.kwargs["filter"] == "songs"
    next(results)
    next(results)
    assert [call.args[0] for call in music_client.search.call_args_list] == [
        "USABC2412345",
        "Song Artist",
        "Song Artist",
    ]
    assert [call.kwargs["filter"] for call in music_client.search.call_args_list] == [
        "songs",
        "songs",
        "videos",
    ]
    results.close()


@pytest.mark.parametrize("isrc", [None, "", "search anything", "US1234", 123])
def test_invalid_isrc_does_not_become_a_search(music_client, isrc):
    list(sources.iter_youtube_music_results("Song", "Artist", isrc=isrc))
    assert [call.args[0] for call in music_client.search.call_args_list] == [
        "Song Artist",
        "Song Artist",
    ]


def test_normalizes_catalog_metadata_and_ignores_unusable_results(music_client):
    music_client.search.return_value = [
        None,
        {},
        music_result(video_id="invalid"),
        music_result(video_id="bad&query=x"),
        music_result(artists=[]),
        music_result(artists={"name": "Artist"}),
        music_result(resultType="album"),
        music_result(title=None),
        music_result(duration_seconds=180),
    ]
    groups = sources.iter_youtube_music_results("Song", "Artist")
    assert next(groups) == [
        {
            "id": "catalog0001",
            "title": "Song",
            "result_type": "song",
            "artists": ["Artist"],
            "album": "Album",
            "duration": 180,
        }
    ]
    groups.close()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1:02:03", 3723),
        ("3:00", 180),
        (180, 180),
        (None, None),
        ("LIVE", None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_result_duration_normalization(music_client, value, expected):
    music_client.search.return_value = [music_result(duration=value)]
    groups = sources.iter_youtube_music_results("Song", "Artist")
    assert next(groups)[0]["duration"] == expected
    groups.close()


def test_network_failure_recreates_client_once_then_abandons_source(music_client):
    music_client.search.side_effect = requests.Timeout("offline")
    assert list(sources.iter_youtube_music_results("Song", "Artist")) == []
    assert music_client.search.call_count == sources.YTMusic.call_count == 2


def test_transient_search_failure_recovers(music_client):
    music_client.search.side_effect = [requests.Timeout("temporary"), [music_result()]]
    groups = sources.iter_youtube_music_results("Song", "Artist")
    assert next(groups)[0]["id"] == "catalog0001"
    assert music_client.search.call_count == 2
    groups.close()


def test_cancel_during_failed_search_prevents_retry(music_client):
    cancelled = threading.Event()

    def fail(*_args, **_kwargs):
        cancelled.set()
        raise requests.Timeout("cancelled")

    music_client.search.side_effect = fail
    with pytest.raises(InterruptedError):
        list(sources.iter_youtube_music_results("Song", "Artist", cancelled=cancelled.is_set))
    assert music_client.search.call_count == 1


def test_parallel_searches_own_sessions_and_close_on_early_return(monkeypatch):
    sessions = []
    session_lock = threading.Lock()

    class SearchSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.was_closed = False
            with session_lock:
                sessions.append(self)

        def close(self):
            self.was_closed = True
            super().close()

    monkeypatch.setattr(sources.requests, "Session", SearchSession)
    factory = MagicMock(
        side_effect=lambda **_kw: MagicMock(search=lambda *_a, **_k: [music_result()])
    )
    monkeypatch.setattr(sources, "YTMusic", factory)

    def search(_index):
        results = sources.iter_youtube_music_results("Song", "Artist")
        try:
            return next(results)
        finally:
            results.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(search, range(4)))
    assert len(sessions) == 4
    assert all(session.was_closed for session in sessions)
    assert all(session.request.keywords["timeout"] == 10 for session in sessions)
    assert {id(call.kwargs["requests_session"]) for call in factory.call_args_list} == {
        id(session) for session in sessions
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "Another Song"},
        {"title": "Song", "artists": [{"name": "Someone Else"}]},
        {"title": "Song (Live)"},
        {"title": "Song (Remix)"},
        {"title": "Song (Cover)"},
        {"title": "Song (Acoustic)"},
        {"title": "Song (Remastered 2024)"},
        {"title": "Song (Reverb)"},
        {"duration": "3:30"},
        {"duration": None},
    ],
)
def test_music_rejects_wrong_recording_even_for_isrc(scraper, music_client, overrides):
    music_client.search.return_value = [music_result(**overrides)]
    assert (
        list(scraper._iter_youtube_music_matches("Song", "Artist", 180, "Album", "USABC2412345"))
        == []
    )


def test_music_requires_requested_live_version(scraper, music_client):
    music_client.search.return_value = [music_result()]
    assert (
        list(scraper._iter_youtube_music_matches("Song (Live)", "Artist", 180, "Album", None)) == []
    )


def test_music_accepts_equivalent_remaster_labels(scraper, music_client):
    music_client.search.return_value = [music_result(title="Song (Remaster 2024)")]
    matches = scraper._iter_youtube_music_matches(
        "Song - Remastered 2024", "Artist", 180, "Album", None
    )
    assert next(matches).endswith("catalog0001")
    matches.close()


def test_album_match_ranks_ahead_of_reissue(scraper, music_client):
    music_client.search.return_value = [
        music_result("reissue0001", album={"name": "Greatest Hits"}),
        music_result("catalog0001", duration="3:01"),
    ]
    matches = scraper._iter_youtube_music_matches("Song", "Artist", 180, "Album", None)
    assert next(matches).endswith("catalog0001")
    matches.close()


def test_music_match_downloads_before_youtube_and_preserves_options(
    scraper, music_client, monkeypatch, tmp_path
):
    music_client.search.return_value = [music_result()]
    selector = MagicMock()
    monkeypatch.setattr(scraper, "_select_youtube_match", selector)
    destination = tmp_path / "song.mp3"
    calls, options = playback(monkeypatch, destination, {"catalog0001"})
    assert download(scraper, destination, expected_isrc="USABC2412345") == str(destination)
    assert calls == ["catalog0001"]
    assert music_client.search.call_count == 1
    selector.assert_not_called()
    assert options[0]["postprocessors"][0]["preferredcodec"] == "mp3"
    assert options[0]["cookiesfrombrowser"] == ("firefox",)


def test_unplayable_music_song_tries_music_video(scraper, music_client, monkeypatch, tmp_path):
    music_client.search.side_effect = [
        [music_result()],
        [music_result("video000001", resultType="video")],
    ]
    selector = MagicMock()
    monkeypatch.setattr(scraper, "_select_youtube_match", selector)
    destination = tmp_path / "song.mp3"
    calls, _ = playback(monkeypatch, destination, {"video000001"})
    assert download(scraper, destination) == str(destination)
    assert calls == ["catalog0001", "video000001"]
    selector.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_empty_or_failed_music_search_falls_back_to_youtube(
    scraper, music_client, monkeypatch, tmp_path, failure
):
    if failure:
        music_client.search.side_effect = requests.ConnectionError("offline")
    monkeypatch.setattr(
        scraper,
        "_select_youtube_match",
        lambda *_a, **_k: "https://www.youtube.com/watch?v=video000001",
    )
    destination = tmp_path / "song.mp3"
    calls, _ = playback(monkeypatch, destination, {"video000001"})
    assert download(scraper, destination) == str(destination)
    assert calls == ["video000001"]


def test_candidates_are_deduplicated_and_each_source_keeps_retry_budget(
    scraper, music_client, monkeypatch, tmp_path
):
    music_client.search.return_value = [music_result(f"music00000{index}") for index in range(3)]
    youtube_ids = iter([f"video00000{index}" for index in range(3)])
    excluded = []

    def select(*_args, **kwargs):
        excluded.append(set(kwargs["excluded_video_urls"]))
        return f"https://www.youtube.com/watch?v={next(youtube_ids)}"

    monkeypatch.setattr(scraper, "_select_youtube_match", select)
    destination = tmp_path / "song.mp3"
    calls, _ = playback(monkeypatch, destination)
    with pytest.raises(RuntimeError, match="no playable audio source"):
        download(scraper, destination)
    assert calls == [f"music00000{index}" for index in range(3)] + [
        f"video00000{index}" for index in range(3)
    ]
    assert len(excluded[0]) == 3


def test_same_music_id_is_not_downloaded_again_in_video_or_youtube_results(
    scraper, music_client, monkeypatch, tmp_path
):
    music_client.search.side_effect = [
        [music_result()],
        [music_result(), music_result("video000001", resultType="video")],
    ]

    def select(*_args, **kwargs):
        assert "https://www.youtube.com/watch?v=catalog0001" in kwargs["excluded_video_urls"]
        return "https://www.youtube.com/watch?v=catalog0001"

    monkeypatch.setattr(scraper, "_select_youtube_match", select)
    calls, _ = playback(monkeypatch, tmp_path / "song.mp3")
    with pytest.raises(RuntimeError, match="no playable audio source"):
        download(scraper, tmp_path / "song.mp3")
    assert calls == ["catalog0001", "video000001"]


@pytest.mark.parametrize("source", ["youtube-music", "youtube"])
def test_explicit_source_limits_discovery(scraper, music_client, monkeypatch, tmp_path, source):
    scraper.audio_source = source
    selector = MagicMock(return_value=None)
    monkeypatch.setattr(scraper, "_select_youtube_match", selector)
    with pytest.raises(RuntimeError, match="no playable audio source"):
        download(scraper, tmp_path / "song.mp3")
    assert bool(music_client.search.call_count) == (source == "youtube-music")
    assert bool(selector.call_count) == (source == "youtube")


def test_cancellation_prevents_discovery(scraper, music_client, monkeypatch, tmp_path):
    scraper._cancel_event.set()
    selector = MagicMock()
    monkeypatch.setattr(scraper, "_select_youtube_match", selector)
    with pytest.raises(InterruptedError):
        download(scraper, tmp_path / "song.mp3")
    music_client.search.assert_not_called()
    selector.assert_not_called()


@pytest.mark.parametrize(
    "fmt,format_selector",
    [
        ("m4a", "bestaudio[ext=m4a]/bestaudio/best"),
        ("opus", "bestaudio[acodec=opus]/bestaudio/best"),
    ],
)
def test_prefers_native_audio_format(
    scraper, music_client, monkeypatch, tmp_path, fmt, format_selector
):
    scraper.audio_format = fmt
    music_client.search.return_value = [music_result()]
    destination = tmp_path / f"song.{fmt}"
    _, options = playback(monkeypatch, destination, {"catalog0001"})
    assert download(scraper, destination) == str(destination)
    assert options[0]["format"] == format_selector


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"isrc": "us-abc-24-12345"}, "USABC2412345"),
        ({"external_ids": {"isrc": "USABC2412345"}}, "USABC2412345"),
        ({"external_id": [None, {"type": "isrc", "id": "USABC2412345"}]}, "USABC2412345"),
        ({"isrc": "invalid", "external_ids": {"isrc": "USABC2412345"}}, "USABC2412345"),
        ({}, None),
        ({"external_ids": None}, None),
        ({"isrc": "arbitrary search"}, None),
    ],
)
def test_spotify_recording_codes_from_supported_metadata_shapes(raw, expected):
    track = TrackInfo("id", "Song", "Artist", "Album", None, None, 180000, None, raw)
    assert track.isrc == expected


def test_enriched_isrc_reaches_downloader(scraper, tmp_path, monkeypatch):
    original = TrackInfo("id", "Song", "Artist", None, None, None, 180000, None, {})
    enriched = TrackInfo(
        "id",
        "Song",
        "Artist",
        "Album",
        "2024",
        None,
        180000,
        None,
        {"external_ids": {"isrc": "USABC2412345"}},
    )
    api = MagicMock()
    api.get_track.return_value = enriched
    scraper.spotifydown_api = api
    captured = {}

    def save(_query, destination, **kwargs):
        captured.update(kwargs)
        from pathlib import Path

        Path(destination).write_bytes(b"audio")
        return destination

    monkeypatch.setattr(scraper, "download_track_audio", save)
    assert scraper._download_one_track(original, str(tmp_path), None) is None
    assert captured["expected_isrc"] == "USABC2412345"


def test_source_cli_flag_reaches_engine(music_client):
    args = cli.build_parser().parse_args(
        ["download", "spotify:track:id", "--audio-source", "youtube"]
    )
    config = {setting.key: setting.default for setting in app.SETTINGS}
    engine = cli._build_scraper(args, config, threading.Event())
    try:
        assert engine.audio_source == "youtube"
    finally:
        engine.close()


def test_source_setting_round_trip_in_dialog(qapp):
    dialog = app.SettingsDialog(None, {"audio_source": "youtube-music"})
    assert dialog._audio_source_cb.currentData() == "youtube-music"
    dialog._audio_source_cb.setCurrentIndex(dialog._audio_source_cb.findData("youtube"))
    assert dialog.result_config()["audio_source"] == "youtube"
    dialog.close()
