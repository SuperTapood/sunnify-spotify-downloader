"""Regressions for the Hebrew releases rejected in the October 18 session."""

from unittest.mock import MagicMock

import pytest

import Spotify_Downloader as app

LIVE_SONGS = ("אירוניה", "הבן זונה", "בספה", "באשמתך", "פאניקה", "תן לי זמן")
SPECIAL_TITLE = 'עשר רמות מעליו - מיוחד למצעד השנתי תשפ"ו בכאן גימל'
ARTIST = "נועם קלינשטיין"


@pytest.fixture
def scraper():
    engine = app.MusicScraper()
    yield engine
    engine.close()


@pytest.mark.parametrize("song", LIVE_SONGS)
def test_selects_logged_hebrew_live_recordings(scraper, monkeypatch, song):
    extract = MagicMock(
        return_value={
            "entries": [
                {"id": "studio", "title": f"{ARTIST} - {song}", "duration": 180},
                {
                    "id": "live",
                    "title": f"{ARTIST} - {song} (LIVE אמפי שוני)",
                    "duration": 182,
                },
            ]
        }
    )
    monkeypatch.setattr(scraper, "_extract_youtube_info", extract)
    assert (
        scraper._select_youtube_match(
            f"ytsearch5:{song} - לייב אמפי שוני {ARTIST} audio",
            180,
            expected_title=f"{song} - לייב אמפי שוני",
            expected_artists=ARTIST,
        )
        == "https://www.youtube.com/watch?v=live"
    )
    assert extract.call_count == 1


@pytest.mark.parametrize(
    "expected_title,candidate_title",
    [
        ("אירוניה - לייב אמפי שוני", "אירוניה (LIVE אמפי שוני)"),
        ("אירוניה (LIVE אמפי שוני)", "אירוניה - לייב אמפי שוני"),
        (SPECIAL_TITLE, "עשר רמות מעליו"),
    ],
)
def test_music_catalog_accepts_equivalent_hebrew_editions(
    scraper, monkeypatch, expected_title, candidate_title
):
    def results(*_args, **_kwargs):
        yield [
            {
                "id": "catalog",
                "title": candidate_title,
                "result_type": "song",
                "artists": [ARTIST],
                "duration": 180,
            }
        ]

    monkeypatch.setattr(app, "iter_youtube_music_results", results)
    assert list(scraper._iter_youtube_music_matches(expected_title, ARTIST, 180, None, None)) == [
        "https://www.youtube.com/watch?v=catalog"
    ]


def test_short_title_retry_finds_special_release(scraper, monkeypatch):
    core_query = f'ytsearch5:"עשר רמות מעליו" "{ARTIST}"'
    queries = []

    def extract(_options, query):
        queries.append(query)
        if query == core_query:
            return {
                "entries": [
                    {
                        "id": "annual",
                        "title": f"{ARTIST} ואנה זק - עשר רמות מעליו",
                        "channel": ARTIST,
                        "duration": 180,
                    }
                ]
            }
        return {"entries": []}

    monkeypatch.setattr(scraper, "_extract_youtube_info", extract)
    assert (
        scraper._select_youtube_match(
            f"ytsearch5:{SPECIAL_TITLE} {ARTIST},\xa0Anna Zak audio",
            180,
            expected_title=SPECIAL_TITLE,
            expected_artists=f"{ARTIST},\xa0Anna Zak",
        )
        == "https://www.youtube.com/watch?v=annual"
    )
    assert core_query in queries
    assert len(queries) == len(set(queries))


@pytest.mark.parametrize(
    "title,duration",
    [
        ("נועם קלינשטיין - שיר אחר (LIVE אמפי שוני)", 180),
        ("נועם קלינשטיין - אירוניה", 180),
        ("נועם קלינשטיין - אירוניה (LIVE אמפי שוני)", 240),
    ],
)
def test_live_matching_still_rejects_wrong_recordings(scraper, monkeypatch, title, duration):
    monkeypatch.setattr(
        scraper,
        "_extract_youtube_info",
        lambda *_a: {"entries": [{"id": "wrong", "title": title, "duration": duration}]},
    )
    assert (
        scraper._select_youtube_match(
            f"ytsearch5:אירוניה - לייב אמפי שוני {ARTIST} audio",
            180,
            expected_title="אירוניה - לייב אמפי שוני",
            expected_artists=ARTIST,
        )
        is None
    )


def test_short_title_retry_preserves_artist_gate(scraper, monkeypatch):
    monkeypatch.setattr(
        scraper,
        "_extract_youtube_info",
        lambda *_a: {
            "entries": [{"id": "wrong", "title": "Someone Else - עשר רמות מעליו", "duration": 180}]
        },
    )
    assert (
        scraper._select_youtube_match(
            f"ytsearch5:{SPECIAL_TITLE} {ARTIST}, Anna Zak audio",
            180,
            expected_title=SPECIAL_TITLE,
            expected_artists=f"{ARTIST}, Anna Zak",
        )
        is None
    )


@pytest.mark.parametrize(
    "title", ["אהבה - געגוע", "אני לייב", "Emil - Despair", "Live and Let Die"]
)
def test_real_song_titles_keep_their_words(title):
    assert app.MusicScraper._spotify_title_core(title) == title
