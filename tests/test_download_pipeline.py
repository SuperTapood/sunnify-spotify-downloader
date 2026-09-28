"""Queue accounting, preview races, bounded prefetch, and audio reuse."""

from __future__ import annotations

import concurrent.futures
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QColor, QImage

import Spotify_Downloader as app
from spotifydown_api import PlaylistInfo, SpotifyEmbedAPI, TrackInfo


def track(tid):
    return TrackInfo(
        id=tid,
        title=tid,
        artists="Artist",
        album="Album",
        release_date="2026",
        cover_url="https://example.com/cover.jpg",
        duration_ms=1000,
        preview_url=None,
        raw={},
    )


def test_queue_counts_skips_failures_and_reuses_overlapping_tracks(tmp_path, monkeypatch):
    scraper = app.MusicScraper()
    sources = {
        "one": [track("shared"), track("bad"), track("existing")],
        "two": [track("shared"), track("new")],
    }
    api = MagicMock()
    api.get_playlist_metadata.side_effect = lambda pid, **_: PlaylistInfo(
        pid, "Owner", None, None, len(sources[pid])
    )
    api.iter_playlist_tracks.side_effect = lambda pid, skip_ids, **_: (
        t for t in sources[pid] if t.id not in skip_ids
    )
    monkeypatch.setattr(scraper, "ensure_spotifydown_api", lambda: api)
    folder = tmp_path / "one - Owner"
    folder.mkdir()
    (folder / "existing.mp3").write_bytes(b"existing audio")
    (folder / app.MANIFEST_FILENAME).write_text(
        json.dumps({"id": "existing", "file": "existing.mp3"}) + "\n"
    )
    downloads = []

    def download(_query, destination, **kwargs):
        downloads.append(kwargs["expected_title"])
        if kwargs["expected_title"] == "bad":
            raise RuntimeError("test failure")
        Path(destination).write_bytes(b"audio")
        return destination

    monkeypatch.setattr(scraper, "download_track_audio", download)
    snapshots = []
    scraper.progress_snapshot.connect(snapshots.append, type=Qt.ConnectionType.DirectConnection)
    scraper.begin_queue(2)
    for index, pid in enumerate(sources, 1):
        scraper.begin_url(index)
        scraper.scrape_playlist(f"spotify:playlist:{pid}", str(tmp_path))
    assert sorted(downloads) == ["bad", "new", "shared"]
    assert snapshots[-1] == {
        "revision": snapshots[-1]["revision"],
        "url_index": 2,
        "url_count": 2,
        "downloaded": 3,
        "skipped": 1,
        "failed": 1,
        "reused": 1,
        "processed": 2,
        "total": 2,
    }
    assert [s["downloaded"] for s in snapshots] == sorted(s["downloaded"] for s in snapshots)
    assert [s["revision"] for s in snapshots] == sorted(s["revision"] for s in snapshots)
    first = tmp_path / "one - Owner" / "shared - Artist.mp3"
    second = tmp_path / "two - Owner" / "shared - Artist.mp3"
    assert first.read_bytes() == second.read_bytes() == b"audio"
    second.write_bytes(b"retagged")
    assert first.read_bytes() == b"audio"  # copies must never be hard links
    scraper.close()


def test_parallel_outcome_snapshots_are_atomic():
    scraper = app.MusicScraper()
    snapshots = []
    scraper.progress_snapshot.connect(snapshots.append, type=Qt.ConnectionType.DirectConnection)
    scraper.begin_queue(1)
    scraper._set_total_tracks(120)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(scraper.increment_counter, ["downloaded", "skipped", "failed"] * 40))
    assert snapshots[-1]["downloaded"] == snapshots[-1]["skipped"] == snapshots[-1]["failed"] == 40
    assert all(s["processed"] == s["downloaded"] + s["skipped"] + s["failed"] for s in snapshots)
    assert [s["revision"] for s in snapshots] == sorted(s["revision"] for s in snapshots)
    scraper.close()


def test_large_queue_keeps_metadata_bounded_and_closes_it_on_stop(tmp_path, monkeypatch):
    scraper = app.MusicScraper(download_workers="4")
    api = MagicMock()
    api.get_playlist_metadata.return_value = PlaylistInfo("Large", "Owner", None, None, 10000)
    seen = []
    queue_full = threading.Event()
    closed = threading.Event()

    def tracks(*_args, **_kwargs):
        try:
            for index in range(10000):
                seen.append(index)
                if len(seen) == 9:
                    queue_full.set()
                yield track(str(index))
        finally:
            closed.set()

    def download(*_args, **_kwargs):
        assert scraper._cancel_event.wait(timeout=5)

    api.iter_playlist_tracks.side_effect = tracks
    monkeypatch.setattr(scraper, "ensure_spotifydown_api", lambda: api)
    monkeypatch.setattr(scraper, "_download_one_track", download)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as runner:
            result = runner.submit(scraper.scrape_playlist, "spotify:playlist:id", str(tmp_path))
            try:
                assert queue_full.wait(timeout=3)
                assert len(seen) == 9
            finally:
                scraper._cancel_event.set()
            result.result(timeout=5)
        assert len(seen) == 9
        assert closed.is_set()
        assert scraper._failed_tracks == []
    finally:
        scraper.close()


