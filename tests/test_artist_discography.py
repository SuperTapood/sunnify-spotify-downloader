"""Full artist catalogues must never fall back to the embed's top ten tracks."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from spotifydown_api import (
    ContentUnavailableError,
    ExtractionError,
    PlaylistClient,
    RateLimitError,
    SpotifyEmbedAPI,
    _gid_to_spotify_id,
    _spotify_id_to_gid,
    detect_spotify_url_type,
)

ARTIST_ID = "4Z8W4fKeB5YxbusRsdQVPb"
ARTIST_URL = f"https://open.spotify.com/artist/{ARTIST_ID}"


def gid(number):
    return f"{number:032x}"


def sid(number):
    return _gid_to_spotify_id(gid(number))


@pytest.mark.parametrize(
    "url",
    [
        ARTIST_URL,
        ARTIST_URL + "?si=shared",
        ARTIST_URL.replace("/artist/", "/intl-he/artist/"),
        f"spotify:artist:{ARTIST_ID}",
    ],
)
def test_detect_artist_url(url):
    assert detect_spotify_url_type(url) == ("artist", ARTIST_ID)


def test_catalogue_id_conversion():
    assert _spotify_id_to_gid(ARTIST_ID) == "a3d4a9be89174457bdd11d2ebd9aaf39"
    assert _gid_to_spotify_id("a3d4a9be89174457bdd11d2ebd9aaf39") == ARTIST_ID
    assert _spotify_id_to_gid(sid(1)) == gid(1)


@pytest.mark.parametrize("value", [None, "", "xyz", "1" * 33])
def test_rejects_malformed_catalogue_gid(value):
    with pytest.raises(ExtractionError):
        _gid_to_spotify_id(value)


@pytest.mark.parametrize("value", ["short", "/" * 22, "z" * 22])
def test_rejects_malformed_catalogue_id(value):
    with pytest.raises(ExtractionError):
        _spotify_id_to_gid(value)


@pytest.fixture
def catalogue(monkeypatch):
    client = PlaylistClient()
    api = client._embed_api
    calls = []
    artist = {
        "name": "Test Artist",
        "album_group": [{"album": [{"gid": gid(1)}, {"gid": gid(1)}]}],
        "single_group": [{"album": [{"gid": gid(2)}]}],
        "compilation_group": [{"album": [{"gid": gid(3)}]}],
        "appears_on_group": [{"album": [{"gid": gid(999)}]}],
    }

    def album(name, discs):
        return {
            "name": name,
            "date": {"year": 2024, "month": 2, "day": 3},
            "cover_group": {
                "image": [{"file_id": "small", "width": 64}, {"file_id": "large", "width": 640}]
            },
            "disc": [
                {"number": idx, "track": [{"gid": gid(n)} for n in tracks]}
                for idx, tracks in enumerate(discs, 1)
            ],
        }

    albums = {
        sid(1): album("Long Album", [range(1000, 1100), [1100, 1101]]),
        sid(2): album("Single / EP", [[1000, 1102]]),
        sid(3): album("Compilation", [[1102, 1103]]),
    }

    def fetch(kind, item_id, artist_id):
        assert artist_id == ARTIST_ID
        calls.append((kind, item_id))
        if kind == "artist":
            return artist
        if kind == "album":
            return albums[item_id]
        return {
            "name": f"Song {item_id}",
            "artist": [{"name": "Test Artist"}, {"name": "Guest"}],
            "duration": 180000,
        }

    monkeypatch.setattr(api, "_fetch_catalog_metadata", fetch)
    yield SimpleNamespace(client=client, api=api, calls=calls, artist=artist, albums=albums)
    client.close()


def test_complete_discography_deduplicates_and_keeps_tags(catalogue):
    metadata = catalogue.client.get_playlist_metadata(ARTIST_ID, content_type="artist")
    assert metadata.name == "Test Artist - Discography"
    assert metadata.track_count == 104
    tracks = list(catalogue.client.iter_playlist_tracks(ARTIST_ID, content_type="artist"))
    assert [t.id for t in tracks] == [sid(n) for n in range(1000, 1104)]
    assert [t.position for t in tracks] == list(range(1, 105))
    assert tracks[101].album == "Long Album"  # second disc, beyond embed limits
    assert tracks[102].album == "Single / EP"
    assert tracks[103].album == "Compilation"
    assert all(t.artists == "Test Artist, Guest" for t in tracks)
    assert all(t.cover_url == "https://i.scdn.co/image/large" for t in tracks)
    assert all(t.release_date == "2024-02-03" for t in tracks)
    assert all(t.duration_ms == 180000 for t in tracks)
    assert catalogue.calls.count(("artist", ARTIST_ID)) == 1  # reuse metadata snapshot
    assert sorted(item_id for kind, item_id in catalogue.calls if kind == "album") == [
        sid(1),
        sid(2),
        sid(3),
    ]


def test_resume_skips_requests_and_preserves_positions(catalogue):
    skip_ids = {sid(n) for n in range(1000, 1103)}
    tracks = list(
        catalogue.client.iter_playlist_tracks(ARTIST_ID, content_type="artist", skip_ids=skip_ids)
    )
    assert len(tracks) == 1
    assert tracks[0].id == sid(1103)
    assert tracks[0].position == 104
    assert [item_id for kind, item_id in catalogue.calls if kind == "track"] == [sid(1103)]


def test_missing_disc_listing_fails_instead_of_truncating(catalogue):
    catalogue.albums[sid(2)].pop("disc")
    with pytest.raises(ExtractionError, match="no disc listing"):
        catalogue.client.get_playlist_metadata(ARTIST_ID, content_type="artist")


def test_malformed_release_group_fails(catalogue):
    catalogue.artist["single_group"] = {}
    with pytest.raises(ExtractionError, match="single_group"):
        catalogue.client.get_playlist_metadata(ARTIST_ID, content_type="artist")


def test_empty_artist(catalogue):
    catalogue.artist.clear()
    catalogue.artist["name"] = "New Artist"
    metadata = catalogue.client.get_playlist_metadata(ARTIST_ID, content_type="artist")
    assert metadata.track_count == 0
    assert list(catalogue.client.iter_playlist_tracks(ARTIST_ID, content_type="artist")) == []


def test_track_failure_is_not_reported_as_a_complete_catalogue(catalogue, monkeypatch):
    monkeypatch.setattr(
        catalogue.api,
        "_artist_track",
        MagicMock(side_effect=ContentUnavailableError("unavailable")),
    )
    with pytest.raises(ContentUnavailableError):
        list(catalogue.client.iter_playlist_tracks(ARTIST_ID, content_type="artist"))


@pytest.fixture
def catalog_session():
    session = MagicMock()
    api = SpotifyEmbedAPI(session=session)
    api._cached_token = "test-token"
    api._token_expiry = time.time() + 3600
    yield api, session
    api.close()


def test_catalogue_request_and_expired_session_refresh(catalog_session, monkeypatch):
    api, session = catalog_session
    session.get.side_effect = [
        MagicMock(status_code=401),
        MagicMock(status_code=200, json=lambda: {"name": "Artist"}),
    ]
    embed = MagicMock()
    monkeypatch.setattr(api, "_fetch_embed_data", embed)
    assert api._fetch_catalog_metadata("artist", ARTIST_ID, ARTIST_ID)["name"] == "Artist"
    embed.assert_called_once_with(f"https://open.spotify.com/embed/artist/{ARTIST_ID}")
    args, kwargs = session.get.call_args
    assert args[0].endswith("/artist/a3d4a9be89174457bdd11d2ebd9aaf39")
    assert kwargs["params"] == {"market": "from_token"}
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"


@pytest.mark.parametrize("status", [403, 404])
def test_catalogue_unavailable(catalog_session, status):
    api, session = catalog_session
    session.get.return_value.status_code = status
    with pytest.raises(ContentUnavailableError):
        api._fetch_catalog_metadata("artist", ARTIST_ID, ARTIST_ID)
    assert session.get.call_count == 1


def test_catalogue_rate_limit_is_bounded(catalog_session, monkeypatch):
    api, session = catalog_session
    monkeypatch.setattr("spotifydown_api.time.sleep", lambda _: None)
    session.get.return_value.status_code = 429
    with pytest.raises(RateLimitError):
        api._fetch_catalog_metadata("artist", ARTIST_ID, ARTIST_ID)
    assert session.get.call_count == 3


def test_catalogue_retries_connection_error(catalog_session, monkeypatch):
    api, session = catalog_session
    monkeypatch.setattr("spotifydown_api.time.sleep", lambda _: None)
    session.get.side_effect = [
        requests.ConnectionError(),
        MagicMock(status_code=200, json=lambda: {"name": "Artist"}),
    ]
    assert api._fetch_catalog_metadata("artist", ARTIST_ID, ARTIST_ID)["name"] == "Artist"
    assert session.get.call_count == 2


@pytest.mark.parametrize("payload", [[], {}, {"error": "unavailable"}])
def test_catalogue_rejects_malformed_payload(catalog_session, payload):
    api, session = catalog_session
    session.get.return_value.status_code = 200
    session.get.return_value.json.return_value = payload
    with pytest.raises(ExtractionError):
        api._fetch_catalog_metadata("artist", ARTIST_ID, ARTIST_ID)


def test_desktop_artist_download_uses_resume_and_collection_folder(
    catalogue, monkeypatch, tmp_path
):
    from Spotify_Downloader import MusicScraper

    scraper = MusicScraper()
    scraper.MAX_WORKERS = 1
    monkeypatch.setattr(scraper, "ensure_spotifydown_api", lambda: catalogue.client)
    monkeypatch.setattr(scraper, "_load_manifest", lambda _: {sid(n) for n in range(1000, 1103)})
    download = MagicMock()
    monkeypatch.setattr(scraper, "_download_one_track", download)
    scraper.scrape_playlist(ARTIST_URL, str(tmp_path))
    args, kwargs = download.call_args
    assert download.call_count == 1
    assert args[0].id == sid(1103)
    assert args[1] == str(tmp_path / "Test Artist - Discography")
    assert kwargs["track_num"] == 104


def test_cli_artist_info(catalogue, monkeypatch, capsys):
    import sunnify_cli

    monkeypatch.setattr(sunnify_cli, "PlaylistClient", lambda: catalogue.client)
    result = sunnify_cli.cmd_info(SimpleNamespace(url=ARTIST_URL, json=True))
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["type"] == "artist"
    assert output["name"] == "Test Artist - Discography"
    assert output["track_count"] == 104


def test_cli_artist_download_dispatch(monkeypatch, tmp_path, capsys):
    import Spotify_Downloader as app
    import sunnify_cli

    scraper = app.MusicScraper()
    monkeypatch.setattr(scraper, "scrape_playlist", MagicMock())
    monkeypatch.setattr(scraper, "scrape_track", MagicMock())
    monkeypatch.setattr(app, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(sunnify_cli, "_build_scraper", lambda *_a, **_kw: scraper)
    args = sunnify_cli.build_parser().parse_args(
        ["download", ARTIST_URL, "--out", str(tmp_path), "--json"]
    )
    assert sunnify_cli.cmd_download(args) == 0
    scraper.scrape_playlist.assert_called_once_with(ARTIST_URL, str(tmp_path))
    scraper.scrape_track.assert_not_called()
    capsys.readouterr()
