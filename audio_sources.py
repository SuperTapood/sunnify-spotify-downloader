"""YouTube Music discovery using spotDL's ISRC, songs, then videos strategy.

The strategy is independently implemented for Sunnify; see
https://github.com/spotDL/spotify-downloader/blob/master/spotdl/providers/audio/ytmusic.py
and spotdl/providers/audio/base.py (MIT licensed).
Playback, cookies, conversion, and recording validation belong to the caller.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterator
from functools import partial

import requests
from ytmusicapi import YTMusic

log = logging.getLogger("sunnify.sources")


def normalize_isrc(value: object) -> str | None:
    """Accept canonical and hyphenated recording codes, never arbitrary queries."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper().replace("-", "")
    return normalized if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", normalized) else None


def _duration_seconds(value: object) -> float | None:
    try:
        if isinstance(value, str) and ":" in value:
            parts = value.split(":")
            if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
                return None
            duration = 0.0
            for part in parts:
                duration = duration * 60 + int(part)
        else:
            duration = float(value)
        return duration if math.isfinite(duration) and duration > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_result(result: object) -> dict | None:
    if not isinstance(result, dict) or result.get("resultType") not in ("song", "video"):
        return None
    video_id = result.get("videoId")
    title = result.get("title")
    if (
        not isinstance(video_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(result.get("artists"), list)
    ):
        return None
    artists = [
        artist["name"].strip()
        for artist in result["artists"]
        if isinstance(artist, dict)
        and isinstance(artist.get("name"), str)
        and artist["name"].strip()
    ]
    if not artists:
        return None
    album = result.get("album")
    return {
        "id": video_id,
        "title": title,
        "result_type": result["resultType"],
        "artists": artists,
        "album": album.get("name") if isinstance(album, dict) else None,
        "duration": _duration_seconds(result.get("duration_seconds"))
        or _duration_seconds(result.get("duration")),
    }


def iter_youtube_music_results(
    title: str,
    artists: str,
    *,
    isrc: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[list[dict]]:
    """Yield bounded result groups lazily so a song match avoids later searches.

    Each call owns its HTTP session, including when playlist workers run in
    parallel. Closing the iterator releases it after an early successful match.
    Discovery is anonymous; playback still uses Sunnify's private cookie copies.
    """

    def check_cancelled():
        if cancelled and cancelled():
            raise InterruptedError("download cancelled")

    check_cancelled()
    query = f"{title} {artists}".strip()
    searches = [(query, "songs"), (query, "videos")]
    recording_code = normalize_isrc(isrc)
    if recording_code:
        searches.insert(0, (recording_code, "songs"))

    with requests.Session() as session:
        session.request = partial(session.request, timeout=10)
        client = None
        for search_term, result_filter in searches:
            check_cancelled()
            for attempt in range(2):
                try:
                    if client is None:
                        client = YTMusic(requests_session=session)
                    results = client.search(
                        search_term, filter=result_filter, limit=20, ignore_spelling=True
                    )
                    if not isinstance(results, list):
                        raise ValueError("YouTube Music returned invalid search results")
                    break
                except InterruptedError:
                    raise
                except Exception as exc:
                    check_cancelled()
                    log.debug("YouTube Music %s search failed: %s", result_filter, exc)
                    client = None
                    if attempt == 1:
                        log.warning("YouTube Music discovery unavailable; trying next source")
                        return
            check_cancelled()
            candidates = [
                candidate
                for result in results[:20]
                if (candidate := _normalize_result(result)) is not None
            ]
            log.debug(
                "YouTube Music %s search returned %d candidates", result_filter, len(candidates)
            )
            yield candidates