def test_reuse_respects_format_settings_and_deleted_files(tmp_path):
    scraper = app.MusicScraper()
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    scraper._remember_audio("id", str(source))
    scraper.audio_quality = "320"
    assert not scraper._reuse_audio("id", str(tmp_path / "different-quality.mp3"))
    scraper.audio_quality = "192"
    source.unlink()
    assert not scraper._reuse_audio("id", str(tmp_path / "missing-source.mp3"))
    scraper.close()


def test_existing_audio_with_unknown_quality_is_not_reused_for_another_url(tmp_path, monkeypatch):
    scraper = app.MusicScraper(audio_quality="320")
    api = MagicMock()
    api.get_track.return_value = track("shared")
    api.get_playlist_metadata.return_value = PlaylistInfo("New", "Owner", None, None, 1)
    api.iter_playlist_tracks.return_value = iter([track("shared")])
    monkeypatch.setattr(scraper, "ensure_spotifydown_api", lambda: api)
    (tmp_path / "shared - Artist.mp3").write_bytes(b"older audio of unknown quality")

    def download(_query, destination, **_kwargs):
        Path(destination).write_bytes(b"new 320 kbps audio")
        return destination

    fetch = MagicMock(side_effect=download)
    monkeypatch.setattr(scraper, "download_track_audio", fetch)
    scraper.begin_queue(2)
    scraper.begin_url(1)
    scraper.scrape_track("spotify:track:shared", str(tmp_path))
    scraper.begin_url(2)
    scraper.scrape_playlist("spotify:playlist:new", str(tmp_path))
    fetch.assert_called_once()
    assert (tmp_path / "New - Owner" / "shared - Artist.mp3").read_bytes() == b"new 320 kbps audio"
    scraper.close()


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr(app.UpdateCheckThread, "start", lambda _: None)
    monkeypatch.setattr(app.DownloadThumbnail, "start", lambda _: None)
    win = app.MainWindow()
    win._config["star_prompt_shown"] = True
    yield win
    win.deleteLater()


def cover(color):
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def meta(index, url, *, source=1):
    return {
        "_preview_id": index,
        "_url_index": source,
        "title": f"Song {index}",
        "artists": "Artist",
        "album": "Album",
        "releaseDate": "2026",
        "cover": url,
    }


def test_late_artwork_cannot_replace_current_song_cover(window):
    window.showPreviewCheck.setChecked(True)
    window.update_song_META(meta(1, "first"))
    first = window._active_threads[-1]
    window.update_song_META(meta(2, "second"))
    second = window._active_threads[-1]
    second.thumbnail_ready.emit(cover("blue"))
    first.thumbnail_ready.emit(cover("red"))
    assert window.SongName.text() == "Song 2"
    assert window.CoverImg.pixmap().toImage().pixelColor(0, 0) == QColor("blue")
    window.update_song_META(meta(1, "first"))
    assert window.SongName.text() == "Song 2"


def test_new_source_clears_old_preview_and_rejects_old_events(window):
    window.update_song_META(meta(1, "first"))
    window.apply_preview_cover("first", cover("red"))
    window.preview_source_started(2, 3)
    window.update_song_META(meta(2, "old-source", source=1))
    window.apply_preview_cover("first", cover("red"))
    assert window.SongName.text() == ""
    assert window.CoverImg.pixmap().isNull()


def test_missing_cover_clears_artwork_and_hidden_preview_keeps_text_current(window):
    window.update_song_META(meta(1, "first"))
    window.apply_preview_cover("first", cover("red"))
    window.update_song_META(meta(2, ""))
    assert window.CoverImg.pixmap().isNull()
    assert window.SongName.text() == "Song 2"
    assert window.AlbumText.text() == "Album"


def test_thumbnail_pool_deduplicates_and_eventually_loads_newest_cover(window):
    window.showPreviewCheck.setChecked(True)
    for index in range(1, 6):
        window.update_song_META(meta(index, "same"))
    active = [t for t in window._active_threads if isinstance(t, app.DownloadThumbnail)]
    assert len(active) == 1
    for index in range(6, 12):
        window.update_song_META(meta(index, f"cover-{index}"))
    active = [t for t in window._active_threads if isinstance(t, app.DownloadThumbnail)]
    assert len(active) == app._MAX_THUMBNAIL_THREADS
    assert not any(t.url == "cover-11" for t in active)
    window._cleanup_thread(active[0])
    assert any(
        isinstance(t, app.DownloadThumbnail) and t.url == "cover-11" for t in window._active_threads
    )


