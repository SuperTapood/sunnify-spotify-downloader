"""Tests for Flask backend app."""

from __future__ import annotations

# Check if Flask is installed
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FLASK_AVAILABLE = all(
    importlib.util.find_spec(module) is not None for module in ("flask", "flask_cors")
)

# Add backend directory for imports
ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT / "web-app" / "sunnify-backend"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Skip all tests in this module if Flask is not installed
pytestmark = pytest.mark.skipif(
    not FLASK_AVAILABLE,
    reason="Flask not installed (web backend tests require separate environment)",
)


@pytest.fixture
def app():
    """Create Flask test app."""
    # Import here to avoid issues with path setup
    from app import app as flask_app

    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    """Create Flask test client."""
    return app.test_client()


class TestHealthEndpoint:
    """Tests for /api/health endpoint."""

    def test_health_returns_200(self, client):
        """Health endpoint should return 200."""
        response = client.get("/api/health")
        assert response.status_code == 200

    def test_health_returns_ok_status(self, client):
        """Health endpoint should return ok status."""
        response = client.get("/api/health")
        data = response.get_json()
        assert data["status"] == "ok"
        assert data["mode"] == "metadata-only"


class TestRootEndpoint:
    """Tests for / endpoint."""

    def test_root_returns_api_info(self, client):
        """Root endpoint should return API info."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.get_json()
        assert data["name"] == "Sunnify API"
        assert "endpoints" in data


class TestScrapePlaylistEndpoint:
    """Tests for /api/scrape-playlist endpoint."""

    def test_missing_url_returns_400(self, client):
        """Missing URL should return 400."""
        response = client.post(
            "/api/scrape-playlist",
            json={},
            content_type="application/json",
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data["event"] == "error"
        assert "No URL" in data["data"]["message"]

    def test_empty_url_returns_400(self, client):
        """Empty URL should return 400."""
        response = client.post(
            "/api/scrape-playlist",
            json={"playlistUrl": ""},
            content_type="application/json",
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("payload", [None, [], "not-an-object", {"playlistUrl": 123}])
    def test_invalid_json_shape_returns_400(self, client, payload):
        response = client.post("/api/scrape-playlist", json=payload)

        assert response.status_code == 400
        assert response.get_json()["event"] == "error"

    def test_oversized_body_is_rejected(self, client):
        response = client.post(
            "/api/scrape-playlist",
            json={"playlistUrl": "x" * (17 * 1024)},
        )

        assert response.status_code == 413

    def test_invalid_url_returns_400(self, client):
        """Invalid Spotify URL should return 400."""
        response = client.post(
            "/api/scrape-playlist",
            json={"playlistUrl": "https://example.com/invalid"},
            content_type="application/json",
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data["event"] == "error"

    def test_malformed_json_returns_400(self, client):
        response = client.post(
            "/api/scrape-playlist",
            data="{not-json",
            content_type="application/json",
        )
        assert response.status_code == 400
        assert response.get_json()["event"] == "error"

    def test_non_object_json_returns_400(self, client):
        response = client.post(
            "/api/scrape-playlist",
            json=["https://open.spotify.com/playlist/abc123"],
        )
        assert response.status_code == 400
        assert response.get_json()["data"]["message"] == "Invalid request body"

    @pytest.mark.parametrize("value", [123, None, ["https://open.spotify.com/playlist/abc123"]])
    def test_non_string_url_returns_400(self, client, value):
        response = client.post("/api/scrape-playlist", json={"playlistUrl": value})
        assert response.status_code == 400
        assert "No URL" in response.get_json()["data"]["message"]

    @patch("app.get_playlist_client")
    def test_valid_playlist_url(self, mock_get_client, client):
        """Valid playlist URL should return track data."""
        # Create mock client
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        # Mock playlist metadata
        mock_metadata = MagicMock()
        mock_metadata.name = "Test Playlist"
        mock_metadata.owner = "Test User"
        mock_metadata.cover_url = "https://example.com/cover.jpg"
        mock_client.get_playlist_metadata.return_value = mock_metadata

        # Mock track iteration
        mock_track = MagicMock()
        mock_track.spotify_id = "abc123"
        mock_track.title = "Test Song"
        mock_track.artists = "Test Artist"
        mock_track.album = "Test Album"
        mock_track.cover_url = None
        mock_track.release_date = "2024-01-01"
        mock_client.iter_playlist_tracks.return_value = [mock_track]

        response = client.post(
            "/api/scrape-playlist",
            json={"playlistUrl": "https://open.spotify.com/playlist/abc123"},
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["event"] == "complete"
        assert data["data"]["playlistName"] == "Test Playlist - Test User"
        assert len(data["data"]["tracks"]) == 1
        assert data["data"]["tracks"][0]["title"] == "Test Song"

    @patch("app.get_playlist_client")
    def test_artist_url_routes_to_full_discography(self, mock_get_client, client):
        from spotifydown_api import PlaylistInfo, TrackInfo

        api = mock_get_client.return_value
        api.get_playlist_metadata.return_value = PlaylistInfo(
            name="Artist - Discography", owner=None, description=None, cover_url=None, track_count=1
        )
        api.iter_playlist_tracks.return_value = [
            TrackInfo(
                id="song",
                title="Song",
                artists="Artist",
                album="Album",
                release_date="2024",
                cover_url="https://example.com/album.jpg",
                duration_ms=180000,
                preview_url=None,
                raw={},
            )
        ]
        response = client.post(
            "/api/scrape-playlist",
            json={"playlistUrl": "https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb"},
        )
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["playlistName"] == "Artist - Discography"
        assert data["tracks"][0]["album"] == "Album"
        assert data["tracks"][0]["cover"] == "https://example.com/album.jpg"
        api.get_playlist_metadata.assert_called_once_with(
            "4Z8W4fKeB5YxbusRsdQVPb", content_type="artist"
        )
        api.iter_playlist_tracks.assert_called_once_with(
            "4Z8W4fKeB5YxbusRsdQVPb", content_type="artist"
        )

    @patch("app.get_playlist_client")
    def test_valid_track_url(self, mock_get_client, client):
        """Single tracks reuse the shared client instead of opening a new session."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        # Mock track data
        mock_track = MagicMock()
        mock_track.spotify_id = "xyz789"
        mock_track.title = "Single Track"
        mock_track.artists = "Solo Artist"
        mock_track.album = "Solo Album"
        mock_track.cover_url = "https://example.com/track-cover.jpg"
        mock_track.release_date = "2024-06-15"
        mock_client.get_track.return_value = mock_track

        response = client.post(
            "/api/scrape-playlist",
            json={"playlistUrl": "https://open.spotify.com/track/xyz789"},
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["event"] == "complete"
        assert len(data["data"]["tracks"]) == 1
        assert data["data"]["tracks"][0]["title"] == "Single Track"
        mock_get_client.assert_called_once_with()
        mock_client.get_track.assert_called_once_with("xyz789")
        mock_client.get_playlist_metadata.assert_not_called()
        mock_client.iter_playlist_tracks.assert_not_called()

    @patch("app.get_playlist_client")
    def test_upstream_value_error_returns_sanitized_500(self, mock_get_client, client):
        mock_get_playlist_client = MagicMock()
        mock_get_playlist_client.get_track.side_effect = ValueError("private parser detail")
        mock_get_client.return_value = mock_get_playlist_client

        response = client.post(
            "/api/scrape-playlist",
            json={"playlistUrl": "https://open.spotify.com/track/xyz789"},
        )

        assert response.status_code == 500
        assert response.get_json() == {
            "event": "error",
            "data": {"message": "Internal server error"},
        }


class TestMultipleUrls:
    @pytest.mark.parametrize(
        "payload",
        [
            {"playlistUrls": ["spotify:artist:artist", "spotify:track:song"]},
            {"playlistUrl": "spotify:artist:artist\nspotify:track:song"},
        ],
    )
    @patch("app.get_playlist_client")
    @patch("app._fetch_collection")
    def test_batch_combines_results_and_deduplicates_tracks(
        self, fetch, get_client, client, payload
    ):
        fetch.side_effect = [
            ("Artist - Discography", [{"id": "shared"}, {"id": "artist-song"}]),
            ("Song", [{"id": "shared"}]),
        ]
        response = client.post("/api/scrape-playlist", json=payload)
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["playlistName"] == "2 of 2 URLs"
        assert data["tracks"] == [{"id": "shared"}, {"id": "artist-song"}]
        assert data["errors"] == []
        assert [call.args[1] for call in fetch.call_args_list] == [
            "spotify:artist:artist",
            "spotify:track:song",
        ]
        get_client.assert_called_once_with()

    @patch("app.get_playlist_client")
    @patch("app._fetch_collection")
    def test_batch_retains_successes_and_sanitizes_errors(self, fetch, get_client, client):
        from spotifydown_api import SpotifyDownAPIError

        fetch.side_effect = [SpotifyDownAPIError("private detail"), ("Song", [{"id": "song"}])]
        response = client.post(
            "/api/scrape-playlist",
            json={"playlistUrls": ["spotify:artist:bad", "spotify:track:song"]},
        )
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["tracks"] == [{"id": "song"}]
        assert data["errors"] == [{"url": "spotify:artist:bad", "message": "Spotify API error"}]
        assert "private detail" not in response.get_data(as_text=True)

    @pytest.mark.parametrize(
        "urls", [["spotify:track:song", "invalid"], ["spotify:track:song", None], [], 123]
    )
    @patch("app.get_playlist_client")
    def test_invalid_batch_does_not_fetch_any_url(self, get_client, client, urls):
        response = client.post("/api/scrape-playlist", json={"playlistUrls": urls})
        assert response.status_code == 400
        get_client.assert_not_called()

    @patch("app.get_playlist_client")
    @patch("app._fetch_collection")
    def test_duplicate_resources_are_only_fetched_once(self, fetch, get_client, client):
        fetch.return_value = ("Song", [{"id": "song"}])
        response = client.post(
            "/api/scrape-playlist",
            json={
                "playlistUrls": [
                    "spotify:track:song",
                    "https://open.spotify.com/track/song?si=shared",
                ]
            },
        )
        assert response.status_code == 200
        assert fetch.call_count == 1
        assert response.get_json()["data"] == {"playlistName": "Song", "tracks": [{"id": "song"}]}


class TestCORS:
    """Tests for CORS configuration."""

    def test_cors_headers_present(self, client):
        """CORS headers should be present on responses."""
        response = client.get(
            "/api/health",
            headers={"Origin": "http://localhost:3000"},
        )

        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"

    def test_options_preflight(self, client):
        """OPTIONS preflight request should work."""
        response = client.options(
            "/api/scrape-playlist",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )

        assert response.status_code in (200, 204)
        assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
        assert "POST" in response.headers["Access-Control-Allow-Methods"]
