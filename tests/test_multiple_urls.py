"""Queue validation, failure reporting, and cancellation across mixed URLs."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import Spotify_Downloader as app
import sunnify_cli as cli
from spotifydown_api import parse_spotify_urls

TRACK = "https://open.spotify.com/track/song"
ALBUM = "https://open.spotify.com/album/album"
ARTIST = "https://open.spotify.com/artist/artist"
PLAYLIST = "https://open.spotify.com/playlist/playlist"


def test_pasted_mixed_urls_preserve_order_and_deduplicate_resources():
    text = f"  {ARTIST}\n{ALBUM}, {TRACK}\t{PLAYLIST}\nspotify:track:song {TRACK}?si=shared"
    assert parse_spotify_urls(text) == [ARTIST, ALBUM, TRACK, PLAYLIST]
    assert parse_spotify_urls([TRACK, TRACK.replace("/track/", "/intl-en/track/")]) == [TRACK]


@pytest.mark.parametrize("value", ["", " , \n ", [], None, [TRACK, 123], {"url": TRACK}])
def test_empty_or_malformed_url_lists_are_rejected(value):
    with pytest.raises(ValueError):
        parse_spotify_urls(value)


@pytest.mark.parametrize(
    "bad_url",
    [
        "oops",
        "https://evil.test/" + TRACK,
        TRACK + "/invalid",
        "https://open.spotify.com/show/show",
    ],
)
def test_invalid_entries_are_not_silently_ignored(bad_url):
    with pytest.raises(ValueError):
        parse_spotify_urls([TRACK, bad_url])


@pytest.fixture
def worker(monkeypatch):
    thread = app.ScraperThread([ARTIST, ALBUM, TRACK, PLAYLIST])
    calls = []
    statuses = []
    thread.progress_update.connect(statuses.append)
    monkeypatch.setattr(thread.scraper, "scrape_track", lambda url, _: calls.append(("track", url)))
    monkeypatch.setattr(
        thread.scraper, "scrape_playlist", lambda url, _: calls.append(("collection", url))
    )
    return thread, calls, statuses


def test_desktop_queue_dispatches_every_url(worker):
    thread, calls, statuses = worker
    thread.run()
    assert calls == [
        ("collection", ARTIST),
        ("collection", ALBUM),
        ("track", TRACK),
        ("collection", PLAYLIST),
    ]
    assert statuses[-1] == "Completed 4 URL(s)."


def test_desktop_queue_continues_after_a_failed_url(worker, monkeypatch):
    thread, calls, statuses = worker

    def download(url, _):
        calls.append(("collection", url))
        if url == ARTIST:
            raise RuntimeError("unavailable")

    monkeypatch.setattr(thread.scraper, "scrape_playlist", download)
    thread.run()
    assert len(calls) == 4
    assert statuses[-1] == "Finished with failures in 1/4 URLs"


def test_desktop_stop_cancels_remaining_urls(worker, monkeypatch):
    thread, calls, statuses = worker

    def download(url, _):
        calls.append(("collection", url))
        thread.request_cancel()

    monkeypatch.setattr(thread.scraper, "scrape_playlist", download)
    thread.run()
    assert calls == [("collection", ARTIST)]
    assert statuses[-1] == "Download cancelled"


@pytest.mark.parametrize("single_url", [False, True])
def test_desktop_metadata_cancellation_is_not_reported_as_failure(worker, monkeypatch, single_url):
    thread, calls, statuses = worker
    if single_url:
        thread.spotify_link = ARTIST

    def download(url, _):
        calls.append(("collection", url))
        thread.request_cancel()
        raise InterruptedError("metadata cancelled")

    monkeypatch.setattr(thread.scraper, "scrape_playlist", download)
    thread.run()
    assert calls == [("collection", ARTIST)]
    assert statuses[-1] == "Download cancelled"
    assert not any("failed" in status for status in statuses)


def test_desktop_validates_entire_queue_before_starting(worker):
    thread, calls, statuses = worker
    thread.spotify_link = [TRACK, "invalid"]
    thread.run()
    assert calls == []
    assert "Invalid Spotify URL" in statuses[-1]


def test_single_track_clears_previous_collection_state(monkeypatch):
    scraper = app.MusicScraper()
    scraper.counter = 5
    scraper._failed_tracks = ["previous failure"]
    scraper._manifest_path = "previous-collection-manifest"
    scraper._manifest_owners = {"track.mp3": "previous"}
    scraper._manifest_records = {("previous", "track.mp3")}
    scraper._parallel_mode = True
    scraper._cancel_event.set()
    fetch = MagicMock()
    monkeypatch.setattr(scraper, "ensure_spotifydown_api", fetch)
    scraper.scrape_track(TRACK, ".")
    assert scraper.counter == 0
    assert scraper._failed_tracks == []
    assert scraper._manifest_path is None
    assert scraper._manifest_owners == {}
    assert scraper._manifest_records == set()
    assert scraper._parallel_mode is False
    fetch.assert_not_called()
    scraper.close()


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr(app.UpdateCheckThread, "start", lambda _: None)
    win = app.MainWindow()
    yield win
    win.deleteLater()


def test_multiple_url_dialog_round_trip(window, monkeypatch):
    window.PlaylistLink.setText(f"{ARTIST} {TRACK}")
    dialog = MagicMock(return_value=(f"{TRACK}\n{ALBUM}", True))
    monkeypatch.setattr(app.QInputDialog, "getMultiLineText", dialog)
    window.MultipleUrlsBtn.click()
    assert dialog.call_args.args[-1] == f"{ARTIST}\n{TRACK}"
    assert parse_spotify_urls(window.PlaylistLink.text()) == [TRACK, ALBUM]


def test_cancel_url_dialog_preserves_input(window, monkeypatch):
    window.PlaylistLink.setText(TRACK)
    monkeypatch.setattr(app.QInputDialog, "getMultiLineText", lambda *_: (ALBUM, False))
    window.MultipleUrlsBtn.click()
    assert window.PlaylistLink.text() == TRACK


def test_invalid_second_url_does_not_prompt_for_download_folder(window, monkeypatch):
    window.PlaylistLink.setText(f"{TRACK}\ninvalid")
    window._download_path_set = False
    prompt = MagicMock()
    monkeypatch.setattr(window, "_prompt_download_location", prompt)
    window.on_returnButton()
    prompt.assert_not_called()
    assert "Invalid Spotify URL" in window.statusMsg.text()


@pytest.fixture
def cli_download(monkeypatch, tmp_path):
    scraper = app.MusicScraper()
    calls = []
    monkeypatch.setattr(app, "get_ffmpeg_path", lambda: "ffmpeg")
    events = []

    def build(_args, _cfg, cancel_event):
        events.append(cancel_event)
        return scraper

    monkeypatch.setattr(cli, "_build_scraper", build)
    monkeypatch.setattr(scraper, "scrape_track", lambda url, _: calls.append(url))
    monkeypatch.setattr(scraper, "scrape_playlist", lambda url, _: calls.append(url))
    args = cli.build_parser().parse_args(
        ["download", ARTIST, TRACK, ALBUM, "--out", str(tmp_path), "--json"]
    )
    return scraper, calls, events, args


def test_cli_batch_reports_all_urls(cli_download, capsys):
    _, calls, _, args = cli_download
    assert cli.cmd_download(args) == cli.EXIT_OK
    assert calls == [ARTIST, TRACK, ALBUM]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "batch"
    assert [event["url"] for event in events if event["event"] == "url_started"] == calls
    assert events[-1]["failed_urls"] == []


def test_cli_batch_retains_failures_from_earlier_urls(cli_download, monkeypatch, capsys):
    scraper, calls, _, args = cli_download

    def download(url, _):
        calls.append(url)
        if url == ARTIST:
            scraper._failed_tracks.append("Failed song")
            scraper.resume_skipped.emit(2)
        else:
            scraper.resume_skipped.emit(3)

    monkeypatch.setattr(scraper, "scrape_playlist", download)
    assert cli.cmd_download(args) == cli.EXIT_PARTIAL
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["failed_titles"] == ["Failed song"]
    assert summary["skipped"] == 5
    assert calls == [ARTIST, TRACK, ALBUM]


def test_cli_batch_continues_after_exception(cli_download, monkeypatch, capsys):
    scraper, calls, _, args = cli_download

    def download(url, _):
        calls.append(url)
        if url == ARTIST:
            raise RuntimeError("unavailable")

    monkeypatch.setattr(scraper, "scrape_playlist", download)
    assert cli.cmd_download(args) == cli.EXIT_PARTIAL
    assert calls == [ARTIST, TRACK, ALBUM]
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["failed_urls"] == [ARTIST]


def test_cli_cancel_stops_queue(cli_download, monkeypatch, capsys):
    scraper, calls, cancel_events, args = cli_download

    def download(url, _):
        calls.append(url)
        cancel_events[0].set()

    monkeypatch.setattr(scraper, "scrape_playlist", download)
    assert cli.cmd_download(args) == cli.EXIT_PARTIAL
    assert calls == [ARTIST]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["stopped"] is True


@pytest.mark.parametrize("single_url", [False, True])
def test_cli_metadata_cancellation_is_not_a_failed_url(
    cli_download, monkeypatch, capsys, single_url
):
    scraper, calls, cancel_events, args = cli_download
    if single_url:
        args.url = [ARTIST]

    def download(url, _):
        calls.append(url)
        cancel_events[0].set()
        raise InterruptedError("metadata cancelled")

    monkeypatch.setattr(scraper, "scrape_playlist", download)
    assert cli.cmd_download(args) == cli.EXIT_PARTIAL
    assert calls == [ARTIST]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    summary = events[-1]
    assert summary["event"] == "run_summary"
    assert summary["stopped"] is True
    assert summary.get("failed_urls", []) == []
    assert not any(event.get("code") == "url_failed" for event in events)


def test_cli_rejects_whole_invalid_queue(cli_download, capsys):
    _, calls, _, args = cli_download
    args.url.append("invalid")
    assert cli.cmd_download(args) == cli.EXIT_FATAL
    assert calls == []
    assert json.loads(capsys.readouterr().out)["code"] == "invalid_url"


def test_cli_batch_info_outputs_one_document_and_retains_errors(monkeypatch, capsys):
    def fetch(url):
        if url == ARTIST:
            raise RuntimeError("unavailable")
        return {"type": "track", "title": "Song", "artists": "Artist"}

    monkeypatch.setattr(cli, "_fetch_info", fetch)
    args = cli.build_parser().parse_args(["info", ARTIST, TRACK, "--json"])
    assert cli.cmd_info(args) == cli.EXIT_PARTIAL
    result = json.loads(capsys.readouterr().out)
    assert result["items"][0]["url"] == TRACK
    assert result["errors"] == [{"url": ARTIST, "message": "unavailable"}]