def test_ui_uses_queue_snapshot_not_mutable_scraper_totals(window):
    snapshot = {
        "revision": 10,
        "url_index": 2,
        "url_count": 3,
        "downloaded": 8,
        "skipped": 2,
        "failed": 1,
        "reused": 3,
        "processed": 4,
        "total": 8,
    }
    window.update_queue_progress(snapshot)
    assert "8 saved" in window.CounterLabel.text()
    assert "2 skipped" in window.CounterLabel.text()
    assert "1 failed" in window.CounterLabel.text()
    assert "URL 2/3" in window.CounterLabel.text()
    assert window.SongDownloadprogress.value() == 50
    window.update_queue_progress({**snapshot, "revision": 9, "downloaded": 1})
    assert "8 saved" in window.CounterLabel.text()


def test_log_button_opens_exact_file_in_notepad(window, monkeypatch, tmp_path):
    import subprocess

    path = tmp_path / "logs with spaces" / "sunnify.log"
    monkeypatch.setattr(app, "log_file_path", lambda: str(path))
    monkeypatch.setattr(app, "sys", SimpleNamespace(platform="win32"))
    launch = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", launch)
    window.OpenLogsBtn.click()
    assert path.is_file()
    launch.assert_called_once_with(["notepad.exe", str(path)])


def test_log_button_reports_notepad_launch_failure(window, monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(app, "log_file_path", lambda: str(tmp_path / "sunnify.log"))
    monkeypatch.setattr(app, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(subprocess, "Popen", MagicMock(side_effect=OSError("Notepad unavailable")))
    window.OpenLogsBtn.click()
    assert "Could not open logs" in window.statusMsg.text()


def test_metadata_prefetch_starts_next_work_before_slow_first_result():
    api = SpotifyEmbedAPI()
    first_release = threading.Event()
    beyond_first_batch = threading.Event()

    def fetch(index):
        if index == 0:
            assert first_release.wait(timeout=5)
        if index == 4:
            beyond_first_batch.set()
        return index

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as consumer:
            result = consumer.submit(lambda: list(api._map_metadata(fetch, range(12))))
            try:
                assert beyond_first_batch.wait(timeout=3)
            finally:
                first_release.set()
            assert result.result(timeout=5) == list(range(12))
    finally:
        api.close()


def test_track_cache_keeps_positions_independent_and_expires(monkeypatch):
    api = SpotifyEmbedAPI()
    fetch = MagicMock(return_value=track("id"))
    monkeypatch.setattr(api, "_fetch_track_metadata", fetch)
    now = [100.0]
    monkeypatch.setattr("spotifydown_api.time.monotonic", lambda: now[0])
    first = api.get_track("id")
    first.position = 99
    first.raw["mutated"] = True
    second = api.get_track("id")
    assert second.position is None
    assert second.raw == {}
    assert fetch.call_count == 1
    now[0] += 901
    api.get_track("id")
    assert fetch.call_count == 2
    api.close()


def test_large_playlist_metadata_prefetch_is_bounded_and_reuses_cache(monkeypatch):
    api = SpotifyEmbedAPI()
    api._cached_token = "test-token"
    monkeypatch.setattr(api, "_fetch_embed_data", lambda _: {})
    monkeypatch.setattr(api, "_extract_entity", lambda _: {"trackList": []})
    monkeypatch.setattr(
        api,
        "_fetch_spclient_data",
        lambda *_args, **_kwargs: {
            "length": 10000,
            "contents": {"items": [{"uri": f"spotify:track:{i}"} for i in range(10000)]},
        },
    )
    fetch = MagicMock(side_effect=track)
    monkeypatch.setattr(api, "_fetch_track_metadata", fetch)
    tracks = api.iter_playlist_tracks("id")
    try:
        first = next(tracks)
        assert first.position == int(first.id) + 1
        api.get_track(first.id)
        assert sum(call.args == (first.id,) for call in fetch.call_args_list) == 1
    finally:
        tracks.close()
        api.close()
    assert fetch.call_count <= 9


def test_cancelled_metadata_does_not_start_requests():
    cancel = threading.Event()
    cancel.set()
    session = MagicMock()
    api = SpotifyEmbedAPI(session=session, cancel_event=cancel)
    with pytest.raises(InterruptedError):
        api._fetch_embed_data("https://example.test/embed")
    session.get.assert_not_called()
    api.close()


def test_download_worker_setting_reaches_engine_and_dialog(qapp):
    scraper = app.MusicScraper(**app.scraper_kwargs_from({"download_workers": "8"}))
    assert scraper.MAX_WORKERS == 8
    dialog = app.SettingsDialog(None, {"download_workers": "6"})
    assert dialog.result_config()["download_workers"] == "6"
    scraper.close()
