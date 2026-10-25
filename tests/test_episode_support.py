"""Spotify episodes use their own embed metadata and full audio sources."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import Spotify_Downloader as app
import sunnify_cli as cli
from spotifydown_api import (
    ContentUnavailableError,
    PlaylistClient,
    SpotifyDownAPIError,
    SpotifyEmbedAPI,
    detect_spotify_url_type,
    parse_spotify_urls,
)

EPISODE_ID = "26KO3JVD9z5auNXdlGu27l"
EPISODE_URL = f"https://open.spotify.com/episode/{EPISODE_ID}"
SHARED_URL = EPISODE_URL + "?si=495152236c58458c"
PREVIEW_URL = "https://p.scdn.co/mp3-preview/preview.mp3"
FULL_AUDIO_URL = "https://podcast.example.com/full-episode.mp3"


@pytest.fixture
def episode_data():
    # The supplied episode's embed shape, with public test URLs and no tokens.
    return {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": {
                            "type": "episode",
                            "name": "SPIN English cover ",
                            "subtitle": "Chilling in the basement ",
                            "uri": f"spotify:episode:{EPISODE_ID}",
                            "duration": 87516,
                            "releaseDate": {"isoString": "2026-10-14T02:01:00Z"},
                            "audioPreview": {"url": PREVIEW_URL},
                            "relatedEntityUri": "spotify:show:3wNxPRUhdrQ80NvqJ4L9z2",
                            "visualIdentity": {
                                "image": [{"url": "https://example.com/cover.jpg", "maxWidth": 300}]
                            },
                        },
                        "defaultAudioFileObject": {
                            "format": "MP4_128_CBCS",
                            "url": ["https://example.com/encrypted-audio"],
                        },
                    }
                }
            }
        }
    }


@pytest.fixture
def episode_client(episode_data):
    session = MagicMock()
    session.get.return_value.status_code = 200
    session.get.return_value.text = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(episode_data)
        + "</script>"
    )
    client = PlaylistClient(session=session)
    yield client
    client.close()


@pytest.mark.parametrize(
    "url",
    [
        EPISODE_URL,
        SHARED_URL,
        EPISODE_URL.replace("/episode/", "/intl-he/episode/") + "?si=shared#details",
        f"spotify:episode:{EPISODE_ID}",
    ],
)
def test_episode_url_forms_are_accepted(url):
    assert detect_spotify_url_type(url) == ("episode", EPISODE_ID)
    assert parse_spotify_urls(url) == [url]


def test_episode_queue_deduplicates_and_preserves_other_types():
    track = f"spotify:track:{EPISODE_ID}"
    assert parse_spotify_urls([SHARED_URL, f"spotify:episode:{EPISODE_ID}", track]) == [
        SHARED_URL,
        track,
    ]


def test_episode_metadata_uses_show_artwork_date_and_duration(episode_client):
    episode = episode_client.get_episode(EPISODE_ID)
    assert episode.id == EPISODE_ID
    assert episode.title == "SPIN English cover"
    assert episode.artists == episode.album == "Chilling in the basement"
    assert episode.release_date == "2026-10-14"
    assert episode.duration_ms == 87516
    assert episode.cover_url == "https://example.com/cover.jpg"
    assert episode.preview_url == PREVIEW_URL
    assert episode.audio_url is None  # This episode's Spotify stream is encrypted.
    episode_client._session.get.assert_called_once()
    assert episode_client._session.get.call_args.args[0] == (
        f"https://open.spotify.com/embed/episode/{EPISODE_ID}"
    )


@pytest.mark.parametrize("container", ["state", "data", "entity"])
def test_episode_metadata_accepts_alternate_embed_layouts(episode_data, monkeypatch, container):
    data = episode_data["props"]["pageProps"]["state"]["data"]
    if container == "state":
        payload = episode_data
    elif container == "data":
        payload = {"props": {"pageProps": {"data": data}}}
    else:
        payload = {"props": {"pageProps": {"entity": data["entity"]}}}
    api = SpotifyEmbedAPI()
    monkeypatch.setattr(api, "_fetch_embed_data", lambda _: payload)
    try:
        assert api.get_episode(EPISODE_ID).title == "SPIN English cover"
    finally:
        api.close()


@pytest.mark.parametrize("url", [FULL_AUDIO_URL, [FULL_AUDIO_URL]])
def test_full_mp3_episode_source_is_separate_from_preview(episode_data, monkeypatch, url):
    data = episode_data["props"]["pageProps"]["state"]["data"]
    data["defaultAudioFileObject"] = {"format": "MP3_128", "url": url}
    api = SpotifyEmbedAPI()
    fetch = MagicMock(return_value=episode_data)
    monkeypatch.setattr(api, "_fetch_embed_data", fetch)
    try:
        episode = api.get_episode(EPISODE_ID)
        assert episode.audio_url == FULL_AUDIO_URL
        assert episode.preview_url == PREVIEW_URL
        api.get_episode(EPISODE_ID)
        assert fetch.call_count == 2  # Signed audio URLs must be fetched afresh.
    finally:
        api.close()


def test_full_mp3_source_prefers_external_podcast_url(episode_data):
    data = episode_data["props"]["pageProps"]["state"]["data"]
    data["defaultAudioFileObject"] = {
        "format": "MP3_128",
        "url": ["https://example.com/cdn.mp3"],
        "passthroughUrl": FULL_AUDIO_URL,
    }
    assert SpotifyEmbedAPI._episode_audio_url(episode_data) == FULL_AUDIO_URL


@pytest.mark.parametrize(
    "audio",
    [
        None,
        {},
        {"format": "MP4_128_CBCS", "url": [FULL_AUDIO_URL]},
        {"format": "MP3_96", "url": [PREVIEW_URL]},
        {"format": "MP3_128", "url": PREVIEW_URL + "?signed=preview"},
        {"format": "MP3_128", "url": "https://p.scdn.co/mp3-preview/other.mp3"},
        {"format": "MP3_128", "url": "file:///local.mp3"},
        {"format": "MP3_128", "url": 123},
    ],
)
def test_previews_and_encrypted_or_missing_sources_are_not_downloads(episode_data, audio):
    data = episode_data["props"]["pageProps"]["state"]["data"]
    data["defaultAudioFileObject"] = audio
    assert SpotifyEmbedAPI._episode_audio_url(episode_data) is None


def test_unavailable_episode_preserves_actionable_error(episode_client):
    episode_client._session.get.return_value.text = (
        '<script id="__NEXT_DATA__">'
        '{"props":{"pageProps":{"status":404,"title":"Unavailable"}}}'
        "</script>"
    )
    with pytest.raises(ContentUnavailableError):
        episode_client.get_episode(EPISODE_ID)


def test_malformed_episode_is_reported_as_episode_error(episode_client):
    episode_client._session.get.return_value.text = "not an embed page"
    with pytest.raises(SpotifyDownAPIError, match=f"Could not fetch episode {EPISODE_ID}"):
        episode_client.get_episode(EPISODE_ID)


def test_desktop_mixed_queue_routes_episodes_as_single_items(monkeypatch):
    thread = app.ScraperThread([SHARED_URL, "spotify:track:song", "spotify:album:album"])
    single = MagicMock()
    collection = MagicMock()
    monkeypatch.setattr(thread.scraper, "scrape_track", single)
    monkeypatch.setattr(thread.scraper, "scrape_playlist", collection)
    try:
        thread.run()
        assert [call.args[0] for call in single.call_args_list] == [
            SHARED_URL,
            "spotify:track:song",
        ]
        collection.assert_called_once_with("spotify:album:album", thread.music_folder)
    finally:
        thread.scraper.close()


@pytest.mark.parametrize("audio_url", [FULL_AUDIO_URL, None])
def test_episode_download_uses_full_audio_or_existing_search(
    episode_client, monkeypatch, tmp_path, audio_url
):
    episode = replace(episode_client.get_episode(EPISODE_ID), audio_url=audio_url)
    client = MagicMock()
    client.get_episode.return_value = episode
    scraper = app.MusicScraper(write_metadata=False)
    monkeypatch.setattr(scraper, "ensure_spotifydown_api", lambda: client)
    download_calls = []

    def download(query, destination, **kwargs):
        download_calls.append((query, kwargs))
        Path(destination).write_bytes(b"full audio")
        return destination

    monkeypatch.setattr(scraper, "download_track_audio", download)
    try:
        scraper.scrape_track(SHARED_URL, str(tmp_path))
        client.get_episode.assert_called_once_with(EPISODE_ID)
        client.get_track.assert_not_called()
        query, options = download_calls[0]
        assert "SPIN English cover" in query
        assert "Chilling in the basement" not in query
        assert options["expected_artists"] == options["expected_album"] == ""
        assert options["is_episode"] is True
        assert options["expected_duration_s"] == 87.516
        assert options.get("source_url") == audio_url
        assert PREVIEW_URL not in options.values()
        assert scraper._failed_tracks == []
        assert len(list(tmp_path.glob("*.mp3"))) == 1
    finally:
        scraper.close()


@pytest.mark.parametrize("duration", [92, 198, None])
def test_episode_youtube_matching_uses_title_and_tight_duration(monkeypatch, duration):
    scraper = app.MusicScraper()
    candidate = {
        "id": "QgdvSXqnN30",
        "title": "Spin ENGLISH COVER | JJBA: Steel Ball Run",
        "duration": duration,
        "uploader": "ChaseD_Ih and 4 more",
    }
    monkeypatch.setattr(scraper, "_extract_youtube_info", lambda *_args: {"entries": [candidate]})
    try:
        result = scraper._select_youtube_match(
            "ytsearch5:SPIN English cover audio",
            87.516,
            expected_title="SPIN English cover",
            expected_artists="",
            is_episode=True,
        )
        assert result == ("https://www.youtube.com/watch?v=QgdvSXqnN30" if duration == 92 else None)
    finally:
        scraper.close()


def test_episode_youtube_music_accepts_a_cover_from_another_channel(monkeypatch):
    scraper = app.MusicScraper()
    candidates = [
        {
            "id": "longversion",
            "title": "SPIN English Cover",
            "result_type": "video",
            "duration": 198,
            "artists": ["ONE PROJECT"],
        },
        {
            "id": "QgdvSXqnN30",
            "title": "Spin ENGLISH COVER | JJBA: Steel Ball Run",
            "result_type": "video",
            "duration": 92,
            "artists": ["ChaseD_Ih and 4 more"],
        },
    ]
    search = MagicMock(return_value=(group for group in [candidates]))
    monkeypatch.setattr(app, "iter_youtube_music_results", search)
    try:
        assert list(
            scraper._iter_youtube_music_matches(
                "SPIN English cover", "", 87.516, "", None, is_episode=True
            )
        ) == ["https://www.youtube.com/watch?v=QgdvSXqnN30"]
        assert search.call_args.args == ("SPIN English cover", "")
    finally:
        scraper.close()


@pytest.mark.parametrize("fmt,quality", [("mp3", "192"), ("flac", "320")])
def test_direct_episode_download_keeps_format_settings_and_skips_youtube(
    monkeypatch, tmp_path, fmt, quality
):
    scraper = app.MusicScraper(audio_format=fmt, audio_quality=quality, sample_rate="44100")
    scraper._network_blocked = True  # A YouTube gate must not block public podcast audio.
    monkeypatch.setattr(app, "get_ffmpeg_path", lambda: "ffmpeg")
    youtube = MagicMock(side_effect=AssertionError("Unexpected music search or browser cookies"))
    monkeypatch.setattr(scraper, "_youtube_dl", youtube)
    destination = tmp_path / f"episode.{fmt}"
    options = []
    ydl = MagicMock()

    def build(opts):
        options.append(opts)
        return ydl

    def download(_url, download):
        assert download is True
        destination.write_bytes(b"full audio")

    ydl.__enter__.return_value.extract_info.side_effect = download
    monkeypatch.setattr(app, "YoutubeDL", build)
    try:
        assert scraper.download_track_audio(
            "unused", str(destination), source_url=FULL_AUDIO_URL
        ) == str(destination)
        ydl.__enter__.return_value.extract_info.assert_called_once_with(
            FULL_AUDIO_URL, download=True
        )
        pp = options[0]["postprocessors"][0]
        assert pp["preferredcodec"] == fmt
        if fmt == "mp3":
            assert pp["preferredquality"] == quality
        else:
            assert "preferredquality" not in pp
        assert options[0]["postprocessor_args"] == {"extractaudio": ["-ar", "44100"]}
        assert "cookiesfrombrowser" not in options[0]
        assert "cookiefile" not in options[0]
        youtube.assert_not_called()
        scraper._cancel_event.set()
        with pytest.raises(InterruptedError):
            options[0]["progress_hooks"][0]({"status": "downloading"})
    finally:
        scraper.close()


def test_direct_episode_download_without_a_file_fails(monkeypatch, tmp_path):
    scraper = app.MusicScraper()
    monkeypatch.setattr(app, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(app, "YoutubeDL", MagicMock())
    try:
        with pytest.raises(RuntimeError, match="Episode audio download produced no file"):
            scraper.download_track_audio(
                "unused", str(tmp_path / "episode.mp3"), source_url=FULL_AUDIO_URL
            )
    finally:
        scraper.close()


def test_cli_episode_info(episode_client, monkeypatch, capsys):
    api = episode_client._embed_api
    monkeypatch.setattr(cli, "SpotifyEmbedAPI", lambda: api)
    args = cli.build_parser().parse_args(["info", SHARED_URL, "--json"])
    assert cli.cmd_info(args) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "episode"
    assert payload["id"] == EPISODE_ID
    assert payload["title"] == "SPIN English cover"
    assert payload["album"] == "Chilling in the basement"
    assert payload["duration_ms"] == 87516


def test_cli_episode_download_dispatch(monkeypatch, tmp_path, capsys):
    scraper = app.MusicScraper()
    single = MagicMock()
    collection = MagicMock()
    monkeypatch.setattr(scraper, "scrape_track", single)
    monkeypatch.setattr(scraper, "scrape_playlist", collection)
    monkeypatch.setattr(app, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(cli, "_build_scraper", lambda *_a, **_kw: scraper)
    args = cli.build_parser().parse_args(["download", SHARED_URL, "--out", str(tmp_path), "--json"])
    assert cli.cmd_download(args) == cli.EXIT_OK
    single.assert_called_once_with(SHARED_URL, str(tmp_path))
    collection.assert_not_called()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "episode"
    assert events[-1]["exit_code"] == cli.EXIT_OK
