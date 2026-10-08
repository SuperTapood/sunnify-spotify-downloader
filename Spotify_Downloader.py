#
"""
Sunnify (Spotify Downloader)
Copyright (C) 2024-2026 Sunny Patel <sunnypatel124555@gmail.com>

EDUCATIONAL PROJECT DISCLAIMER:
This software is a student portfolio project developed for educational purposes only.
It is intended to demonstrate software engineering skills and is provided free of charge.
Users are solely responsible for ensuring compliance with applicable laws in their jurisdiction.
This software should only be used with content you own or have permission to download.
See DISCLAIMER.md for full terms.

For the program to work, the playlist URL pattern must follow the format of
/playlist/abcdefghijklmnopqrstuvwxyz... If the program stops working, email
<sunnypatel124555@gmail.com> or open an issue in the repository.
"""

from __future__ import annotations

__version__ = "2.4.3"

import atexit
import concurrent.futures
import contextlib
import faulthandler
import hashlib
import logging
import os
import platform
import re
import shutil
import signal
import sys
import threading
import unicodedata
import webbrowser
from collections import OrderedDict
from logging.handlers import RotatingFileHandler

import requests
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3
from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QSize,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QCursor, QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)
from yt_dlp import YoutubeDL
from yt_dlp.cookies import CookieLoadError

from spotifydown_api import (
    ContentUnavailableError,
    ExtractionError,
    NetworkError,
    PlaylistClient,
    PlaylistInfo,
    RateLimitError,
    SpotifyDownAPIError,
    cap_filename,
    detect_spotify_url_type,
    extract_playlist_id,
    parse_spotify_urls,
    sanitize_filename,
)
from Template import Ui_MainWindow

# Module logger. Stays a no-op (no handlers) until _setup_logging() runs at
# startup, so importing this module in tests stays silent and writes nothing.
log = logging.getLogger("sunnify")

_crash_log_handle = None
_diagnostics_lock = threading.Lock()
_diagnostics_exit_registered = False


def _log_excepthook(exc_type, exc, tb):
    """Route uncaught main-thread exceptions to the log before the default handler."""
    # ctrl+c and an intentional sys.exit() are clean exits, not crashes
    if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        with contextlib.suppress(Exception):
            log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
    if sys.stderr is not None:  # windowed builds have no stderr to write to
        sys.__excepthook__(exc_type, exc, tb)


def _thread_excepthook(args):
    """Same, for python threads; qt threads log inside their own run()."""
    if issubclass(args.exc_type, SystemExit):
        return
    with contextlib.suppress(Exception):
        log.critical(
            "uncaught exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )


def _close_crash_log() -> None:
    """Disable faulthandler and close the file object backing its descriptor."""
    global _crash_log_handle
    with _diagnostics_lock:
        handle, _crash_log_handle = _crash_log_handle, None
        if handle is None:
            return
        with contextlib.suppress(Exception):
            faulthandler.disable()
        with contextlib.suppress(Exception):
            handle.close()


def _shutdown_diagnostics() -> None:
    """Flush the final session marker before releasing crash diagnostics."""
    with contextlib.suppress(Exception):
        log.info("==== sunnify session end ====")
    _close_crash_log()


def _install_crash_handlers() -> None:
    """Make every abnormal exit land in the log; logging is our only diagnostic."""
    global _crash_log_handle, _diagnostics_exit_registered
    sys.excepthook = _log_excepthook
    threading.excepthook = _thread_excepthook
    # faulthandler catches native crashes (qt/ffmpeg segfaults) excepthook can't;
    # crash.log sits next to sunnify.log. Keep the Python file object alive:
    # faulthandler retains only its descriptor, so a temporary open(...) object
    # would be closed immediately and native-crash diagnostics would be lost.
    with contextlib.suppress(Exception):
        crash_path = os.path.join(os.path.dirname(log_file_path()), "crash.log")
        with _diagnostics_lock:
            current_path = getattr(_crash_log_handle, "name", None)
            if _crash_log_handle is None or _crash_log_handle.closed or current_path != crash_path:
                if _crash_log_handle is not None:
                    with contextlib.suppress(Exception):
                        faulthandler.disable()
                    with contextlib.suppress(Exception):
                        _crash_log_handle.close()
                _crash_log_handle = open(crash_path, "a", encoding="utf-8")  # noqa: SIM115
                faulthandler.enable(_crash_log_handle)
            if not _diagnostics_exit_registered:
                atexit.register(_shutdown_diagnostics)
                _diagnostics_exit_registered = True


class _YtdlpLog:
    """Bridge yt-dlp's own output into our log and remember the last error.

    With ignoreerrors=True a failed download won't raise, so capturing error()
    here is the only way to record *why* no audio file landed (bot-block vs
    unavailable vs format vs nsig). Routine yt-dlp chatter goes to our DEBUG so
    the default INFO log stays lean.
    """

    def __init__(self):
        self.last_error = None

    def debug(self, msg):
        log.debug("yt-dlp: %s", msg)

    info = debug

    def warning(self, msg):
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg):
        self.last_error = msg
        log.debug("yt-dlp error: %s", msg)


def get_ffmpeg_path():
    """Get path to FFmpeg - checks bundled first, then system paths."""
    # Check bundled FFmpeg first (for PyInstaller builds)
    if getattr(sys, "frozen", False):
        base_path = sys._MEIPASS
        if sys.platform == "win32":
            ffmpeg = os.path.join(base_path, "ffmpeg", "ffmpeg.exe")
        else:
            ffmpeg = os.path.join(base_path, "ffmpeg", "ffmpeg")
        if os.path.exists(ffmpeg):
            return os.path.join(base_path, "ffmpeg")

    # Check common system paths (for homebrew/system installs)
    ffmpeg_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    common_paths = [
        "/opt/homebrew/bin",  # macOS ARM homebrew
        "/usr/local/bin",  # macOS Intel homebrew / Linux
        "/usr/bin",  # Linux system
    ]
    if sys.platform == "win32":
        # package managers put these on PATH, so shutil.which below usually
        # wins first; these cover the install-then-same-shell case, and are
        # read from each tool's own env var so a relocated install still hits
        choco = os.environ.get("CHOCOLATEYINSTALL") or r"C:\ProgramData\chocolatey"
        scoop = os.environ.get("SCOOP") or os.path.join(os.path.expanduser("~"), "scoop")
        common_paths += [
            os.path.join(choco, "bin"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
            os.path.join(scoop, "shims"),
            r"C:\ffmpeg\bin",  # the unzip convention, the one that never lands on PATH
        ]

    for path in common_paths:
        ffmpeg = os.path.join(path, ffmpeg_name)
        if os.path.exists(ffmpeg):
            return path

    # Check if ffmpeg is in PATH
    import shutil

    ffmpeg_in_path = shutil.which("ffmpeg")
    if ffmpeg_in_path:
        return os.path.dirname(ffmpeg_in_path)

    # On Windows, check user/machine environment in case PATH was modified recently
    if sys.platform == "win32":
        try:
            import winreg

            for hkey in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                sub = (
                    r"Environment"
                    if hkey == winreg.HKEY_CURRENT_USER
                    else r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
                )
                try:
                    with winreg.OpenKey(hkey, sub) as key:
                        val, _ = winreg.QueryValueEx(key, "PATH")
                        for p in val.split(os.pathsep):
                            expanded = os.path.expandvars(p.strip())
                            if expanded and os.path.exists(os.path.join(expanded, "ffmpeg.exe")):
                                return expanded
                except OSError:
                    pass
        except Exception:
            pass

    return None


# Supported output formats. "lossy" means quality/bitrate applies; "lossless"
# means the ffmpeg postprocessor ignores preferredquality.
SUPPORTED_FORMATS = {
    "mp3": {"ext": "mp3", "lossy": True},
    "m4a": {"ext": "m4a", "lossy": True},
    "opus": {"ext": "opus", "lossy": True},
    "flac": {"ext": "flac", "lossy": False},
    "wav": {"ext": "wav", "lossy": False},
}
SUPPORTED_QUALITIES = ("128", "192", "256", "320")
# "auto" keeps whatever rate the source stream has (YouTube audio is 48 kHz).
SUPPORTED_SAMPLE_RATES = ("auto", "44100", "48000")


class _Setting:
    """One user setting: config key, validation, scraper wiring, CLI surface."""

    __slots__ = ("key", "default", "kind", "choices", "scraper_kwarg", "cli_flag", "help")

    def __init__(self, key, default, kind, choices=(), scraper_kwarg=None, cli_flag=None, help=""):
        self.key = key
        self.default = default
        self.kind = kind  # "choice" | "bool" | "path"
        self.choices = choices
        self.scraper_kwarg = scraper_kwarg
        self.cli_flag = cli_flag
        self.help = help

    def coerce(self, value):
        """Validated value or the default - never raises on user data."""
        if self.kind == "bool":
            return value if isinstance(value, bool) else self.default
        if self.kind == "choice":
            return value if value in self.choices else self.default
        return value if isinstance(value, str) or value is None else self.default


# Single source of truth for user settings. Config load/validation, scraper
# construction, and every CLI flag derive from this - a new setting added
# here reaches the CLI with zero CLI changes (the dialog row stays bespoke).
SETTINGS = (
    _Setting("download_path", None, "path", help="where downloads land"),
    _Setting(
        "format",
        "mp3",
        "choice",
        tuple(SUPPORTED_FORMATS),
        scraper_kwarg="audio_format",
        cli_flag="--format",
        help="audio format",
    ),
    _Setting(
        "quality",
        "192",
        "choice",
        SUPPORTED_QUALITIES,
        scraper_kwarg="audio_quality",
        cli_flag="--quality",
        help="bitrate in kbps, lossy formats only",
    ),
    _Setting(
        "sample_rate",
        "auto",
        "choice",
        SUPPORTED_SAMPLE_RATES,
        scraper_kwarg="sample_rate",
        cli_flag="--sample-rate",
        help="output sample rate, applies to mp3/flac/wav",
    ),
    _Setting(
        "include_track_number",
        False,
        "bool",
        scraper_kwarg="include_track_number",
        cli_flag="--track-numbers",
        help="prefix filenames with playlist position",
    ),
    _Setting(
        "download_workers",
        "4",
        "choice",
        ("1", "2", "4", "6", "8"),
        scraper_kwarg="download_workers",
        cli_flag="--workers",
        help="simultaneous downloads (4 by default; up to 8 for faster connections)",
    ),
    _Setting(
        "artist_first",
        False,
        "bool",
        scraper_kwarg="artist_first",
        cli_flag="--artist-first",
        help='name files "Artist - Song" instead of "Song - Artist"',
    ),
    _Setting(
        "title_only",
        False,
        "bool",
        scraper_kwarg="title_only",
        cli_flag="--title-only",
        help="name files by song title alone; the artist stays in the tags",
    ),
    _Setting(
        "loose_match",
        False,
        "bool",
        scraper_kwarg="loose_match",
        cli_flag="--loose-match",
        help="fall back to the closest result when strict matching finds nothing",
    ),
)


def scraper_kwargs_from(settings: dict) -> dict:
    """Registry-derived MusicScraper kwargs from a config/settings dict."""
    return {s.scraper_kwarg: settings.get(s.key, s.default) for s in SETTINGS if s.scraper_kwarg}


# Resume manifest: JSON-lines file per playlist folder recording landed tracks,
# so a rate-limited playlist finishes across sessions instead of restarting (#40).
MANIFEST_FILENAME = ".sunnify-manifest.jsonl"


def _iter_manifest_records(path: str):
    """Yield validated ``{"id", "file"}`` records from a resume manifest.

    A power loss can leave a partial final line, and users may inspect/edit the
    JSONL manually. Semantically invalid JSON (a list, scalar, non-string path,
    or path traversal) is ignored just like syntactically invalid JSON instead
    of crashing resume/status or reading outside the playlist directory.
    """
    import json
    import ntpath

    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                track_id = record.get("id")
                filename = record.get("file")
                if not isinstance(track_id, str) or not track_id:
                    continue
                if not isinstance(filename, str) or not filename:
                    continue
                if (
                    filename in (".", "..")
                    or filename != os.path.basename(filename)
                    or os.path.isabs(filename)
                    or ntpath.splitdrive(filename)[0]
                    or "/" in filename
                    or "\\" in filename
                    or any(ord(char) < 32 for char in filename)
                ):
                    continue
                yield {"id": track_id, "file": filename}
    except OSError:
        return


def _config_dir() -> str:
    """Return the per-user config directory, creating it if needed."""
    import json as _json  # noqa: F401 (used by load/save)

    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
    path = os.path.join(base, "Sunnify")
    os.makedirs(path, exist_ok=True)
    return path


def _log_dir() -> str:
    """Return the per-user log directory path (does not create it).

    Pure path computation, no filesystem side effects, so callers that only
    need the string (e.g. a settings tooltip) don't create stray folders;
    setup_logging() and _open_logs() create the dir when they actually use it.

    Uses each platform's conventional spot for app logs (not config), so the
    files are where a user (or a support request) would expect them:
      windows -> %LOCALAPPDATA%\\Sunnify\\logs
      macOS   -> ~/Library/Logs/Sunnify
      linux   -> $XDG_STATE_HOME/sunnify/logs (defaults to ~/.local/state)
    """
    if sys.platform == "win32":
        base = (
            os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
        )
        path = os.path.join(base, "Sunnify", "logs")
    elif sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library", "Logs", "Sunnify")
    else:
        base = os.environ.get(
            "XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")
        )
        path = os.path.join(base, "sunnify", "logs")
    return path


def log_file_path() -> str:
    """Absolute path of the current log file (used by the 'open logs' action)."""
    return os.path.join(_log_dir(), "sunnify.log")


def setup_logging() -> str:
    """Configure file logging once; return the log file path.

    Rotating handler caps disk use at ~6MB total (1MB x 5 backups) so logs are
    diagnostic, never bloat. Idempotent: safe to call more than once. The line
    format is deliberately dense (timestamp, level, function:line) so a single
    log pasted into an issue is enough to pinpoint where a download went wrong.
    A session header records the environment every launch.
    """
    if any(getattr(h, "_sunnify", False) for h in log.handlers):
        return log_file_path()

    path = log_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=5, encoding="utf-8", delay=True
    )
    handler.__dict__["_sunnify"] = True  # tag so we don't double-attach on re-call
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(funcName)s:%(lineno)d] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    # default INFO stays lean (scales with failures, not track count); set
    # SUNNIFY_DEBUG=1 to get the full per-track + yt-dlp trail for hard cases.
    level = logging.DEBUG if os.environ.get("SUNNIFY_DEBUG") else logging.INFO
    log.setLevel(level)
    log.addHandler(handler)
    log.propagate = False

    try:
        ytdlp_ver = __import__("yt_dlp").version.__version__
    except Exception:
        ytdlp_ver = "?"
    log.info("==== sunnify session start ====")
    log.info(
        "version=%s platform=%s python=%s yt-dlp=%s",
        __version__,
        f"{sys.platform}-{platform.machine()}",
        platform.python_version(),
        ytdlp_ver,
    )
    log.info("ffmpeg=%s", get_ffmpeg_path() or "(not found)")
    log.info("logs=%s level=%s", path, logging.getLevelName(level))
    _install_crash_handlers()
    return path


def _config_path() -> str:
    return os.path.join(_config_dir(), "config.json")


def load_config() -> dict:
    """Load persisted user config. Missing or corrupt file returns defaults.

    Every user setting is validated through the SETTINGS registry;
    star_prompt_shown is internal state, not a setting."""
    import json

    defaults = {s.key: s.default for s in SETTINGS}
    defaults["version"] = 1
    defaults["star_prompt_shown"] = False
    try:
        with open(_config_path(), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return defaults
        for s in SETTINGS:
            if s.key in data:
                defaults[s.key] = s.coerce(data[s.key])
        if isinstance(data.get("star_prompt_shown"), bool):
            defaults["star_prompt_shown"] = data["star_prompt_shown"]
        return defaults
    except (OSError, json.JSONDecodeError):
        return defaults


def save_config(config: dict) -> None:
    """Persist user config atomically. Best-effort, swallowing I/O errors."""
    import json
    import tempfile

    temp_path = None
    fd = None
    try:
        destination = _config_path()
        fd, temp_path = tempfile.mkstemp(
            prefix=".config-", suffix=".tmp", dir=os.path.dirname(destination)
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            fd = None
            json.dump(config, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, destination)
        temp_path = None
    except OSError as exc:
        log.warning("could not save config: %s", exc)
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if temp_path:
            with contextlib.suppress(OSError):
                os.remove(temp_path)


GITHUB_REPO = "sunnypatell/sunnify-spotify-downloader"
_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


def _parse_version(s: str) -> tuple:
    """'v2.0.13' / '2.0.13' / '2.0.13-beta' -> (2, 0, 13). Stops at the first non-int part."""
    parts = []
    for chunk in (s or "").strip().lstrip("vV").split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            break
        parts.append(int(num))
    return tuple(parts)


def _is_newer_version(latest: str, current: str) -> bool:
    """True if latest is a strictly newer release than current (numeric, not lexical)."""
    lv, cv = _parse_version(latest), _parse_version(current)
    return bool(lv) and lv > cv


def _check_for_update(current: str, timeout: int = 5):
    """Return (latest_version, release_url) if a newer release exists, else None.

    Fail-silent (returns None) on any network/parse error so launch is never
    blocked or crashed by the check.
    """
    try:
        r = requests.get(
            _LATEST_RELEASE_API,
            timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
        )
        if r.status_code != 200:
            log.debug("update check: github api returned %s", r.status_code)
            return None
        data = r.json()
        tag = data.get("tag_name") or ""
        url = data.get("html_url") or _RELEASES_PAGE
        if _is_newer_version(tag, current):
            return (tag.lstrip("vV"), url)
        log.debug("update check: on latest (%s, newest %s)", current, tag or "?")
        return None
    except Exception as exc:  # network/parse must never disrupt launch
        log.debug("update check skipped: %s", exc)
        return None


class UpdateCheckThread(QThread):
    """Runs _check_for_update off the UI thread and signals if a release is newer."""

    update_available = pyqtSignal(str, str)  # (latest_version, release_url)

    def __init__(self, current_version: str):
        super().__init__()
        self._current = current_version

    def run(self):
        result = _check_for_update(self._current)
        if result:
            self.update_available.emit(result[0], result[1])


# Clients for the retry attempt, as a set rather than a preference list:
# android/ios expose streams the default path may not, and tv/web_safari
# supply an alternative URL when those return 403. The combination is what
# recovers a track - measured per client, each alone fails on videos the
# set handles, and yt-dlp's own defaults 403 on some of them too. Kept
# deliberately, not by inertia; validated below so a retired client can't
# rot into a hard failure, and exercised end to end by the youtube_retry
# check in scripts/check_api_status.py.
_RETRY_CLIENTS = ("android", "ios", "tv", "web_safari")


def _retry_player_clients() -> tuple[str, ...]:
    """_RETRY_CLIENTS filtered to the clients yt-dlp still ships.

    YouTube retires client names and yt-dlp follows; naming a dead one is a
    hard extractor error, so unknown names are dropped with a warning that
    says which. Everything dropping out means the retry falls back to
    yt-dlp's maintained defaults, which is degraded but never broken.
    """
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    except Exception:
        # table moved: our names are still the best guess we have
        log.debug("yt-dlp client table not introspectable; using retry clients unvalidated")
        return _RETRY_CLIENTS
    live = tuple(name for name in _RETRY_CLIENTS if name in INNERTUBE_CLIENTS)
    retired = [name for name in _RETRY_CLIENTS if name not in INNERTUBE_CLIENTS]
    if retired:
        log.warning("retry clients no longer shipped by yt-dlp, dropped: %s", retired)
    if not live:
        log.warning("no retry clients survive; retry now uses yt-dlp's defaults")
    return live


class MusicScraper(QThread):
    PlaylistCompleted = pyqtSignal(str)
    PlaylistID = pyqtSignal(str)
    song_Album = pyqtSignal(str)
    song_meta = pyqtSignal(dict)
    add_song_meta = pyqtSignal(dict)
    count_updated = pyqtSignal(int)
    progress_snapshot = pyqtSignal(dict)
    dlprogress_signal = pyqtSignal(int)
    Resetprogress_signal = pyqtSignal(int)
    resume_skipped = pyqtSignal(int)  # manifest-resumed tracks never reach song_meta
    error_signal = pyqtSignal(str)  # Signal for error messages to UI

    # Max concurrent track downloads. 4 is the measured sweet spot:
    # linear speedup through 4, diminishing returns past 6 (CPU-bound ffmpeg).
    MAX_WORKERS = 4

    def _youtube_dl(self, options):
        """Use Firefox cookies by default; continue anonymously if unavailable."""
        opts = dict(options)
        if not getattr(self, "_firefox_cookies_unavailable", False):
            opts["cookiesfrombrowser"] = ("firefox",)
        ydl = YoutubeDL(opts)
        if "cookiesfrombrowser" in opts:
            try:
                # yt-dlp loads browser cookies lazily. Check now so a missing
                # profile does not turn every search into a track failure.
                getattr(ydl, "cookiejar", None)
            except CookieLoadError:
                self._firefox_cookies_unavailable = True
                log.warning("Firefox cookies unavailable; continuing without browser cookies")
                ydl.close()
                opts.pop("cookiesfrombrowser")
                ydl = YoutubeDL(opts)
        return ydl

    def __init__(
        self,
        cancel_event: threading.Event | None = None,
        *,
        audio_format: str = "mp3",
        audio_quality: str = "192",
        include_track_number: bool = False,
        artist_first: bool = False,
        title_only: bool = False,
        sample_rate: str = "auto",
        loose_match: bool = False,
        write_metadata: bool = False,
        download_workers: str = "4",
    ):
        super().__init__()
        self.counter = 0  # Initialize counter to zero
        self.MAX_WORKERS = (
            int(download_workers) if download_workers in ("1", "2", "4", "6", "8") else 4
        )
        self.session = requests.Session()
        self.spotifydown_api = None
        self._cancel_event = cancel_event or threading.Event()
        self._failed_tracks: list[str] = []  # Track failed downloads
        # Output options. audio_format must be a key of SUPPORTED_FORMATS;
        # audio_quality only applies to lossy formats (mp3/m4a/opus).
        self.audio_format = audio_format if audio_format in SUPPORTED_FORMATS else "mp3"
        self.audio_quality = audio_quality if audio_quality in SUPPORTED_QUALITIES else "192"
        self.include_track_number = bool(include_track_number)
        self.artist_first = bool(artist_first)
        self.title_only = bool(title_only)
        self.sample_rate = sample_rate if sample_rate in SUPPORTED_SAMPLE_RATES else "auto"
        # opt-in: when strict title/artist matching fails, fall back to the
        # duration-closest youtube result (recovers cross-script matches);
        # off by default so the wrong-audio safeguard (#52) stays the default.
        self.loose_match = bool(loose_match)
        # GUI downloads opt in from the "Add Meta Tags" checkbox; the CLI
        # enables this unconditionally. Writing inside the bounded download
        # workers guarantees completion before a run is reported finished.
        self.write_metadata = bool(write_metadata)
        self._counter_lock = threading.RLock()
        self._queue_counts = {"downloaded": 0, "skipped": 0, "failed": 0, "reused": 0}
        self._queue_active = False
        self._url_index = 1
        self._url_count = 1
        self._progress_revision = 0
        self._preview_serial = 0
        self._current_resumed = 0
        self._total_known = False
        self._completed_audio: dict[tuple, str] = {}
        self._failed_lock = threading.Lock()
        self._filename_lock = threading.Lock()
        self._manifest_lock = threading.Lock()
        self._manifest_path: str | None = None
        self._manifest_owners: dict[str, str] = {}
        self._manifest_records: set[tuple[str, str]] = set()
        self._manifest_write_warned = False
        # youtube blocks per-IP, so it hits every track at once; say it once
        # rather than 300 times, and say what it actually means
        self._network_blocked = False
        self._in_flight_files: set[str] = set()
        # Set to True during parallel playlist downloads so workers can suppress
        # per-track UI noise (label flicker, thumbnail spam, progress bar jitter)
        # that only makes sense for a single active download.
        self._parallel_mode = False
        self._total_tracks = 0

    def begin_queue(self, url_count: int) -> None:
        with self._counter_lock:
            self._queue_active = True
            self._queue_counts = dict.fromkeys(self._queue_counts, 0)
            self._url_count = url_count
            self._completed_audio.clear()

    def begin_url(self, index: int) -> None:
        with self._counter_lock:
            self._url_index = index
            self.counter = 0
            self._current_resumed = 0
            self._total_known = False
            self._emit_progress_snapshot()

    def _emit_progress_snapshot(self) -> None:
        """Called under _counter_lock: UI reads this snapshot, never mutable state."""
        self._progress_revision += 1
        self.progress_snapshot.emit(
            {
                **self._queue_counts,
                "revision": self._progress_revision,
                "url_index": self._url_index,
                "url_count": self._url_count,
                "processed": self.counter + self._current_resumed,
                "total": self._total_tracks + self._current_resumed if self._total_known else None,
            }
        )

    def _set_total_tracks(self, total: int) -> None:
        with self._counter_lock:
            self._total_tracks = total
            self._total_known = True
            self._emit_progress_snapshot()

    def _emit_song_meta(self, meta: dict) -> None:
        with self._counter_lock:
            self._preview_serial += 1
            self.song_meta.emit(
                {**meta, "_preview_id": self._preview_serial, "_url_index": self._url_index}
            )

    def _reuse_audio(self, track_id: str, destination: str) -> bool:
        with self._counter_lock:
            source = self._completed_audio.get(self._audio_cache_key(track_id))
        if not source or not os.path.isfile(source):
            return False
        # Copy, never hard-link: writing tags for this collection must not
        # modify a previously downloaded collection's artwork/track numbers.
        try:
            if os.path.abspath(source) != os.path.abspath(destination):
                shutil.copyfile(source, destination)
        except OSError:
            log.debug("could not reuse downloaded track %s", track_id, exc_info=True)
            # A partial copy must not be mistaken for a finished download.
            with contextlib.suppress(OSError):
                if os.path.abspath(source) != os.path.abspath(destination):
                    os.remove(destination)
            return False
        with self._counter_lock:
            self._queue_counts["reused"] += 1
        return True

    def _remember_audio(self, track_id: str, path: str) -> None:
        # Only register audio produced by this run. Existing files and old
        # manifests don't establish their bitrate or sample rate, so copying
        # them into another collection could silently lower output quality.
        if track_id:
            with self._counter_lock:
                self._completed_audio[self._audio_cache_key(track_id)] = path

    def _audio_cache_key(self, track_id: str) -> tuple:
        return track_id, self.audio_format, self.audio_quality, self.sample_rate

    def is_cancelled(self) -> bool:
        """Check if cancellation has been requested."""
        return self._cancel_event.is_set()

    _BLOCK_MARKERS = ("not a bot", "bot check", "bot challenge")

    def _note_if_network_blocked(self, reason: str) -> None:
        """Emit one plain-language notice the first time YouTube gates us.

        The gate is applied to the whole network, so without this every track
        fails with its own generic message and the real cause never surfaces.
        """
        text = (reason or "").lower()
        if not any(m in text for m in self._BLOCK_MARKERS):
            return
        with self._failed_lock:
            if self._network_blocked:
                return
            self._network_blocked = True
        log.error("youtube is gating this network (bot check); downloads cannot proceed")
        self.error_signal.emit(
            "YouTube is asking this network to prove it isn't a bot, so downloads are "
            "being refused. This is applied to your IP, not to Sunnify: try another "
            "network or connection, or wait a few hours."
        )

    def _get_user_friendly_error(self, error: Exception, track_title: str = "") -> str:
        """Convert exception to user-friendly error message."""
        if isinstance(error, RateLimitError):
            return "Rate limited by Spotify - waiting..."
        if isinstance(error, NetworkError):
            return "Network error - retrying..."
        if isinstance(error, ContentUnavailableError):
            return str(error)
        if isinstance(error, ExtractionError):
            return f"Could not access '{track_title}' - may be unavailable"
        if any(m in str(error).lower() for m in self._BLOCK_MARKERS):
            return f"YouTube blocked this network - '{track_title}' skipped"
        if "HTTP Error 429" in str(error):
            return "YouTube rate limit - waiting..."
        error_text = str(error).lower()
        if (
            "no video formats" in error_text
            or "no playable audio source" in error_text
            or "unavailable" in error_text
        ):
            return f"'{track_title}' not found on YouTube"
        return f"Error: {str(error)[:50]}"

    def ensure_spotifydown_api(self):
        if self.spotifydown_api is None:
            # PlaylistClient owns thread-local connection pools. Passing this
            # scraper's legacy Session would force four enrichment workers to
            # share one requests.Session, which is not thread-safe.
            self.spotifydown_api = PlaylistClient(cancel_event=self._cancel_event)
        return self.spotifydown_api

    def close(self) -> None:
        """Release HTTP connection pools owned by this scraper."""
        client, self.spotifydown_api = self.spotifydown_api, None
        if client is not None and hasattr(client, "close"):
            with contextlib.suppress(Exception):
                client.close()
        with contextlib.suppress(Exception):
            self.session.close()

    def _youtube_is_blocked(self) -> bool:
        with self._failed_lock:
            return self._network_blocked

    def _write_metadata_if_enabled(self, song_meta: dict) -> None:
        if not self.write_metadata:
            return
        try:
            song_meta["_metadata_status"] = _write_song_metadata(song_meta, song_meta["file"])
        except Exception:
            log.error("tag write failed: %s", song_meta.get("file", "?"), exc_info=True)
            song_meta["_metadata_status"] = "Tagging failed (audio was downloaded)"

    def sanitize_text(self, text):
        """Sanitize text for filename usage."""
        return sanitize_filename(text, allow_spaces=True)

    def _compose_filename(self, sanitized_title, sanitized_artists, track_num=None, disambig=None):
        """Filename from the naming settings (#77, #91). Parts arrive
        sanitized, so composition can't change path safety. disambig is a
        track id appended when two tracks resolve to the same name."""
        if self.title_only:
            # a title that sanitizes to nothing falls back rather than yield ".mp3"
            stem = sanitized_title or sanitized_artists or "track"
        elif self.artist_first:
            stem = f"{sanitized_artists} - {sanitized_title}"
        else:
            stem = f"{sanitized_title} - {sanitized_artists}"
        preserved_suffix = ""
        if disambig:
            safe_disambig = sanitize_filename(str(disambig), allow_spaces=False)
            if len(safe_disambig.encode("utf-8")) > 64:
                # Remote ids are normally 22 ASCII characters. Hash a
                # pathological/custom id so numbered collision attempts stay
                # distinct instead of all truncating to the same 64-byte prefix.
                safe_disambig = hashlib.sha256(safe_disambig.encode("utf-8")).hexdigest()[:20]
            preserved_suffix = f" [{safe_disambig}]"
            stem = f"{stem}{preserved_suffix}"
        extension = SUPPORTED_FORMATS[self.audio_format]["ext"]
        if self.include_track_number and track_num is not None:
            filename = f"{track_num:02d}. {stem}.{extension}"
        else:
            filename = f"{stem}.{extension}"
        return cap_filename(filename, preserve_stem_suffix=preserved_suffix)

    def format_playlist_name(self, metadata: PlaylistInfo):
        owner = metadata.owner or "Spotify"
        return f"{metadata.name} - {owner}".strip(" -")

    def prepare_playlist_folder(self, base_folder, playlist_name):
        os.makedirs(base_folder, exist_ok=True)
        # Same cross-platform sanitizer as track files (windows reserved
        # chars/device names, posix rules - see sanitize_filename's doc refs).
        safe_name = sanitize_filename(playlist_name)
        if not safe_name or safe_name == "Unknown":
            safe_name = "Sunnify Playlist"
        playlist_folder = os.path.join(base_folder, safe_name)
        # If the ascii-allowlist-era folder name ("Name  Owner") exists, keep
        # using it so re-runs resume there instead of orphaning the manifest (#40).
        legacy_name = "".join(
            ch for ch in playlist_name if ch.isalnum() or ch in (" ", "_")
        ).strip()
        legacy_folder = os.path.join(base_folder, legacy_name)
        if legacy_name and legacy_name != safe_name and os.path.isdir(legacy_folder):
            playlist_folder = legacy_folder
        try:
            os.makedirs(playlist_folder, exist_ok=True)
        except OSError:
            log.error("could not create playlist folder %r", playlist_folder, exc_info=True)
            raise
        return playlist_folder

    @staticmethod
    def _widen_search(search_query: str) -> str:
        """Search several YouTube results instead of only the top hit.

        A track's #1 result can be region-locked or removed; `ytsearch1`
        fails the whole download in that case (closes #42). Widening to
        `ytsearch5` lets yt-dlp skip unavailable results and download the
        first one that actually plays.
        """
        if search_query.startswith("ytsearch1:"):
            return "ytsearch5:" + search_query[len("ytsearch1:") :]
        return search_query

    @staticmethod
    def _simplify_search(search_query: str) -> str:
        """Strip parenthetical/bracketed qualifiers for a looser fallback.

        Hyper-specific titles (classical works like `(Wiegenlied, Op. 49,
        No. 4)`, tone tracks like `(528 Hz)`) can return zero YouTube
        matches. Dropping the qualifiers widens the net on a second attempt.
        Returns the original query unchanged if there is nothing to strip.
        """
        _, sep, terms = search_query.partition(":")
        if not sep:
            terms = search_query
        stripped = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", terms)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if not stripped or stripped == terms.strip():
            return search_query
        return f"ytsearch5:{stripped}"

    @staticmethod
    def _split_artist_names(artists: str | None) -> list[str]:
        """Split Spotify's collaboration string without losing name words."""
        if not artists:
            return []
        return [
            token.strip()
            for token in re.split(
                r"[,&•]+|\s+(?:feat\.?|ft\.?)\s+",
                artists,
                flags=re.IGNORECASE,
            )
            if token.strip()
        ]

    @classmethod
    def _topic_search_query(cls, expected_title: str | None, expected_artists: str | None) -> str:
        """Build a targeted retry for YouTube's auto-generated audio catalog."""
        artists = cls._split_artist_names(expected_artists)
        if not expected_title or not artists:
            return ""
        # YouTube treats an unspaced ampersand inconsistently in search.  The
        # spaced form exposes the Sabl3 Topic item for ``S&M``.
        title = re.sub(r"\s*&\s*", " & ", expected_title)
        title = re.sub(r"\s+", " ", title).strip()
        return f"ytsearch5:{title} {artists[0]} Topic"

    @staticmethod
    def _quote_youtube_search_term(value: str | None) -> str:
        """Quote one internally generated YouTube search term.

        Quotes materially improve catalog discovery for titles containing
        generic words (``in the pool``) and version suffixes.  Strip literal
        quotes first so Spotify metadata cannot escape the generated term.
        """
        cleaned = re.sub(r"\s+", " ", str(value or "").replace('"', " ")).strip()
        return f'"{cleaned}"' if cleaned else ""

    @classmethod
    def _catalog_search_queries(
        cls,
        expected_title: str | None,
        expected_artists: str | None,
        expected_album: str | None,
    ) -> list[str]:
        """Return narrow discovery retries for official/catalog recordings.

        YouTube search does not expose every art track through the same words:
        some need the parenthesized edition name, some the album, and some the
        auto-generation phrase.  These queries only discover candidates; the
        selector still validates their full metadata before accepting them.
        """
        artists = cls._split_artist_names(expected_artists)
        if not expected_title or not artists:
            return []

        title = cls._quote_youtube_search_term(expected_title)
        artist = cls._quote_youtube_search_term(artists[0])
        queries: list[str] = []

        def add(terms: str):
            query = f"ytsearch5:{terms}"
            if query not in queries:
                queries.append(query)

        # Spotify spells release editions as ``Title - Video Edit`` while
        # YouTube's exact catalog title commonly uses parentheses.
        head, separator, suffix = str(expected_title).rpartition(" - ")
        if separator and cls._SPOTIFY_VARIANT_SUFFIX_RE.fullmatch(suffix.strip()) and head.strip():
            edition = cls._quote_youtube_search_term(f"{head.strip()} ({suffix.strip()})")
            add(f"{edition} {artist}")

        add(f'{title} {artist} "Auto-generated by YouTube"')
        if expected_album:
            album = cls._quote_youtube_search_term(expected_album)
            soundtrack_title = cls._quote_youtube_search_term(
                f"{expected_title} ({expected_album})"
            )
            add(f"{soundtrack_title} {artist}")
            add(f"{title} {album} {artist}")
        return queries

    # Max spotify-vs-youtube length gap to count as the same recording; the
    # top hit is often the music video or an extended cut.
    _DURATION_TOLERANCE_S = 7

    # Wider bound for the title+duration combined check: right title but >30s
    # off means remix/live/extended - fail loudly rather than ship it.
    _DURATION_TOLERANCE_S_WIDE = 30

    # Only these suffixes are release/version qualifiers.  A blanket split on
    # ``" - "`` corrupts real song names such as ``Emil - Despair`` and
    # bilingual titles such as ``エマニエル - Emmanuelle``.
    _SPOTIFY_VARIANT_SUFFIX_RE = re.compile(
        r"^(?:"
        r"(?:\d{4}\s+)?(?:re)?master(?:ed)?(?:\s+\d{4})?|"
        r"live(?:\s+.*)?|"
        r"from\b.+|"
        r"(?:(?:radio|video|single|album|alternate|alternative|acoustic|extended|"
        r"original|club|mono|stereo)\s+)?(?:edit|version|mix)|"
        r"remix(?:\s+edit)?|"
        r"demo|instrumental|karaoke|acoustic|mono|stereo"
        r")$",
        flags=re.IGNORECASE,
    )

    @staticmethod
    def _normalize_title(s: str | None) -> str:
        """Lowercase, strip diacritics, drop bracketed segments + `feat./ft.`
        tails, collapse to word characters + spaces (any script). Used on
        BOTH sides of title comparison.

        Critically: this does NOT split on ` - ` because YouTube titles
        commonly use the `Artist - Song` convention; splitting would turn
        "The Weeknd - Blinding Lights" into just "The Weeknd" and lose the
        song name. Spotify-side variant stripping (`Title - Remastered`)
        is handled separately in `_spotify_title_core`.
        """
        if not s:
            return ""
        import unicodedata

        # NFKD + selective mark folding: "Café" -> "Cafe".  Japanese
        # dakuten/handakuten and Cyrillic marks are letters semantically, not
        # optional accents (ど must not collapse to と; й must not become и).
        # Preserve those marks and NFC-recompose them after compatibility
        # normalization.  The previous behavior remains for Latin/Greek and
        # for script-neutral diacritics.
        folded = []
        base_name = ""
        for char in unicodedata.normalize("NFKD", s):
            if unicodedata.combining(char):
                semantic_mark = char in ("\u3099", "\u309a") or (
                    "CYRILLIC" in base_name and char in ("\u0306", "\u0308")
                )
                if not semantic_mark:
                    continue
            else:
                base_name = unicodedata.name(char, "")
            folded.append(char)
        s = unicodedata.normalize("NFC", "".join(folded))
        s = s.lower()
        s = re.sub(r"\([^)]*\)", " ", s)  # "Hello (Remix)" -> "Hello "
        s = re.sub(r"\[[^\]]*\]", " ", s)  # "Hello [Edit]" -> "Hello "
        s = re.sub(r"\b(feat\.?|ft\.?)\s+.*$", "", s, flags=re.IGNORECASE)
        # Strip apostrophes BEFORE the general punctuation->space step so
        # "I'm" becomes "im" not "i m".
        s = s.replace("'", "").replace("’", "")
        # Keep letters/digits/marks from ANY script (#77). Category M is
        # load-bearing: indic/thai vowel signs are combining-class-0 marks,
        # and dropping them collapses distinct words ("दिल"/"दाल").
        s = "".join(
            ch if (ch.isspace() or unicodedata.category(ch)[0] in "LNM") else " " for ch in s
        )
        s = re.sub(r"\s+", " ", s).strip()
        return s

    @staticmethod
    def _spotify_title_core(s: str | None) -> str:
        """Drop a recognized ` - Variant` suffix Spotify uses for releases.

        Examples:
            "Bohemian Rhapsody - Remastered 2011" -> "Bohemian Rhapsody"
            "Hello - Live"                        -> "Hello"
            "Sweet Disposition - Remix Edit"      -> "Sweet Disposition"
            "Take-Off"                            -> "Take-Off" (literal hyphen, no spaces)
            "Mi Gente"                            -> "Mi Gente"

        Only applied to the Spotify-side title before fuzzy comparison so
        a YouTube upload titled just "Bohemian Rhapsody" still matches.
        Don't apply this to YouTube titles - they use ` - ` for
        `Artist - Song` and the strip would lose the song name.
        """
        if not s:
            return ""
        core = s
        while True:
            head, separator, suffix = core.rpartition(" - ")
            if not separator or not MusicScraper._SPOTIFY_VARIANT_SUFFIX_RE.fullmatch(
                suffix.strip()
            ):
                return core
            core = head

    @staticmethod
    def _has_latin_letters(value: str) -> bool:
        return any(
            unicodedata.category(char).startswith("L") and "LATIN" in unicodedata.name(char, "")
            for char in value
        )

    @classmethod
    def _has_non_latin_letters(cls, value: str) -> bool:
        return any(
            unicodedata.category(char).startswith("L") and "LATIN" not in unicodedata.name(char, "")
            for char in value
        )

    @classmethod
    def _looks_like_title_qualifier(cls, value: str) -> bool:
        """Reject descriptive/version text masquerading as an alias."""
        normalized = cls._normalize_title(value)
        qualifier_phrases = (
            "acoustic",
            "audio",
            "edit",
            "english version",
            "instrumental",
            "karaoke",
            "live",
            "lyric video",
            "lyrics",
            "official audio",
            "official video",
            "original soundtrack",
            "ost",
            "radio edit",
            "remaster",
            "remastered",
            "remix",
            "romanization",
            "romanized",
            "soundtrack",
            "translation",
            "translated",
            "tv size",
            "version",
            "video",
        )
        return any(cls._contains_token_sequence(normalized, phrase) for phrase in qualifier_phrases)

    @classmethod
    def _spotify_title_targets(cls, title: str | None) -> list[str]:
        """Return normalized canonical and explicitly supplied title aliases.

        Spotify sometimes stores a native title and its Latin alias on either
        side of `` - `` or inside parentheses.  Those are safe aliases because
        both spellings came from Spotify itself; arbitrary transliteration or
        fuzzy spelling remains outside the strict gate.
        """
        if not title:
            return []

        raw = str(title)
        targets = []

        def add(value):
            normalized = cls._normalize_title(value)
            if normalized and normalized not in targets:
                targets.append(normalized)

        core = cls._spotify_title_core(raw)
        add(core)

        parts = [part.strip() for part in core.split(" - ") if part.strip()]
        if (
            len(parts) == 2
            and not any(cls._looks_like_title_qualifier(part) for part in parts)
            and any(cls._has_latin_letters(part) for part in parts)
            and any(cls._has_non_latin_letters(part) for part in parts)
        ):
            for part in parts:
                add(part)

        compatible = unicodedata.normalize("NFKC", raw)
        outside = re.sub(r"\([^)]*\)|\[[^]]*]", " ", compatible)
        if cls._has_non_latin_letters(outside) and not cls._has_latin_letters(outside):
            for group in re.findall(r"\(([^)]*)\)|\[([^]]*)]", compatible):
                bracket_text = next((item for item in group if item), "")
                for latin_run in re.findall(
                    r"[A-Za-z0-9]+(?:[\s'’&.+-]+[A-Za-z0-9]+)*", bracket_text
                ):
                    alias = latin_run.strip(" .-+")
                    normalized_alias = cls._normalize_title(alias)
                    if (
                        len(re.sub(r"[^a-z0-9]", "", normalized_alias)) >= 4
                        and not cls._looks_like_title_qualifier(normalized_alias)
                        and not normalized_alias.startswith(("feat ", "ft "))
                    ):
                        add(alias)

        return targets

    @staticmethod
    def _contains_token_sequence(haystack: str, needle: str) -> bool:
        """Match a normalized phrase on token boundaries.

        This makes the two-token normalized title ``S&M`` (``s m``) work,
        while preventing ``His Dream`` from matching ``This Dream``.
        """
        haystack_tokens = haystack.split()
        needle_tokens = needle.split()
        width = len(needle_tokens)
        if not width or width > len(haystack_tokens):
            return False
        return any(
            haystack_tokens[index : index + width] == needle_tokens
            for index in range(len(haystack_tokens) - width + 1)
        )

    @classmethod
    def _normalize_preserving_bracket_text(cls, value: str | None) -> str:
        """Normalize text while retaining parenthetical/bracketed credits."""
        compatible = unicodedata.normalize("NFKC", str(value or ""))
        unwrapped = re.sub(r"[()\[\]{}]", " ", compatible)
        return cls._normalize_title(unwrapped)

    @classmethod
    def _title_plausibly_matches(cls, yt_title: str | None, expected_title: str | None) -> bool:
        """True when the YouTube candidate's title could reasonably be the
        Spotify track. Recognized Spotify release variants are stripped and
        explicit cross-script aliases are retained. Both sides then match as
        contiguous token sequences, so punctuation-only short names work but
        ``His Dream`` cannot match ``This Dream``."""
        yt = cls._normalize_title(yt_title)
        if not yt:
            return False
        return any(
            cls._contains_token_sequence(yt, target)
            for target in cls._spotify_title_targets(expected_title)
        )

    @classmethod
    def _candidate_attribution_matches_any_artist(cls, candidate, artist_tokens) -> bool:
        """Match artists only against channel/uploader attribution fields."""
        attribution_fields = [
            cls._normalize_title(candidate.get(field) or "") for field in ("channel", "uploader")
        ]
        compact_suffixes = ("official", "music", "topic", "vevo")

        for artist in artist_tokens:
            if any(cls._contains_token_sequence(field, artist) for field in attribution_fields):
                return True
            compact_artist = artist.replace(" ", "")
            if len(compact_artist) < 6:
                continue
            for field in attribution_fields:
                compact_field = field.replace(" ", "")
                if compact_field == compact_artist or any(
                    compact_field == compact_artist + suffix for suffix in compact_suffixes
                ):
                    return True
        return False

    @classmethod
    def _candidate_matches_any_artist(cls, candidate, artist_tokens) -> bool:
        """Match Spotify artists against a video's title or attribution.

        yt-dlp search entries expose the channel/uploader separately, and
        auto-generated audio titles often omit the artist entirely. A narrow
        compact comparison also handles channel brands such as ``DuduFaruk``
        for Spotify's ``Dudu Faruk`` without weakening short/common names.
        """
        # Keep bracket contents for credits: in
        # ``Rihanna - S&M (METAL COVER BY SABL3)`` the expected artist only
        # appears inside parentheses.  Title matching intentionally drops
        # those contents, but artist matching must not.
        title = cls._normalize_preserving_bracket_text(candidate.get("title"))
        for artist in artist_tokens:
            if cls._contains_token_sequence(title, artist):
                return True
        return cls._candidate_attribution_matches_any_artist(candidate, artist_tokens)

    @classmethod
    def _has_conflicting_audio_mix_marker(cls, candidate, expected_title) -> bool:
        """Reject the explicit fan-edit label seen in the S&M failure."""
        candidate_title = cls._normalize_preserving_bracket_text(candidate.get("title"))
        expected = cls._normalize_preserving_bracket_text(expected_title)
        return cls._contains_token_sequence(
            candidate_title, "audio mix"
        ) and not cls._contains_token_sequence(expected, "audio mix")

    @classmethod
    def _metadata_matches_any_artist(cls, candidate, artist_tokens) -> bool:
        """Match artists against hydrated YouTube Music metadata.

        These fields are absent from flat search results, but are useful as
        high-confidence evidence after a candidate has passed the tight
        duration and official-catalog gates.
        """
        if cls._candidate_attribution_matches_any_artist(candidate, artist_tokens):
            return True

        values = []
        for field in ("artist", "creator"):
            if candidate.get(field):
                values.append(candidate[field])
        raw_artists = candidate.get("artists") or []
        if isinstance(raw_artists, str):
            values.append(raw_artists)
        else:
            values.extend(raw_artists)
        raw_tags = candidate.get("tags") or []
        if isinstance(raw_tags, str):
            values.append(raw_tags)
        else:
            values.extend(raw_tags)

        normalized_values = [cls._normalize_title(str(value)) for value in values]
        for value in normalized_values:
            for artist in artist_tokens:
                if cls._contains_token_sequence(value, artist):
                    return True
                compact_artist = artist.replace(" ", "")
                compact_value = value.replace(" ", "")
                if len(compact_artist) >= 3 and compact_value == compact_artist:
                    return True
        return False

    @classmethod
    def _structured_metadata_artist_matches(
        cls, candidate, artist_tokens, *, require_all=False
    ) -> bool:
        """Match artists using structured fields, never free-form tags.

        The relaxed localized-title path depends on this stricter form: tags
        are uploader-controlled on ordinary videos, whereas ``artist`` and
        ``artists`` come from yt-dlp's parsed music metadata on art tracks.
        """
        values = []
        for field in ("artist", "creator"):
            if candidate.get(field):
                values.append(candidate[field])
        for field in ("artists", "creators"):
            raw_values = candidate.get(field) or []
            if isinstance(raw_values, str):
                values.append(raw_values)
            else:
                values.extend(raw_values)

        normalized_values = [cls._normalize_title(str(value)) for value in values]

        def matches(artist):
            compact_artist = artist.replace(" ", "")
            for value in normalized_values:
                if cls._contains_token_sequence(value, artist):
                    return True
                if len(compact_artist) >= 3 and value.replace(" ", "") == compact_artist:
                    return True
            return False

        if not artist_tokens:
            return False
        decisions = [matches(artist) for artist in artist_tokens]
        return all(decisions) if require_all else any(decisions)

    @classmethod
    def _metadata_title_matches(cls, candidate, expected_title) -> bool:
        """Look for a Spotify title/alias in hydrated catalog metadata."""
        values = [candidate.get(field) for field in ("title", "track", "alt_title")]
        tags = candidate.get("tags") or []
        if isinstance(tags, str):
            values.append(tags)
        else:
            values.extend(tags)
        return any(
            value and cls._title_plausibly_matches(str(value), expected_title) for value in values
        )

    @classmethod
    def _opposite_script_titles(cls, left: str | None, right: str | None) -> bool:
        """True only for pure-Latin vs pure-non-Latin title spellings.

        This deliberately does not call arbitrary same-script mismatches an
        alias.  In particular, artist/duration evidence can never turn
        ``Mi Chico`` into ``Mi Gente`` or ``This Dream`` into ``His Dream``.
        """
        left = cls._normalize_title(left)
        right = cls._normalize_title(right)
        if not left or not right:
            return False
        left_latin = cls._has_latin_letters(left)
        left_other = cls._has_non_latin_letters(left)
        right_latin = cls._has_latin_letters(right)
        right_other = cls._has_non_latin_letters(right)
        return (left_latin and not left_other and right_other and not right_latin) or (
            right_latin and not right_other and left_other and not left_latin
        )

    @classmethod
    def _candidate_song_title(cls, candidate, artist_tokens) -> str:
        """Extract a song spelling from a verified artist-channel title.

        Used only as a discovery bridge.  For example, an official search hit
        ``【MV】Creepy Nuts - 助演男優賞`` can safely teach the retry query the
        Japanese spelling; the eventual audio still has to pass the full
        catalog, duration, artist, and ambiguity gates.
        """
        raw = unicodedata.normalize("NFKC", str(candidate.get("title") or "")).strip()
        while True:
            stripped = re.sub(
                r"^\s*(?:\[[^\]]+\]|【[^】]+】|\([^)]*\))\s*",
                "",
                raw,
                count=1,
            )
            if stripped == raw:
                break
            raw = stripped

        parts = [part.strip() for part in re.split(r"\s+(?:-|–|—|\|)\s+", raw) if part.strip()]
        if len(parts) > 1:
            normalized_parts = [cls._normalize_title(part) for part in parts]

            def is_artist_part(value):
                return any(cls._contains_token_sequence(value, artist) for artist in artist_tokens)

            if is_artist_part(normalized_parts[0]):
                raw = " - ".join(parts[1:])
            elif is_artist_part(normalized_parts[-1]):
                raw = " - ".join(parts[:-1])
        return raw.strip()

    @staticmethod
    def _has_structured_catalog_fields(candidate) -> bool:
        return bool(
            candidate.get("track")
            and candidate.get("album")
            and (candidate.get("artist") or candidate.get("artists"))
        )

    @staticmethod
    def _catalog_provenance(candidate) -> tuple[bool, bool]:
        """Return ``(real_topic, verified_channel)`` for hydrated metadata."""
        channel = str(candidate.get("channel") or "").strip()
        uploader = str(candidate.get("uploader") or "").strip()
        description = str(candidate.get("description") or "").casefold()
        has_music_fields = MusicScraper._has_structured_catalog_fields(candidate)

        # A display name is user-controlled.  Treat `` - Topic`` as catalog
        # provenance only when full extraction also exposes YouTube Music's
        # structured fields and auto-generation notice.
        real_topic = bool(
            channel
            and uploader
            and channel.casefold() == uploader.casefold()
            and channel.casefold().endswith(" - topic")
            and has_music_fields
            and "auto-generated by youtube" in description
        )
        # A verified channel is only catalog provenance when the full item is
        # itself an auto-generated art track.  Verification alone says who
        # uploaded a video, not which recording its free-form tags describe.
        verified_catalog = bool(
            candidate.get("channel_is_verified") is True
            and has_music_fields
            and "auto-generated by youtube" in description
        )
        return real_topic, verified_catalog

    @classmethod
    def _has_conflicting_version_marker(cls, candidate, expected_title) -> bool:
        """Reject obvious alternate recordings on the relaxed catalog path."""
        expected = cls._normalize_preserving_bracket_text(expected_title)
        candidate_title = " ".join(
            cls._normalize_preserving_bracket_text(candidate.get(field))
            for field in ("title", "track", "alt_title")
            if candidate.get(field)
        )
        marker_phrases = (
            "arranged by",
            "complete edit",
            "cover",
            "instrumental",
            "karaoke",
            "live",
            "mashup",
            "nightcore",
            "originally performed by",
            "piano",
            "radio edit",
            "remix",
            "slowed",
            "sped up",
            "video edit",
            "アレンジ",
            "インスト",
            "オフボーカル",
            "カバー",
            "カラオケ",
            "ピアノ",
            "リミックス",
            "歌ってみた",
        )
        return any(
            cls._contains_token_sequence(candidate_title, marker)
            and not cls._contains_token_sequence(expected, marker)
            for marker in marker_phrases
        )

    def _select_youtube_match(
        self,
        search_query,
        expected_duration_s,
        expected_title=None,
        expected_artists=None,
        expected_album=None,
        excluded_video_urls=None,
    ):
        """Return the best YouTube watch URL for a search, or None.

        Selection policy (closes #52):
          1. Filter to candidates whose title plausibly matches the Spotify
             track title (a token-boundary match against canonical/explicit
             alias forms). If
             none do, retry YouTube's auto-generated Topic catalog for the
             exact canonical title before rejecting the track.
             Rules out the failure mode where YouTube's top hit is a
             DIFFERENT track by the SAME artist with a similar duration -
             e.g. searching "Mi Gente DJ Goja audio" returns "Dj Goja -
             Mi Chico" at the top, only ~2s off the real Mi Gente, and the
             prior pure-duration matcher would happily pick it and write
             Mi Gente metadata onto Mi Chico audio.
          2. Require an artist to plausibly appear in the YouTube title,
             channel, or uploader attribution. All-native artist names retain
             the existing title-only fallback because YouTube often
             romanizes them.
          3. Among the resulting pool, pick the duration-closest if duration
             is known, but reject the whole result if even the best
             candidate's duration is >30s off the Spotify track - that means
             the closest title-matching upload is a remix / live cover /
             extended edit, and shipping a 5-minute remix under a 2-minute
             track's metadata still corrupts the library.
          4. On a failed strict gate, retry a targeted Topic query. A localized
             title may be recovered only after hydrating the otherwise absent
             catalog metadata, proving official/Topic provenance, matching an
             artist, staying within seven seconds, and remaining unambiguous.
             Otherwise return None rather than ship the wrong audio.

        `expected_title` + `expected_artists` are optional so legacy callers
        that haven't been updated still work via the older trust-the-top-
        hit-unless-duration-is-clearly-off policy.
        """
        select_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "ignoreerrors": True,
            "retries": 5,
            "socket_timeout": 15,
            "concurrent_fragment_downloads": 4,
        }
        log.debug(
            "yt search: query=%r title=%r artists=%r album=%r dur=%ss",
            search_query,
            expected_title,
            expected_artists,
            expected_album,
            expected_duration_s,
        )

        def fetch_entries(query):
            if self.is_cancelled() or self._youtube_is_blocked():
                return None
            try:
                with self._youtube_dl(select_opts) as ydl:
                    info = ydl.extract_info(query, download=False)
            except Exception as exc:
                # A real exception here (bot-challenge, SSL, network, region
                # block) is otherwise invisible to the caller.
                log.warning(
                    "yt search raised %s for %r: %s",
                    type(exc).__name__,
                    query,
                    str(exc)[:300],
                )
                self._note_if_network_blocked(str(exc))
                return None
            found = [
                e
                for e in (info or {}).get("entries", [])
                if e
                and e.get("id")
                and f"https://www.youtube.com/watch?v={e['id']}" not in (excluded_video_urls or ())
            ]
            log.debug("yt search returned %d entries for %r", len(found), query)
            return found

        hydrated_cache = {}

        def hydrate_entry(entry):
            """Fetch fields omitted by ``extract_flat`` for one candidate."""
            if self.is_cancelled() or self._youtube_is_blocked():
                return None
            entry_id = entry.get("id")
            if entry_id in hydrated_cache:
                return hydrated_cache[entry_id]
            hydrate_opts = dict(select_opts)
            hydrate_opts.pop("extract_flat", None)
            hydrate_opts["noplaylist"] = True
            video_url = f"https://www.youtube.com/watch?v={entry['id']}"
            try:
                with self._youtube_dl(hydrate_opts) as ydl:
                    hydrated = ydl.extract_info(video_url, download=False)
            except Exception as exc:
                log.debug(
                    "yt metadata hydration raised %s for %s: %s",
                    type(exc).__name__,
                    entry["id"],
                    str(exc)[:200],
                )
                self._note_if_network_blocked(str(exc))
                hydrated_cache[entry_id] = None
                return None
            if not hydrated:
                hydrated_cache[entry_id] = None
                return None
            merged = dict(hydrated)
            for key, value in entry.items():
                current = merged.get(key)
                if current is None or (isinstance(current, str) and not current.strip()):
                    merged[key] = value
            hydrated_cache[entry_id] = merged
            return merged

        entries = fetch_entries(search_query)
        if entries is None:
            return None

        artist_tokens = []
        if expected_artists:
            # Split on collaboration separators BEFORE normalizing -
            # normalization eats commas/bullets, which would collapse the
            # multi-artist string into one unmatchable token.
            raw_tokens = self._split_artist_names(expected_artists)
            artist_tokens = [self._normalize_title(t) for t in raw_tokens]
            artist_tokens = [t for t in artist_tokens if t]

        if expected_title:
            all_native_artists = bool(artist_tokens) and not any(
                re.search(r"[a-z0-9]", token) for token in artist_tokens
            )

            def evaluate(candidate_entries, *, require_attribution=False):
                title_ok = [
                    entry
                    for entry in candidate_entries
                    if self._title_plausibly_matches(entry.get("title"), expected_title)
                    and not self._has_conflicting_audio_mix_marker(entry, expected_title)
                ]
                if not title_ok:
                    return None, "title", title_ok, None, False

                pool = title_ok
                native_artist_fallback = False
                if artist_tokens:
                    attribution_pool = [
                        entry
                        for entry in title_ok
                        if self._candidate_attribution_matches_any_artist(entry, artist_tokens)
                    ]
                    artist_pool = [
                        entry
                        for entry in title_ok
                        if self._candidate_matches_any_artist(entry, artist_tokens)
                    ]
                    # A matching channel/uploader is stronger evidence than
                    # an artist name typed into a fan video's title.  Keep the
                    # historical title-credit fallback when no attributed
                    # result exists (label uploads commonly need it).
                    pool = attribution_pool or ([] if require_attribution else artist_pool)
                    if not pool and all_native_artists and not require_attribution:
                        pool = title_ok
                        native_artist_fallback = True
                    if not pool:
                        return None, "artist", title_ok, None, False

                chosen = pool[0]
                if expected_duration_s:
                    timed = [entry for entry in pool if entry.get("duration")]
                    if timed:
                        chosen = min(
                            timed,
                            key=lambda entry: abs(entry["duration"] - expected_duration_s),
                        )
                        off = abs(chosen["duration"] - expected_duration_s)
                        if off > self._DURATION_TOLERANCE_S_WIDE:
                            return None, "duration", title_ok, off, native_artist_fallback
                return chosen, "ok", title_ok, None, native_artist_fallback

            def recover_from_catalog(
                candidate_entries, *, trusted_aliases=(), allow_cross_script=False
            ):
                """Recover an official art track from fully hydrated metadata.

                Exact aliases present in YouTube's structured catalog/tags use
                the existing seven-second tolerance.  A title whose spelling
                switches scripts is substantially narrower: it needs an art
                track, structured artist evidence, at most four seconds of
                drift, and either the exact Spotify album or a spelling learned
                from a verified artist-channel search result.
                """
                if not expected_duration_s or not artist_tokens:
                    return None

                normalized_expected_album = self._normalize_title(expected_album)
                trusted_aliases = {
                    self._normalize_title(alias) for alias in trusted_aliases if alias
                }
                exact_aliases = []
                cross_script_aliases = []
                seen_ids = set()

                for entry in candidate_entries:
                    entry_id = entry.get("id")
                    duration = entry.get("duration")
                    if not entry_id or entry_id in seen_ids or not duration:
                        continue
                    seen_ids.add(entry_id)
                    delta = abs(duration - expected_duration_s)
                    if delta > self._DURATION_TOLERANCE_S:
                        continue

                    hydrated = hydrate_entry(entry)
                    if not hydrated or self._has_conflicting_version_marker(
                        hydrated, expected_title
                    ):
                        continue

                    real_topic, verified_catalog = self._catalog_provenance(hydrated)
                    if not (real_topic or verified_catalog):
                        continue

                    album = self._normalize_title(hydrated.get("album"))
                    album_exact = bool(
                        normalized_expected_album and album == normalized_expected_album
                    )
                    artist_match = self._metadata_matches_any_artist(hydrated, artist_tokens)
                    metadata_title = self._metadata_title_matches(hydrated, expected_title)

                    # A declared canonical alias is strong, but when Spotify
                    # supplied an album it must identify the same release.
                    # This is what keeps licensed karaoke/tribute records out.
                    if (
                        metadata_title
                        and artist_match
                        and (not normalized_expected_album or album_exact)
                    ):
                        exact_aliases.append(hydrated)
                        continue

                    if not allow_cross_script or delta > 4:
                        continue
                    track_title = (
                        hydrated.get("track") or hydrated.get("alt_title") or hydrated.get("title")
                    )
                    track_normalized = self._normalize_title(track_title)
                    if not self._opposite_script_titles(expected_title, track_title):
                        continue
                    if not self._has_structured_catalog_fields(hydrated):
                        continue

                    # Exact album identity can itself bridge the localized
                    # spelling, but multi-artist tracks must prove every
                    # credited artist.  Compilation reissues legitimately use
                    # a different album; those instead need a verified OAC
                    # result that supplied this exact alternate spelling.
                    if album_exact:
                        artist_ok = self._structured_metadata_artist_matches(
                            hydrated,
                            artist_tokens,
                            require_all=len(artist_tokens) > 1,
                        )
                    else:
                        artist_ok = (
                            track_normalized in trusted_aliases
                            and self._structured_metadata_artist_matches(hydrated, artist_tokens)
                        )
                    if artist_ok:
                        cross_script_aliases.append((hydrated, album_exact))

                qualified = exact_aliases
                if not qualified and cross_script_aliases:
                    exact_album_matches = [
                        candidate for candidate, album_exact in cross_script_aliases if album_exact
                    ]
                    qualified = exact_album_matches or [
                        candidate for candidate, _ in cross_script_aliases
                    ]

                # Search retries can surface the same video repeatedly.
                unique = {candidate.get("id"): candidate for candidate in qualified}
                if len(unique) == 1:
                    return next(iter(unique.values()))
                if len(unique) > 1:
                    log.info(
                        "official catalog retry ambiguous for %r (%d candidates: %s), rejecting",
                        expected_title,
                        len(unique),
                        ", ".join(str(candidate_id) for candidate_id in unique),
                    )
                return None

            def merge_entries(*entry_groups):
                merged = []
                seen = set()
                for group in entry_groups:
                    for entry in group or []:
                        entry_id = entry.get("id")
                        if entry_id and entry_id not in seen:
                            seen.add(entry_id)
                            merged.append(entry)
                return merged

            def discover_verified_aliases(*entry_groups):
                """Learn one opposite-script spelling per search result set."""
                aliases = []
                for group in entry_groups:
                    for entry in group or []:
                        if not self._candidate_attribution_matches_any_artist(entry, artist_tokens):
                            continue
                        candidate = entry
                        if candidate.get("channel_is_verified") is not True:
                            candidate = hydrate_entry(entry)
                        if not candidate or candidate.get("channel_is_verified") is not True:
                            continue
                        alias = self._candidate_song_title(candidate, artist_tokens)
                        if self._opposite_script_titles(expected_title, alias):
                            aliases.append(alias)
                        # The highest-ranked verified artist result is the only
                        # spelling this search group is allowed to teach us.
                        break
                return aliases

            chosen, failure, title_ok, duration_off, used_native_fallback = evaluate(entries)

            if chosen is None:
                # YouTube can suppress an art track from ordinary results.
                # Start with the cheap historical Topic retry, then use narrow
                # quoted/album searches and verified artist-title bridges.
                topic_query = self._topic_search_query(expected_title, expected_artists)
                topic_entries = (
                    fetch_entries(topic_query)
                    if topic_query and topic_query != search_query
                    else None
                )
                topic_chosen, _, _, _, topic_native_fallback = evaluate(topic_entries or [])
                if topic_chosen is not None:
                    if topic_native_fallback:
                        log.info(
                            "artist gate skipped: no latin token in %r, falling back to title-only",
                            expected_artists,
                        )
                    log.info("exact title recovered via targeted Topic search: %r", expected_title)
                    log.debug("selected youtube video %s", topic_chosen["id"])
                    return f"https://www.youtube.com/watch?v={topic_chosen['id']}"

                catalog_chosen = recover_from_catalog(topic_entries or [])
                if catalog_chosen is not None:
                    log.info(
                        "localized title recovered via official catalog metadata: %r -> %r",
                        expected_title,
                        catalog_chosen.get("track")
                        or catalog_chosen.get("alt_title")
                        or catalog_chosen.get("title"),
                    )
                    log.debug("selected youtube video %s", catalog_chosen["id"])
                    return f"https://www.youtube.com/watch?v={catalog_chosen['id']}"

                search_groups = [entries, topic_entries or []]
                catalog_entries = merge_entries(*search_groups)
                trusted_aliases = discover_verified_aliases(*search_groups)

                # A verified official search result can expose the localized
                # spelling even when it is a long music video.  Search that
                # exact spelling for the duration-matched catalog audio.
                used_queries = {search_query, topic_query}
                raw_artists = self._split_artist_names(expected_artists)
                quoted_artist = self._quote_youtube_search_term(
                    raw_artists[0] if raw_artists else ""
                )
                queried_aliases = set()

                def search_trusted_aliases():
                    nonlocal catalog_entries
                    for alias in list(trusted_aliases):
                        normalized_alias = self._normalize_title(alias)
                        if not normalized_alias or normalized_alias in queried_aliases:
                            continue
                        queried_aliases.add(normalized_alias)
                        alias_query = (
                            f"ytsearch5:{self._quote_youtube_search_term(alias)} "
                            f"{quoted_artist} Topic"
                        )
                        if alias_query in used_queries:
                            continue
                        used_queries.add(alias_query)
                        alias_entries = fetch_entries(alias_query) or []
                        search_groups.append(alias_entries)
                        catalog_entries = merge_entries(catalog_entries, alias_entries)

                search_trusted_aliases()
                catalog_chosen = recover_from_catalog(
                    catalog_entries,
                    trusted_aliases=trusted_aliases,
                    allow_cross_script=True,
                )
                if catalog_chosen is not None:
                    log.info(
                        "localized title recovered via verified catalog bridge: %r -> %r",
                        expected_title,
                        catalog_chosen.get("track")
                        or catalog_chosen.get("alt_title")
                        or catalog_chosen.get("title"),
                    )
                    log.debug("selected youtube video %s", catalog_chosen["id"])
                    return f"https://www.youtube.com/watch?v={catalog_chosen['id']}"

                for retry_query in self._catalog_search_queries(
                    expected_title, expected_artists, expected_album
                ):
                    if retry_query in used_queries:
                        continue
                    used_queries.add(retry_query)
                    retry_entries = fetch_entries(retry_query) or []
                    search_groups.append(retry_entries)

                    retry_chosen, _, _, _, retry_native_fallback = evaluate(
                        retry_entries, require_attribution=True
                    )
                    if retry_chosen is not None:
                        if retry_native_fallback:
                            log.info(
                                "artist gate skipped: no latin token in %r, falling back to title-only",
                                expected_artists,
                            )
                        log.info(
                            "exact title recovered via targeted catalog search: %r",
                            expected_title,
                        )
                        log.debug("selected youtube video %s", retry_chosen["id"])
                        return f"https://www.youtube.com/watch?v={retry_chosen['id']}"

                    catalog_entries = merge_entries(catalog_entries, retry_entries)
                    new_aliases = discover_verified_aliases(retry_entries)
                    for alias in new_aliases:
                        if self._normalize_title(alias) not in {
                            self._normalize_title(item) for item in trusted_aliases
                        }:
                            trusted_aliases.append(alias)
                    search_trusted_aliases()

                    catalog_chosen = recover_from_catalog(
                        catalog_entries,
                        trusted_aliases=trusted_aliases,
                        allow_cross_script=True,
                    )
                    if catalog_chosen is not None:
                        log.info(
                            "localized title recovered via official catalog metadata: %r -> %r",
                            expected_title,
                            catalog_chosen.get("track")
                            or catalog_chosen.get("alt_title")
                            or catalog_chosen.get("title"),
                        )
                        log.debug("selected youtube video %s", catalog_chosen["id"])
                        return f"https://www.youtube.com/watch?v={catalog_chosen['id']}"

            if chosen is not None:
                if used_native_fallback:
                    log.info(
                        "artist gate skipped: no latin token in %r, falling back to title-only",
                        expected_artists,
                    )
                log.debug("selected youtube video %s", chosen["id"])
                return f"https://www.youtube.com/watch?v={chosen['id']}"

            if not entries:
                # Empty results with no exception is the classic bot-block /
                # rate-limit / region signature. Distinct from filtered hits.
                log.warning(
                    "yt search returned 0 entries for %r (bot-block/network/region?)",
                    search_query,
                )
                return None

            if failure == "title":
                log.info(
                    "title filter rejected all %d candidates for %r (e.g. %r)",
                    len(entries),
                    expected_title,
                    (entries[0].get("title") if entries else None),
                )
                if self.loose_match:
                    return self._loose_pick(entries, expected_duration_s)
                return None
            if failure == "artist":
                log.info(
                    "artist filter rejected all %d title-matches (artists=%r)",
                    len(title_ok),
                    expected_artists,
                )
                if self.loose_match:
                    return self._loose_pick(title_ok, expected_duration_s)
                return None
            if failure == "duration":
                log.info(
                    "closest candidate duration off by %.0fs (>%ss), rejecting",
                    duration_off,
                    self._DURATION_TOLERANCE_S_WIDE,
                )
                return None

        # Legacy path - kept for any caller that hasn't been updated yet.
        if not entries:
            log.warning(
                "yt search returned 0 entries for %r (bot-block/network/region?)", search_query
            )
            return None
        chosen = entries[0]
        if expected_duration_s:
            top_duration = chosen.get("duration")
            top_off = top_duration is None or (
                abs(top_duration - expected_duration_s) > self._DURATION_TOLERANCE_S
            )
            if top_off:
                timed = [e for e in entries if e.get("duration")]
                if timed:
                    chosen = min(timed, key=lambda e: abs(e["duration"] - expected_duration_s))
        return f"https://www.youtube.com/watch?v={chosen['id']}"

    def _loose_pick(self, candidates, expected_duration_s):
        """Opt-in fallback (Settings: "use closest result if no match").

        When strict title/artist matching finds nothing, return the
        duration-closest candidate (or the top result if durations are
        missing). Recovers cross-script matches the ascii title filter can
        never make - e.g. a Latin Spotify title vs a Greek/Cyrillic/CJK
        youtube title. Trades the never-grab-the-wrong-audio guarantee for
        coverage, which is why the caller only reaches here when loose_match
        is on (off by default).
        """
        if not candidates:
            return None
        chosen = candidates[0]
        if expected_duration_s:
            timed = [e for e in candidates if e.get("duration")]
            if timed:
                chosen = min(timed, key=lambda e: abs(e["duration"] - expected_duration_s))
        log.warning("loose match (no confident title/artist match): selected %s", chosen["id"])
        return f"https://www.youtube.com/watch?v={chosen['id']}"

    def download_track_audio(
        self,
        search_query,
        destination,
        expected_duration_s=None,
        expected_title=None,
        expected_artists=None,
        expected_album=None,
    ):
        # Check for FFmpeg first
        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            if sys.platform == "win32":
                instructions = "Install via: winget install Gyan.FFmpeg or choco install ffmpeg"
            elif sys.platform == "darwin":
                instructions = "Install via: brew install ffmpeg (macOS)"
            else:
                instructions = "Install via: apt install ffmpeg (Linux)"
            raise RuntimeError(f"FFmpeg not found! {instructions}")

        fmt = self.audio_format if self.audio_format in SUPPORTED_FORMATS else "mp3"
        ext = SUPPORTED_FORMATS[fmt]["ext"]
        is_lossy = SUPPORTED_FORMATS[fmt]["lossy"]

        base, _ = os.path.splitext(destination)
        output_template = base + ".%(ext)s"
        postprocessor = {
            "key": "FFmpegExtractAudio",
            "preferredcodec": fmt,
        }
        if is_lossy:
            postprocessor["preferredquality"] = self.audio_quality

        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": output_template,
            "ffmpeg_location": ffmpeg_path,
            "retries": 5,
            "socket_timeout": 15,
            "concurrent_fragment_downloads": 4,
            "ignoreerrors": True,
            "postprocessors": [postprocessor],
        }

        def stop_if_cancelled(_status):
            if self.is_cancelled():
                raise InterruptedError("download cancelled")

        ydl_opts["progress_hooks"] = [stop_if_cancelled]
        ydl_opts["postprocessor_hooks"] = [stop_if_cancelled]
        if self.sample_rate != "auto" and fmt in ("mp3", "flac", "wav"):
            # "extractaudio" is the only key yt-dlp matches for this PP.
            # opus excluded (libopus is 48 kHz-only); m4a excluded (may
            # stream-copy aac, where ffmpeg silently drops -ar).
            ydl_opts["postprocessor_args"] = {"extractaudio": ["-ar", self.sample_rate]}

        expected_path = base + "." + ext

        # Widened query then a simplified fallback; success = an audio file
        # actually on disk, so an empty search fails loudly.
        queries = [self._widen_search(search_query)]
        fallback = self._simplify_search(search_query)
        if fallback not in queries:
            queries.append(fallback)

        # yt-dlp's maintained defaults first, then a different client family:
        # youtube bot-challenges per-IP and per-client, so a second family is
        # worth one retry. Never a pinned list - see _retry_player_clients.
        attempts = [("default", ydl_opts)]
        retry_clients = _retry_player_clients()
        if retry_clients:
            fallback_opts = dict(ydl_opts)
            fallback_opts["extractor_args"] = {"youtube": {"player_client": list(retry_clients)}}
            attempts.append(("fallback", fallback_opts))
        attempted_video_urls = set()

        for query in queries:
            if self.is_cancelled():
                raise InterruptedError("download cancelled")
            if self._youtube_is_blocked():
                raise RuntimeError("YouTube blocked this network with a bot check")
            # A selected video can be unavailable or return 403 even though a
            # second title/artist/duration-matched search result is playable.
            # Bound retries so a large playlist cannot spend indefinitely on
            # one track when YouTube blocks every result.
            while len(attempted_video_urls) < 3:
                video_url = self._select_youtube_match(
                    query,
                    expected_duration_s,
                    expected_title=expected_title,
                    expected_artists=expected_artists,
                    expected_album=expected_album,
                    excluded_video_urls=attempted_video_urls,
                )
                if not video_url or video_url in attempted_video_urls:
                    break
                attempted_video_urls.add(video_url)
                for label, opts in attempts:
                    if self.is_cancelled():
                        raise InterruptedError("download cancelled")
                    if self._youtube_is_blocked():
                        raise RuntimeError("YouTube blocked this network with a bot check")
                    # Capture yt-dlp's error even when ignoreerrors swallows it.
                    ytlog = _YtdlpLog()
                    try:
                        with self._youtube_dl({**opts, "logger": ytlog}) as ydl:
                            ydl.extract_info(video_url, download=True)
                    except Exception as exc:
                        log.warning(
                            "download attempt (%s) failed for %s: %s",
                            label,
                            video_url,
                            str(exc)[:300],
                        )
                        self._note_if_network_blocked(str(exc))
                        if self.is_cancelled():
                            raise InterruptedError("download cancelled") from exc
                    else:
                        if not os.path.exists(expected_path):
                            reason = (ytlog.last_error or "no error reported by yt-dlp").strip()
                            log.warning(
                                "download attempt (%s) produced no file for %s: %s",
                                label,
                                video_url,
                                reason[:300],
                            )
                            self._note_if_network_blocked(reason)
                    if os.path.exists(expected_path):
                        if label != "default":
                            log.info("recovered via %s player clients", label)
                        return expected_path

        log.debug("no playable audio landed for query set %r", queries)
        raise RuntimeError("no playable audio source found on YouTube for this track")

    def download_http_file(self, url, destination):
        with self.session.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            downloaded = 0
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        progress = int(downloaded / total * 100)
                        self.dlprogress_signal.emit(progress)
        return destination

    def _download_one_track(self, track, playlist_folder_path, default_cover_url, track_num=0):
        """Download a single track. Runs inside a ThreadPoolExecutor worker.

        Returns None on success, the track title on failure (for _failed_tracks).
        Qt signals emitted here cross thread boundaries via queued connections,
        which is safe.

        In parallel mode, progress reports completed tracks. The preview
        follows the latest track to start, with ordered metadata snapshots
        keeping late worker events from replacing a newer preview.

        track_num (1-based) is passed through to song_meta so the ID3 TRCK
        frame can be populated for playlist ordering.
        """
        if self.is_cancelled():
            return None

        if self._youtube_is_blocked():
            with self._failed_lock:
                self._failed_tracks.append(track.title)
            self._finish_track_ui(ok=False)
            return track.title

        track_title = track.title
        artists = track.artists
        sanitized_title = self.sanitize_text(track_title)
        sanitized_artists = self.sanitize_text(artists)

        # Per-track enrichment: the playlist embed carries no per-track cover
        # url (the "all 300 songs have the same cover" report) and no album
        # at all (#104), so fetch /embed/track/{id} when any of them is
        # missing. ~100-300ms per track, overlapped by other workers in
        # parallel mode.
        cover_url = track.cover_url
        album_name = track.album or ""
        release_date = track.release_date or ""
        if (
            (not cover_url or not album_name or not release_date)
            and track.id
            and self.spotifydown_api is not None
            and not self.is_cancelled()
        ):
            try:
                enriched = self.spotifydown_api.get_track(track.id)
                if enriched:
                    if enriched.cover_url:
                        cover_url = enriched.cover_url
                    if not album_name and enriched.album:
                        album_name = enriched.album
                    if not release_date and enriched.release_date:
                        release_date = enriched.release_date
            except InterruptedError:
                if self.is_cancelled():
                    return None
                raise
            except SpotifyDownAPIError as exc:
                log.debug("track enrichment failed for '%s': %s", track_title, exc)

        cover_url = cover_url or default_cover_url

        # Collision guard: distinct tracks can resolve to the same name
        # (always possible, likely under title_only), and parallel workers
        # racing os.path.exists would clobber each other. Claim under a lock;
        # if taken in-flight, on a case-folding filesystem, or on disk owned
        # by a different track per the manifest, suffix the track id. Keep
        # incrementing when the suffixed name is also in flight (duplicate
        # playlist entries can otherwise make a third worker clobber the
        # second worker's `[id]` file).
        with self._filename_lock:
            disambig = None
            collision_index = 0
            while True:
                filename = self._compose_filename(
                    sanitized_title, sanitized_artists, track_num, disambig=disambig
                )
                filepath = os.path.join(playlist_folder_path, cap_filename(filename))
                claim = filepath.casefold()
                if claim not in self._in_flight_files and not self._file_belongs_to_other(
                    filepath, track.id
                ):
                    break
                if disambig is None:
                    log.info(
                        "filename collision: %r already claimed, suffixing track id %s",
                        os.path.basename(filepath),
                        track.id,
                    )
                collision_index += 1
                base_disambig = str(track.id or "duplicate")
                disambig = (
                    base_disambig if collision_index == 1 else f"{base_disambig}-{collision_index}"
                )
            self._in_flight_files.add(claim)

        song_meta = {
            "title": track_title,
            "artists": artists,
            "album": album_name,
            "releaseDate": release_date,
            "cover": cover_url or "",
            "file": filepath,
            "trackNumber": track_num,
        }

        try:
            # Preview panel shows whichever track most recently started; the
            # worker race is fine (better than a blank panel).
            self._emit_song_meta(song_meta)

            if os.path.exists(filepath):
                self._write_metadata_if_enabled(song_meta)
                self._record_in_manifest(track.id, filepath)
                self.add_song_meta.emit(song_meta)
                self._finish_track_ui(ok=True, skipped=True)
                return None

            search_query = f"ytsearch1:{track_title} {artists} audio"
            expected_dur = (track.duration_ms / 1000) if track.duration_ms else None
            try:
                final_path = (
                    filepath
                    if self._reuse_audio(track.id, filepath)
                    else self.download_track_audio(
                        search_query,
                        filepath,
                        expected_duration_s=expected_dur,
                        expected_title=track_title,
                        expected_artists=artists,
                        expected_album=album_name,
                    )
                )
            except Exception as error_status:
                if self.is_cancelled():
                    return None
                error_msg = self._get_user_friendly_error(error_status, track_title)
                self.error_signal.emit(error_msg)
                # concise reason at WARNING (the per-attempt yt-dlp reason is
                # already logged above); full traceback only when verbose
                log.warning("track failed: '%s': %s", track_title, str(error_status)[:200])
                log.debug("track failure traceback for '%s'", track_title, exc_info=True)
                with self._failed_lock:
                    self._failed_tracks.append(track_title)
                self._finish_track_ui(ok=False)
                return track_title

            if not final_path or not os.path.exists(final_path):
                self.error_signal.emit(f"'{track_title}' - download failed")
                log.warning(
                    "track produced no audio file (no confident match or blocked): '%s'",
                    track_title,
                )
                with self._failed_lock:
                    self._failed_tracks.append(track_title)
                self._finish_track_ui(ok=False)
                return track_title

            song_meta["file"] = final_path
            self._write_metadata_if_enabled(song_meta)
            self._record_in_manifest(track.id, final_path)
            self._remember_audio(track.id, final_path)
            self.add_song_meta.emit(song_meta)
            self._finish_track_ui(ok=True)
            return None
        finally:
            with self._filename_lock:
                self._in_flight_files.discard(filepath.casefold())

    def _finish_track_ui(self, ok: bool, skipped: bool = False) -> None:
        """Update counter + progress bar after a track completes or fails."""
        with self._counter_lock:
            current = self.increment_counter(
                "skipped" if skipped else "downloaded" if ok else "failed"
            )
            # Aggregate progress across all workers: show how many tracks are
            # done as a percentage. Avoids the N-workers-jittering-one-bar
            # problem where per-byte emits from 4 downloads make the bar jump.
            if self._parallel_mode and self._total_tracks > 0:
                pct = int(current / self._total_tracks * 100)
                self.dlprogress_signal.emit(min(pct, 100))
            elif ok:
                self.dlprogress_signal.emit(100)

    def _load_manifest(self, folder: str) -> set:
        """Load the set of track IDs already downloaded into `folder`.

        The manifest is a JSON-lines file inside the folder; each line is a
        `{"id", "file"}` record. Entries whose file is missing or belongs to
        another output format are ignored, so deletions and format changes
        download the requested audio. Returns the set of valid IDs and arms
        `_manifest_path` for incremental appends during this run.
        """
        path = os.path.join(folder, MANIFEST_FILENAME)
        self._manifest_path = path
        self._manifest_owners = {}
        self._manifest_records = set()
        self._manifest_write_warned = False
        done: set[str] = set()
        target_extension = f".{SUPPORTED_FORMATS[self.audio_format]['ext']}".casefold()
        for record in _iter_manifest_records(path):
            track_id = record["id"]
            filename = record["file"]
            if os.path.splitext(filename)[1].casefold() == target_extension and os.path.isfile(
                os.path.join(folder, filename)
            ):
                done.add(track_id)
                self._manifest_owners[filename.casefold()] = track_id
                self._manifest_records.add((track_id, filename.casefold()))
        return done

    def _file_belongs_to_other(self, filepath: str, track_id) -> bool:
        """True when a file already at this path is a different track's per
        the manifest - a title collision across runs, not a resume hit (#91).
        An unmapped existing file stays a resume hit so crash recovery
        (files present, manifest lost) keeps skipping instead of re-downloading."""
        if not track_id or not os.path.exists(filepath):
            return False
        owner = self._manifest_owners.get(os.path.basename(filepath).casefold())
        return owner is not None and owner != track_id

    def _record_in_manifest(self, track_id, filepath: str) -> None:
        """Append a completed track to the manifest (thread-safe).

        Append-only JSON-lines so recording a track is O(1) regardless of how
        large the playlist is. Failures are swallowed: the manifest is an
        optimization for resuming, never a hard dependency of a download.
        """
        if not track_id or not self._manifest_path:
            return
        import json

        filename = os.path.basename(filepath)
        record_key = (str(track_id), filename.casefold())
        record = json.dumps({"id": str(track_id), "file": filename})
        # _file_belongs_to_other reads this map under _filename_lock while
        # parallel workers claim paths; publish ownership under the same lock.
        with self._filename_lock:
            self._manifest_owners.setdefault(filename.casefold(), str(track_id))
        with self._manifest_lock:
            if record_key in self._manifest_records:
                return
            try:
                with open(self._manifest_path, "a", encoding="utf-8") as handle:
                    handle.write(record + "\n")
                    handle.flush()
                self._manifest_records.add(record_key)
            except OSError as exc:
                if not self._manifest_write_warned:
                    log.warning("could not update resume manifest: %s", exc)
                    self._manifest_write_warned = True

    def _reset_download_state(self):
        # Reset mutable state so repeat invocations on the same scraper
        # instance don't carry stale counters or failure lists.
        with self._counter_lock:
            self.counter = 0
            self._current_resumed = 0
            self._total_known = False
            if not self._queue_active:
                self._queue_counts = dict.fromkeys(self._queue_counts, 0)
        with self._failed_lock:
            self._failed_tracks.clear()
            self._network_blocked = False
        with self._filename_lock:
            self._in_flight_files.clear()
        self._parallel_mode = False
        self._total_tracks = 0
        self._manifest_path = None
        self._manifest_owners.clear()
        self._manifest_records.clear()
        self._manifest_write_warned = False

    def scrape_playlist(self, spotify_playlist_link, music_folder):
        self._reset_download_state()

        # All collections share download, numbering, and resume handling.
        content_type, playlist_id = detect_spotify_url_type(spotify_playlist_link)
        if content_type not in ("playlist", "album", "artist"):
            raise ValueError("Expected a playlist, album, or artist URL")
        self.PlaylistID.emit(playlist_id)

        # Bail before network work if stop was already clicked.
        if self.is_cancelled():
            self.PlaylistCompleted.emit("Download cancelled")
            return

        try:
            spotify_api = self.ensure_spotifydown_api()
        except SpotifyDownAPIError as exc:
            raise RuntimeError(str(exc)) from exc

        metadata = spotify_api.get_playlist_metadata(playlist_id, content_type=content_type)
        playlist_display_name = (
            metadata.name if content_type == "artist" else self.format_playlist_name(metadata)
        )
        self.song_Album.emit(playlist_display_name)

        playlist_folder_path = self.prepare_playlist_folder(music_folder, playlist_display_name)

        # Resume support: skip tracks already downloaded in a previous run of
        # this folder before fetching their (rate-limited) metadata, so a huge
        # playlist can be finished across multiple sessions (closes #40).
        already_done = self._load_manifest(playlist_folder_path)
        if already_done:
            with self._counter_lock:
                self._queue_counts["skipped"] += len(already_done)
                self._current_resumed = len(already_done)
            self.resume_skipped.emit(len(already_done))
            self.error_signal.emit(
                f"Resuming: skipping {len(already_done)} already-downloaded track(s)"
            )

        # Spotify normally gives us an exact count. Use it to size the worker
        # pool before consuming the track generator, allowing the first audio
        # downloads to overlap metadata retrieval for the rest of a large
        # playlist. If an alternate/mock provider has no trustworthy count,
        # retain the safe materialized fallback.
        raw_total = metadata.track_count
        expected_total = (
            raw_total if isinstance(raw_total, int) and not isinstance(raw_total, bool) else 0
        )
        expected_remaining = max(expected_total - len(already_done), 0)
        track_iter = spotify_api.iter_playlist_tracks(
            playlist_id, content_type=content_type, skip_ids=already_done
        )
        if expected_total:
            tracks = track_iter
            planned_tracks = expected_remaining
        else:
            materialized = []
            for track in track_iter:
                if self.is_cancelled():
                    break
                materialized.append(track)
            tracks = iter(materialized)
            planned_tracks = len(materialized)

        if self.is_cancelled():
            self.PlaylistCompleted.emit("Download cancelled")
            return

        self._set_total_tracks(planned_tracks)
        self.Resetprogress_signal.emit(0)

        worker_count = max(1, min(self.MAX_WORKERS, planned_tracks))
        self._parallel_mode = worker_count > 1

        log.info(
            "%s scrape: name=%r id=%s tracks=%d (resume-skipped %d) mode=%s workers=%d fmt=%s/%s naming=%s",
            content_type,
            playlist_display_name,
            playlist_id,
            planned_tracks,
            len(already_done),
            "parallel" if self._parallel_mode else "sequential",
            worker_count,
            self.audio_format,
            self.audio_quality,
            "title-only"
            if self.title_only
            else ("artist-first" if self.artist_first else "default"),
        )

        # Canonical playlist position over enumerate order: spclient yields
        # in http-completion order on >100-track playlists (#51). Enumerate
        # only for albums/small playlists, which arrive already ordered.
        def _track_num_for(track, idx):
            return track.position if getattr(track, "position", None) else idx

        scheduled = 0
        if worker_count == 1:
            try:
                for idx, track in enumerate(tracks, start=1):
                    if self.is_cancelled():
                        break
                    scheduled = idx
                    self._total_tracks = max(self._total_tracks, scheduled)
                    # Reset the per-track progress bar for sequential downloads.
                    self.Resetprogress_signal.emit(0)
                    self._download_one_track(
                        track,
                        playlist_folder_path,
                        metadata.cover_url,
                        track_num=_track_num_for(track, idx),
                    )
            finally:
                close_tracks = getattr(tracks, "close", None)
                if close_tracks:
                    close_tracks()
        else:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                    pending = {}

                    def reap_completed():
                        done, _ = concurrent.futures.wait(
                            pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
                        )
                        for future in done:
                            track = pending.pop(future)
                            try:
                                future.result()
                            except Exception as exc:
                                if self.is_cancelled():
                                    continue
                                log.error("unexpected worker error", exc_info=exc)
                                self.error_signal.emit(f"Unexpected worker error: {exc}")
                                with self._failed_lock:
                                    self._failed_tracks.append(track.title)
                                self._finish_track_ui(ok=False)

                    try:
                        for idx, track in enumerate(tracks, start=1):
                            while len(pending) >= worker_count * 2 and not self.is_cancelled():
                                reap_completed()
                            if self.is_cancelled() or self._youtube_is_blocked():
                                break
                            scheduled = idx
                            future = executor.submit(
                                self._download_one_track,
                                track,
                                playlist_folder_path,
                                metadata.cover_url,
                                _track_num_for(track, idx),
                            )
                            pending[future] = track
                        self._set_total_tracks(scheduled)
                        while pending and not self.is_cancelled():
                            reap_completed()
                    finally:
                        for future in pending:
                            future.cancel()
                        close_tracks = getattr(tracks, "close", None)
                        if close_tracks:
                            close_tracks()
            finally:
                # Reset only after executor shutdown: in-flight workers that
                # observed False mid-run would emit single-track UI signals.
                self._parallel_mode = False

        self._set_total_tracks(scheduled)

        if self.is_cancelled():
            log.info("scrape cancelled by user (%d done before cancel)", self.counter)
            self.PlaylistCompleted.emit("Download cancelled")
            return

        # Report completion with failed track count
        ok = max(self._total_tracks - len(self._failed_tracks), 0)
        if self._failed_tracks:
            log.info("scrape done: %d ok, %d failed", ok, len(self._failed_tracks))
            log.info("failed tracks: %s", " | ".join(self._failed_tracks))
            self.PlaylistCompleted.emit(f"Done! {len(self._failed_tracks)} track(s) failed")
        else:
            log.info("scrape done: %d ok, 0 failed", self._total_tracks)
            self.PlaylistCompleted.emit("Download Complete!")

    def returnSPOT_ID(self, link):
        """Extract playlist ID from Spotify URL."""
        return extract_playlist_id(link)

    def scrape_track(self, spotify_track_link, music_folder):
        """Download a single track from Spotify."""
        self._reset_download_state()
        if self.is_cancelled():
            self.PlaylistCompleted.emit("Download cancelled")
            return
        url_type, track_id = detect_spotify_url_type(spotify_track_link)
        if url_type != "track":
            raise ValueError("Expected a track URL")
        self._set_total_tracks(1)

        try:
            spotify_api = self.ensure_spotifydown_api()
        except SpotifyDownAPIError as exc:
            raise RuntimeError(str(exc)) from exc

        track = spotify_api.get_track(track_id)
        log.info(
            "single-track scrape: %r by %r id=%s fmt=%s/%s",
            track.title,
            track.artists,
            track_id,
            self.audio_format,
            self.audio_quality,
        )
        self.song_Album.emit("Single Track Download")

        if not os.path.exists(music_folder):
            os.makedirs(music_folder)

        self.Resetprogress_signal.emit(0)

        track_title = track.title
        artists = track.artists
        sanitized_title = self.sanitize_text(track_title)
        sanitized_artists = self.sanitize_text(artists)
        filename = self._compose_filename(sanitized_title, sanitized_artists)
        filepath = os.path.join(music_folder, cap_filename(filename))

        album_name = track.album or ""
        release_date = track.release_date or ""
        cover_url = track.cover_url

        song_meta = {
            "title": track_title,
            "artists": artists,
            "album": album_name,
            "releaseDate": release_date,
            "cover": cover_url or "",
            "file": filepath,
            "trackNumber": 1,
        }

        self._emit_song_meta(song_meta)

        if os.path.exists(filepath):
            self._write_metadata_if_enabled(song_meta)
            self.add_song_meta.emit(song_meta)
            self.increment_counter("skipped")
            self.PlaylistCompleted.emit("Track already exists!")
            return

        # Download via YouTube search
        search_query = f"ytsearch1:{track_title} {artists} audio"
        expected_dur = (track.duration_ms / 1000) if track.duration_ms else None
        try:
            final_path = (
                filepath
                if self._reuse_audio(track.id, filepath)
                else self.download_track_audio(
                    search_query,
                    filepath,
                    expected_duration_s=expected_dur,
                    expected_title=track_title,
                    expected_artists=artists,
                    expected_album=album_name,
                )
            )
        except Exception as error_status:
            if self.is_cancelled():
                self.PlaylistCompleted.emit("Download cancelled")
                return
            error_msg = self._get_user_friendly_error(error_status, track_title)
            log.error("single-track download failed: '%s'", track_title, exc_info=True)
            # record it: the CLI derives its failure count and exit code from
            # this list, and a silent single-track failure exited 0 with no file
            with self._failed_lock:
                self._failed_tracks.append(track_title)
            self.increment_counter("failed")
            self.PlaylistCompleted.emit(error_msg)
            return

        if not final_path or not os.path.exists(final_path):
            log.warning("single-track produced no audio file: '%s'", track_title)
            with self._failed_lock:
                self._failed_tracks.append(track_title)
            self.increment_counter("failed")
            self.PlaylistCompleted.emit("Download failed - no audio file produced")
            return

        song_meta["file"] = final_path
        self._write_metadata_if_enabled(song_meta)
        self.add_song_meta.emit(song_meta)
        self._remember_audio(track.id, final_path)
        self.increment_counter()
        self.dlprogress_signal.emit(100)
        self.PlaylistCompleted.emit("Download Complete!")

    def increment_counter(self, outcome: str = "downloaded") -> int:
        with self._counter_lock:
            self.counter += 1
            current = self.counter
            self._queue_counts[outcome] += 1
            self.count_updated.emit(current)
            self._emit_progress_snapshot()
        return current


# Scraper Thread
class ScraperThread(QThread):
    progress_update = pyqtSignal(str)
    source_started = pyqtSignal(int, int)

    def __init__(
        self,
        spotify_link,
        music_folder=None,
        cancel_event: threading.Event | None = None,
        **scraper_opts,
    ):
        super().__init__()
        self.spotify_link = spotify_link
        self.music_folder = music_folder or os.path.join(os.getcwd(), "music")
        self._cancel_event = cancel_event or threading.Event()
        # MusicScraper's explicit signature validates the option names
        self.scraper = MusicScraper(cancel_event=self._cancel_event, **scraper_opts)

    def request_cancel(self):
        """Request cancellation of the download."""
        self._cancel_event.set()

    def run(self):
        self.progress_update.emit("Scraping started...")
        try:
            urls = parse_spotify_urls(self.spotify_link)
            self.scraper.begin_queue(len(urls))
            failed_urls = 0
            for index, url in enumerate(urls, 1):
                if self._cancel_event.is_set():
                    break
                self.scraper.begin_url(index)
                self.source_started.emit(index, len(urls))
                self.progress_update.emit(f"URL {index}/{len(urls)}: {url}")
                try:
                    url_type, _ = detect_spotify_url_type(url)
                    if url_type == "track":
                        self.scraper.scrape_track(url, self.music_folder)
                    else:
                        self.scraper.scrape_playlist(url, self.music_folder)
                    if self.scraper._failed_tracks:
                        failed_urls += 1
                except Exception as exc:
                    if self._cancel_event.is_set():
                        break
                    if len(urls) == 1:
                        raise
                    failed_urls += 1
                    log.exception("scrape failed for %s", url)
                    self.progress_update.emit(f"URL {index}/{len(urls)} failed: {exc}")
            if self._cancel_event.is_set():
                self.progress_update.emit("Download cancelled")
            elif failed_urls:
                self.progress_update.emit(
                    f"Finished with failures in {failed_urls}/{len(urls)} URLs"
                )
            else:
                self.progress_update.emit(f"Completed {len(urls)} URL(s).")
        except Exception as e:
            log.exception("scrape failed for %s", self.spotify_link)
            self.progress_update.emit(f"{e}")
        finally:
            self.scraper.close()


_COVER_CACHE_ITEMS = 64
_COVER_CACHE_BYTES = 32 * 1024 * 1024
_MAX_THUMBNAIL_THREADS = 4
_cover_cache: OrderedDict[str, bytes] = OrderedDict()
_cover_cache_size = 0
_cover_inflight: dict[str, threading.Event] = {}
_cover_cache_lock = threading.Lock()


def _clear_cover_cache() -> None:
    """Clear the process-local artwork cache (primarily useful for tests)."""
    global _cover_cache_size
    with _cover_cache_lock:
        _cover_cache.clear()
        _cover_cache_size = 0


def _fetch_cover_bytes(url: str) -> bytes | None:
    """Download cover bytes once per URL, with a bounded single-flight LRU."""
    global _cover_cache_size
    if not url:
        return None

    with _cover_cache_lock:
        cached = _cover_cache.pop(url, None)
        if cached is not None:
            _cover_cache[url] = cached
            return cached
        waiter = _cover_inflight.get(url)
        if waiter is None:
            waiter = threading.Event()
            _cover_inflight[url] = waiter
            leader = True
        else:
            leader = False

    if not leader:
        # The network timeout is 15s. A small margin prevents a stuck leader
        # from pinning every tag/preview worker indefinitely.
        waiter.wait(17)
        with _cover_cache_lock:
            return _cover_cache.get(url)

    result = None
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200 and resp.content:
            result = bytes(resp.content)
    except (requests.RequestException, OSError) as exc:
        log.debug("cover fetch failed: %s", exc)
    finally:
        with _cover_cache_lock:
            # Oversized artwork is still returned to this caller but not held
            # for the process lifetime. Spotify covers are normally far below
            # this overall 32 MiB cache ceiling.
            if result is not None and len(result) <= _COVER_CACHE_BYTES:
                previous = _cover_cache.pop(url, None)
                if previous is not None:
                    _cover_cache_size -= len(previous)
                _cover_cache[url] = result
                _cover_cache_size += len(result)
                while _cover_cache and (
                    len(_cover_cache) > _COVER_CACHE_ITEMS or _cover_cache_size > _COVER_CACHE_BYTES
                ):
                    _, evicted = _cover_cache.popitem(last=False)
                    _cover_cache_size -= len(evicted)
            _cover_inflight.pop(url, None)
            waiter.set()
    return result


def _detect_image_mime(data: bytes) -> str:
    """Return the MIME string for image bytes, sniffed from magic numbers.

    Spotify currently serves JPEG covers; this function exists so a future
    switch to PNG (or a mid-flight content-type change) doesn't silently
    produce broken cover-art frames mis-tagged as JPEG.

    ref: JPEG magic ff d8 ff (any JFIF/Exif variant), per ISO/IEC 10918-1
    ref: PNG signature 89 50 4e 47 0d 0a 1a 0a, per W3C PNG spec section 5.2
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"  # safe default; Spotify has served JPEG since 2015


def _write_metadata_mp3(filename: str, tags: dict, cover_bytes: bytes | None) -> None:
    """Write ID3 tags + embedded cover art to an MP3.

    Tags and the APIC cover frame are written as ID3v2.3 with UTF-16 text
    encoding instead of mutagen's v2.4 / UTF-8 default. v2.3 + UTF-16 is the
    lowest common denominator that's understood by older iTunes, Windows
    Media Player, most car head-units, and stock Android players, none of
    which read v2.4 APIC frames reliably (closes #46).

    ref: ID3v2.3 spec section 3.3 (only encoding values $00 ISO-8859-1
         and $01 Unicode UTF-16+BOM are defined) https://id3.org/id3v2.3.0
    ref: ID3v2.4 spec adds $02 UTF-16BE and $03 UTF-8 (which is what
         mutagen writes by default) https://id3.org/id3v2.4.0-frames
    ref: mutagen `update_to_v23()` downgrades any UTF-8 frames to UTF-16
         before saving as v2.3 https://mutagen.readthedocs.io/en/latest/api/id3.html
    """
    audio = EasyID3(filename)
    audio["title"] = tags.get("title", "")
    audio["artist"] = tags.get("artists", "")
    audio["album"] = tags.get("album", "")
    audio["date"] = tags.get("releaseDate", "")
    track_num = tags.get("trackNumber") or 0
    if track_num:
        audio["tracknumber"] = str(track_num)
    # EasyID3.save() defaults to v2.4 + UTF-8. Passing v2_version=3 tells
    # mutagen to downgrade text frames to a v2.3-allowed encoding (UTF-16
    # with BOM for non-ASCII, Latin-1 for ASCII) before writing.
    audio.save(v2_version=3)
    if cover_bytes:
        id3 = ID3(filename)
        mime = _detect_image_mime(cover_bytes)
        # encoding=1 (UTF-16+BOM) is the only Unicode encoding v2.3 defines.
        # type=3 is "Cover (front)" per the v2.3 APIC enum.
        id3.add(APIC(encoding=1, mime=mime, type=3, desc="Cover", data=cover_bytes))
        id3.update_to_v23()
        id3.save(v2_version=3)


def _write_metadata_m4a(filename: str, tags: dict, cover_bytes: bytes | None) -> None:
    """Write iTunes atom tags + embedded cover art to an M4A/MP4.

    iTunes atoms (`covr`, `\xa9nam`, etc.) are a stable, version-less spec
    used by every MP4-aware player. The only knob worth getting right is
    the cover-art image format, which we sniff so a future PNG cover from
    Spotify doesn't get mis-tagged as JPEG.

    ref: mutagen MP4Tags atom keys (`\xa9nam`/`\xa9ART`/`\xa9alb`/`\xa9day`/
         `trkn`/`covr`) and MP4Cover.FORMAT_JPEG/PNG, which is the spec we
         write against https://mutagen.readthedocs.io/en/latest/api/mp4.html
    """
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(filename)
    audio["\xa9nam"] = tags.get("title", "")
    audio["\xa9ART"] = tags.get("artists", "")
    audio["\xa9alb"] = tags.get("album", "")
    date = tags.get("releaseDate", "")
    if date:
        audio["\xa9day"] = date
    track_num = tags.get("trackNumber") or 0
    if track_num:
        audio["trkn"] = [(int(track_num), 0)]
    if cover_bytes:
        mime = _detect_image_mime(cover_bytes)
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover_bytes, imageformat=fmt)]
    audio.save()


def _write_metadata_flac(filename: str, tags: dict, cover_bytes: bytes | None) -> None:
    """Write Vorbis comments + embedded cover art to a FLAC.

    FLAC's Picture block carries an explicit MIME string, so we sniff the
    image type and pass it through. Vorbis comments are always UTF-8 and
    universally supported, so nothing else is version-sensitive here.

    ref: FLAC METADATA_BLOCK_PICTURE format spec
         https://xiph.org/flac/format.html#metadata_block_picture
    """
    from mutagen.flac import FLAC, Picture

    audio = FLAC(filename)
    audio["title"] = tags.get("title", "")
    audio["artist"] = tags.get("artists", "")
    audio["album"] = tags.get("album", "")
    date = tags.get("releaseDate", "")
    if date:
        audio["date"] = date
    track_num = tags.get("trackNumber") or 0
    if track_num:
        audio["tracknumber"] = str(track_num)
    if cover_bytes:
        # add_picture() appends; clear first so a re-tag doesn't stack duplicate covers
        audio.clear_pictures()
        pic = Picture()
        pic.type = 3  # Front cover
        pic.mime = _detect_image_mime(cover_bytes)
        pic.desc = "Cover"
        pic.data = cover_bytes
        audio.add_picture(pic)
    audio.save()


_METADATA_WRITERS = {
    ".mp3": _write_metadata_mp3,
    ".m4a": _write_metadata_m4a,
    ".flac": _write_metadata_flac,
}


def _write_song_metadata(tags: dict, filename: str) -> str:
    """Write tags synchronously and return a concise status message."""
    log.info("writing tags: %s", filename)
    ext = os.path.splitext(filename)[1].lower()
    writer = _METADATA_WRITERS.get(ext)
    if writer is None:
        return "Tags skipped (unsupported container)"
    cover_bytes = _fetch_cover_bytes(tags.get("cover", ""))
    writer(filename, tags, cover_bytes)
    return "Tags added successfully"


class WritingMetaTagsThread(QThread):
    tags_success = pyqtSignal(str)

    def __init__(self, tags, filename):
        super().__init__()
        self.tags = tags
        self.filename = filename

    def run(self):
        """Write tags + cover art synchronously, dispatching on file extension.

        Each container uses a different tag system (ID3 for mp3, iTunes atoms
        for m4a, Vorbis comments for flac). Opus/WAV are skipped with a log
        line; those formats have limited or no standard cover-art story that
        would repay the extra dependency surface for this project's scope.
        """
        try:
            self.tags_success.emit(_write_song_metadata(self.tags, self.filename))
        except Exception:
            log.error("tag write failed: %s", self.filename, exc_info=True)


class DownloadThumbnail(QThread):
    thumbnail_ready = pyqtSignal(bytes)  # Signal to safely update UI from main thread

    def __init__(self, url, main_UI):
        super().__init__()
        self.url = url
        self.main_UI = main_UI
        self.thumbnail_ready.connect(self._update_ui)

    def run(self):
        if not self.url:
            return
        data = _fetch_cover_bytes(self.url)
        if data:
            self.thumbnail_ready.emit(data)

    @pyqtSlot(bytes)
    def _update_ui(self, data):
        """Update UI from main thread via signal."""
        self.main_UI.apply_preview_cover(self.url, data)


class SettingsDialog(QDialog):
    """Download folder + audio format + quality in one dialog."""

    def __init__(self, parent, config: dict):
        super().__init__(parent)
        self.setWindowTitle("Sunnify Settings")
        self.setModal(True)
        # min width so long macOS paths fit; height is sized at the end once hints exist
        self.setMinimumWidth(560)
        self._config = dict(config)
        self._workers_cb = QComboBox()
        self._workers_cb.addItems(["1", "2", "4", "6", "8"])
        self._workers_cb.setCurrentText(config.get("download_workers", "4"))

        from PyQt6.QtWidgets import QLabel, QLineEdit

        # Read-only QLineEdit: long paths scroll instead of truncating;
        # tooltip carries the full value.
        self._folder_label = QLineEdit(self._config.get("download_path") or "(not set)")
        self._folder_label.setReadOnly(True)
        self._folder_label.setFrame(False)
        self._folder_label.setCursorPosition(0)
        self._folder_label.setToolTip(self._folder_label.text())
        self._folder_label.setStyleSheet("QLineEdit { background: transparent; padding: 0; }")
        browse = QPushButton("Choose folder")
        browse.clicked.connect(self._choose_folder)

        folder_row = QHBoxLayout()
        folder_row.addWidget(self._folder_label, 1)
        folder_row.addWidget(browse)

        self._format_cb = QComboBox()
        for key in SUPPORTED_FORMATS:
            self._format_cb.addItem(key)
        self._format_cb.setCurrentText(self._config.get("format", "mp3"))
        self._format_cb.currentTextChanged.connect(self._on_format_change)

        self._quality_cb = QComboBox()
        for q in SUPPORTED_QUALITIES:
            self._quality_cb.addItem(f"{q} kbps")
        current_q = self._config.get("quality", "192")
        self._quality_cb.setCurrentText(f"{current_q} kbps")

        self._include_track_number_cb = QCheckBox()
        self._include_track_number_cb.setChecked(self._config.get("include_track_number", False))

        # the three naming shapes are mutually exclusive, so they present as
        # one pick-one control; underneath they map to the artist_first and
        # title_only config booleans the engine and CLI flags already use
        self._filename_style_cb = QComboBox()
        self._filename_style_cb.addItems(["Song - Artist", "Artist - Song", "Song only"])
        if self._config.get("title_only", False):
            self._filename_style_cb.setCurrentText("Song only")
        elif self._config.get("artist_first", False):
            self._filename_style_cb.setCurrentText("Artist - Song")

        self._filename_preview = QLabel()
        self._filename_preview.setStyleSheet("font-weight: 600;")
        self._include_track_number_cb.toggled.connect(self._update_filename_preview)
        self._filename_style_cb.currentTextChanged.connect(self._update_filename_preview)

        # display text <-> stored value; stored value is what ffmpeg -ar gets
        self._sample_rate_labels = {
            "auto": "auto (keep source)",
            "44100": "44.1 kHz",
            "48000": "48 kHz",
        }
        self._sample_rate_cb = QComboBox()
        for value in SUPPORTED_SAMPLE_RATES:
            self._sample_rate_cb.addItem(self._sample_rate_labels[value])
        current_sr = self._config.get("sample_rate", "auto")
        if current_sr not in SUPPORTED_SAMPLE_RATES:
            current_sr = "auto"
        self._sample_rate_cb.setCurrentText(self._sample_rate_labels[current_sr])

        self._loose_match_cb = QCheckBox()
        self._loose_match_cb.setChecked(self._config.get("loose_match", False))

        # initial enable/disable sync needs every dependent combo to exist
        self._on_format_change(self._format_cb.currentText())
        self._update_filename_preview()

        # Each setting owns its height as a QFrame+QVBoxLayout block:
        # QFormLayout computes row height from the label column and clips
        # word-wrapped hints, this lets every hint take the lines it needs.
        from PyQt6.QtGui import QFontMetrics, QPalette
        from PyQt6.QtWidgets import QFrame, QSizePolicy

        # one list so the label column is measured from the longest label, not a magic width
        _settings = [
            (
                "Download folder:",
                folder_row,
                "Each playlist, album, or artist discography gets its own folder here.",
            ),
            (
                "Audio format:",
                self._format_cb,
                "mp3 plays everywhere. m4a is smaller at the same quality. "
                "flac and wav are lossless (much larger files).",
            ),
            (
                "Audio quality:",
                self._quality_cb,
                "Applies to lossy formats only (mp3, m4a, opus). "
                "320 kbps is the highest quality these formats support.",
            ),
            (
                "Parallel downloads:",
                self._workers_cb,
                "4 is the default. Try 6 or 8 on a fast connection; reduce this if downloads are rate-limited.",
            ),
            (
                "Track number in filename:",
                self._include_track_number_cb,
                'Off → "Song - Artist.mp3".   On → "01. Song - Artist.mp3".   '
                "Files sort in playlist order in your file manager.",
            ),
            (
                "Filename style:",
                self._filename_style_cb,
                '"Song only" keeps the artist out of the filename; every tag '
                "(artist, album, art) is still written, and two different songs "
                "with the same title get a short id suffix instead of "
                "overwriting each other. Applies to new downloads; files "
                "already on disk keep their names.",
            ),
            (
                "Filename preview:",
                self._filename_preview,
                "",
            ),
            (
                "Sample rate:",
                self._sample_rate_cb,
                "auto keeps the source rate (YouTube audio is 48 kHz). "
                "44.1 kHz matches CDs and older players. "
                "Applies to mp3, flac and wav; opus and m4a keep their source rate.",
            ),
            (
                "Use closest result if no match:",
                self._loose_match_cb,
                "Off (default): skips a track rather than risk the wrong audio. "
                "On: falls back to the closest result by length, which recovers "
                "songs whose YouTube title is in another script (Greek, Cyrillic, "
                "Korean) but may let an occasional cover or remix slip through.",
            ),
        ]
        _fm = QFontMetrics(self.font())
        LABEL_W = max(_fm.horizontalAdvance(lbl) for lbl, _, _ in _settings) + 8

        # muted palette text so hints stay readable on light (win/linux) and dark (mac) themes
        _fg = self.palette().color(QPalette.ColorRole.WindowText)
        _hint_color = f"rgba({_fg.red()}, {_fg.green()}, {_fg.blue()}, 175)"

        def _setting_block(label_text: str, control, hint_text: str) -> QFrame:
            container = QFrame()
            box = QVBoxLayout(container)
            box.setSpacing(4)
            box.setContentsMargins(0, 0, 0, 12)  # visual gap between settings

            # Top row: label + control side-by-side, label fixed-width so all
            # the controls in different blocks line up vertically.
            row = QHBoxLayout()
            row.setSpacing(10)
            row.setContentsMargins(0, 0, 0, 0)
            name = QLabel(label_text)
            name.setMinimumWidth(LABEL_W)
            name.setMaximumWidth(LABEL_W)
            name.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(name)
            if isinstance(control, QHBoxLayout):
                row.addLayout(control, 1)
            else:
                row.addWidget(control, 1)
            box.addLayout(row)

            # Hint below, indented to start under the control column so it
            # visually associates with the control rather than the label.
            if hint_text:
                hint = QLabel(hint_text)
                hint.setWordWrap(True)
                hint.setStyleSheet(f"color: {_hint_color}; font-size: 11px;")
                hint.setContentsMargins(LABEL_W + 12, 2, 4, 0)
                hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                box.addWidget(hint)
            return container

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        # Gives users a one-click way to grab the log file to attach to a bug
        # report, without hunting through ~/Library/Logs or %LOCALAPPDATA%.
        open_logs = btns.addButton("Open logs folder", QDialogButtonBox.ButtonRole.ActionRole)
        open_logs.setToolTip(log_file_path())
        open_logs.clicked.connect(self._open_logs)

        from PyQt6.QtWidgets import QScrollArea, QWidget

        rows_host = QWidget()
        rows = QVBoxLayout(rows_host)
        rows.setSpacing(0)
        rows.setContentsMargins(0, 0, 0, 0)
        for _label, _control, _hint in _settings:
            rows.addWidget(_setting_block(_label, _control, _hint))
        rows.addStretch(1)

        # settings scroll when the screen can't fit them (small laptops,
        # windows dpi scaling); the ok/cancel row never leaves the screen
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidget(rows_host)

        layout = QVBoxLayout(self)
        layout.addWidget(self._scroll, 1)
        layout.addWidget(btns)

        # activate() first: hint labels word-wrap, so their heights are
        # height-for-width and only correct after layout activation. Then
        # open at the content's natural size, clamped to the screen so small
        # laptops and dpi scaling scroll instead of clipping.
        rows.activate()
        layout.activate()
        content_w = max(rows_host.sizeHint().width() + 40, 620)
        content_h = (
            rows.totalHeightForWidth(rows_host.sizeHint().width()) + btns.sizeHint().height() + 48
        )
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            content_w = min(content_w, int(avail.width() * 0.9))
            content_h = min(content_h, int(avail.height() * 0.9))
        self.resize(content_w, content_h)

    def _open_logs(self):
        """Reveal the log folder in the OS file manager."""
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        log_dir = _log_dir()
        with contextlib.suppress(OSError):
            os.makedirs(log_dir, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(log_dir))

    def _choose_folder(self):
        start = (
            self._folder_label.text()
            if os.path.isdir(self._folder_label.text())
            else os.path.expanduser("~")
        )
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Download Folder",
            start,
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks,
        )
        if folder:
            # Only append "Sunnify" when the user picked a non-Sunnify folder,
            # otherwise re-selecting the existing destination creates nested
            # Sunnify/Sunnify/... paths.
            chosen = (
                folder
                if os.path.basename(folder.rstrip(os.sep)) == "Sunnify"
                else os.path.join(folder, "Sunnify")
            )
            self._folder_label.setText(chosen)
            self._folder_label.setCursorPosition(0)
            self._folder_label.setToolTip(chosen)

    def _on_format_change(self, fmt: str) -> None:
        """Lossless formats (flac/wav) ignore the bitrate selector. The
        sample-rate selector only applies to formats that always transcode
        (mp3/flac/wav): opus is 48 kHz-only and m4a may stream-copy."""
        is_lossy = SUPPORTED_FORMATS.get(fmt, {}).get("lossy", True)
        self._quality_cb.setEnabled(is_lossy)
        self._sample_rate_cb.setEnabled(fmt in ("mp3", "flac", "wav"))

    def _update_filename_preview(self) -> None:
        style = self._filename_style_cb.currentText()
        stem = "Song" if style == "Song only" else style
        prefix = "01. " if self._include_track_number_cb.isChecked() else ""
        self._filename_preview.setText(f"{prefix}{stem}.mp3")

    def result_config(self) -> dict:
        self._config["download_workers"] = self._workers_cb.currentText()
        self._config["download_path"] = self._folder_label.text()
        self._config["format"] = self._format_cb.currentText()
        self._config["quality"] = self._quality_cb.currentText().split()[0]
        self._config["include_track_number"] = self._include_track_number_cb.isChecked()
        style = self._filename_style_cb.currentText()
        self._config["title_only"] = style == "Song only"
        if style == "Song only":
            # keep the stored order preference so switching back remembers it
            self._config.setdefault("artist_first", False)
        else:
            self._config["artist_first"] = style == "Artist - Song"
        label_to_value = {v: k for k, v in self._sample_rate_labels.items()}
        self._config["sample_rate"] = label_to_value.get(self._sample_rate_cb.currentText(), "auto")
        self._config["loose_match"] = self._loose_match_cb.isChecked()
        return self._config


class UpdateNotifier(QDialog):
    """Toast-style 'new version available' card. Static copy, dynamic versions,
    so there's no per-release content to maintain. Download opens the releases
    page (where the auto-generated changelog already lives)."""

    def __init__(self, parent, current: str, latest: str, url: str):
        super().__init__(parent)
        from PyQt6.QtGui import QColor, QFont, QFontMetrics
        from PyQt6.QtWidgets import QFrame, QLabel, QWidget

        self._url = url
        self.setWindowTitle("Update available")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setFont(QFont("Arial", 10))

        green, green_hover = "#1ED760", "#1FE968"
        cyan, purple = "rgba(80, 214, 255, 255)", "rgba(112, 32, 213, 255)"
        ink, mute = "#15151F", "#7A7A8C"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 26, 28, 30)  # room for the drop shadow
        # fractional dpi under-measures the wrapped body and squeezes the card (#64)
        outer.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(430)
        card.setStyleSheet("QFrame#card{background:#FFFFFF;border-radius:18px;}")
        shadow = QGraphicsDropShadowEffect(blurRadius=48, xOffset=0, yOffset=16)
        shadow.setColor(QColor(20, 10, 40, 110))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        v = QVBoxLayout(card)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QFrame()
        header.setObjectName("hdr")
        header.setStyleSheet(
            "QFrame#hdr{border-top-left-radius:18px;border-top-right-radius:18px;"
            f"background:qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:1,"
            f"stop:0.23 {cyan}, stop:0.81 {purple});}}"
        )
        hv = QVBoxLayout(header)
        hv.setContentsMargins(26, 20, 26, 22)
        hv.setSpacing(0)  # gaps are set explicitly between rows below

        eyebrow = QLabel("UPDATE AVAILABLE")
        ef = QFont("Arial", 9, QFont.Weight.Bold)
        ef.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        eyebrow.setFont(ef)
        eyebrow.setStyleSheet("color: rgba(255,255,255,0.85);")
        hv.addWidget(eyebrow)
        hv.addSpacing(8)

        name = QLabel("Sunnify")
        nfont = QFont("Arial", 22, QFont.Weight.Bold)
        name.setFont(nfont)
        name.setStyleSheet("color: #FFFFFF;")
        # large bold glyphs exceed QLabel's tight default box; reserve full height
        name.setMinimumHeight(QFontMetrics(nfont).height() + 10)
        hv.addWidget(name)
        hv.addSpacing(10)  # clear gap so the 'y' descender never crowds the version line

        # current -> new, read left to right; new version brighter/bold to draw the eye
        prog = QLabel(
            f'<span style="color:rgba(255,255,255,0.8)">{current}</span>'
            "&nbsp;&nbsp;&#8594;&nbsp;&nbsp;"
            f'<span style="color:#FFFFFF;font-weight:bold">{latest}</span>'
        )
        pfont = QFont("Arial", 13)
        prog.setFont(pfont)
        prog.setMinimumHeight(QFontMetrics(pfont).height() + 6)
        hv.addWidget(prog)
        v.addWidget(header)

        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(26, 22, 26, 6)
        msg = QLabel(
            "A newer version of Sunnify is available. Download it from the "
            "releases page to get the latest fixes and improvements."
        )
        msg.setWordWrap(True)
        msg.setFont(QFont("Arial", 10))
        msg.setStyleSheet(f"color: {mute};")
        bv.addWidget(msg)
        v.addWidget(body)

        footer = QWidget()
        fv = QHBoxLayout(footer)
        fv.setContentsMargins(26, 8, 26, 22)
        fv.setSpacing(10)

        later = QPushButton("Remind me later")
        later.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        later.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        later.setFixedHeight(40)
        later.setStyleSheet(
            f"QPushButton{{background:transparent;color:{mute};border:none;}}"
            f"QPushButton:hover{{color:{ink};}}"
        )
        later.clicked.connect(self.reject)
        fv.addWidget(later)
        fv.addStretch(1)

        download = QPushButton("Download")
        download.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        download.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        download.setFixedSize(132, 40)
        download.setStyleSheet(
            f"QPushButton{{background:{green};color:white;border-radius:10px;}}"
            f"QPushButton:hover{{background:{green_hover};}}"
        )
        download.clicked.connect(self._open_releases)
        fv.addWidget(download)
        v.addWidget(footer)

    def _open_releases(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        if not QDesktopServices.openUrl(QUrl(self._url)):
            log.warning("could not open releases page in browser: %s", self._url)
        self.accept()


class StarPromptNotifier(QDialog):
    """One-time 'star the repo' card, shown the moment the first song of the
    user's first download lands on disk (owner call: value is proven by a
    real file and the user is mid-wait; huge playlists never reach
    'complete' in one sitting, so completion was the wrong hook). Same card
    pattern as UpdateNotifier so it inherits the high-dpi behaviour verified
    for 2.0.13. Shown exactly once per install: the config flag is persisted
    before the dialog opens, so even a crash mid-dialog can never make it
    nag twice."""

    def __init__(self, parent):
        super().__init__(parent)
        from PyQt6.QtGui import QColor, QFont, QFontMetrics
        from PyQt6.QtWidgets import QFrame, QLabel, QWidget

        self._url = f"https://github.com/{GITHUB_REPO}"
        self.setWindowTitle("Enjoying Sunnify?")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setFont(QFont("Arial", 10))

        green, green_hover = "#1ED760", "#1FE968"
        cyan, purple = "rgba(80, 214, 255, 255)", "rgba(112, 32, 213, 255)"
        ink, mute = "#15151F", "#7A7A8C"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 26, 28, 30)  # room for the drop shadow
        # fractional dpi under-measures the wrapped body and squeezes the card (#64)
        outer.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(430)
        card.setStyleSheet("QFrame#card{background:#FFFFFF;border-radius:18px;}")
        shadow = QGraphicsDropShadowEffect(blurRadius=48, xOffset=0, yOffset=16)
        shadow.setColor(QColor(20, 10, 40, 110))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        v = QVBoxLayout(card)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QFrame()
        header.setObjectName("hdr")
        header.setStyleSheet(
            "QFrame#hdr{border-top-left-radius:18px;border-top-right-radius:18px;"
            f"background:qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:1,"
            f"stop:0.23 {cyan}, stop:0.81 {purple});}}"
        )
        hv = QVBoxLayout(header)
        hv.setContentsMargins(26, 20, 26, 22)
        hv.setSpacing(0)  # gaps are set explicitly between rows below

        eyebrow = QLabel("FIRST SONG DOWNLOADED")
        ef = QFont("Arial", 9, QFont.Weight.Bold)
        ef.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        eyebrow.setFont(ef)
        eyebrow.setStyleSheet("color: rgba(255,255,255,0.85);")
        hv.addWidget(eyebrow)
        hv.addSpacing(8)

        name = QLabel("Enjoying Sunnify?")
        nfont = QFont("Arial", 22, QFont.Weight.Bold)
        name.setFont(nfont)
        name.setStyleSheet("color: #FFFFFF;")
        # large bold glyphs exceed QLabel's tight default box; reserve full height
        name.setMinimumHeight(QFontMetrics(nfont).height() + 10)
        hv.addWidget(name)
        hv.addSpacing(6)  # descender room; 'j'/'y'/'g' tails clip at 1.5x without it
        v.addWidget(header)

        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(26, 22, 26, 6)
        msg = QLabel(
            "A star on GitHub keeps this project alive - it's how new people "
            "find Sunnify, and it takes five seconds. This asks once and never "
            "again."
        )
        msg.setWordWrap(True)
        msg.setFont(QFont("Arial", 10))
        msg.setStyleSheet(f"color: {mute};")
        bv.addWidget(msg)
        v.addWidget(body)

        footer = QWidget()
        fv = QHBoxLayout(footer)
        fv.setContentsMargins(26, 8, 26, 22)
        fv.setSpacing(10)

        later = QPushButton("Maybe later")
        later.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        later.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        later.setFixedHeight(40)
        # pad right so the invisible hit area matches the other card's dismiss
        later.setStyleSheet(
            f"QPushButton{{background:transparent;color:{mute};border:none;"
            "text-align:left;padding-right:32px;}"
            f"QPushButton:hover{{color:{ink};}}"
        )
        later.clicked.connect(self.reject)
        fv.addWidget(later)
        fv.addStretch(1)

        star = QPushButton("Star on GitHub")
        star.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        sfont = QFont("Arial", 10, QFont.Weight.Bold)
        star.setFont(sfont)
        star.setFixedHeight(40)
        # metrics-derived width; a fixed box clips when linux substitutes arial
        star.setMinimumWidth(QFontMetrics(sfont).horizontalAdvance("Star on GitHub") + 44)
        star.setStyleSheet(
            f"QPushButton{{background:{green};color:white;border-radius:10px;"
            "padding-left:18px;padding-right:18px;}"
            f"QPushButton:hover{{background:{green_hover};}}"
        )
        star.clicked.connect(self._open_repo)
        fv.addWidget(star)
        v.addWidget(footer)

    def _open_repo(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        if not QDesktopServices.openUrl(QUrl(self._url)):
            log.warning("could not open repo page in browser: %s", self._url)
        self.accept()


# Main Window
class MainWindow(QMainWindow, Ui_MainWindow):
    def __init__(self):
        """MainWindow constructor"""
        super().__init__()
        self.setupUi(self)
        self.PlaylistLink.setGeometry(20, 60, 200, 34)
        self.PlaylistLink.setPlaceholderText("Paste Spotify URL(s)")
        self.PlaylistLink.setToolTip("Separate URLs with spaces or commas, or use Multiple URLs.")
        self.MultipleUrlsBtn = QPushButton("Multiple URLs…", self.frame)
        self.MultipleUrlsBtn.setGeometry(20, 95, 130, 18)
        self.MultipleUrlsBtn.setStyleSheet(
            "QPushButton { border: none; color: #145c46; text-align: left; }"
            "QPushButton:hover { text-decoration: underline; }"
        )
        self.MultipleUrlsBtn.clicked.connect(self.edit_multiple_urls)
        self.OpenLogsBtn = QPushButton("Open logs", self.frame)
        self.OpenLogsBtn.setGeometry(20, 370, 115, 28)
        self.OpenLogsBtn.setToolTip(
            "Open the current log in Notepad" if sys.platform == "win32" else "Open the current log"
        )
        self.OpenLogsBtn.clicked.connect(self.open_log_file)
        self.label_10.hide()
        self.horizontalLayoutWidget_4.setGeometry(20, 250, 280, 42)
        self.CounterLabel.setWordWrap(True)
        self.horizontalLayoutWidget_3.setGeometry(20, 300, 280, 60)
        self.statusMsg.setWordWrap(True)
        # let the options row size to its content so "Add Meta Tags" isn't clipped
        # by the .ui's fixed-width container (varies with font/locale/dpi)
        self.horizontalLayoutWidget_5.adjustSize()

        # Load persisted user config so format/quality/folder survive restarts
        self._config = load_config()
        self.download_path = self._config.get("download_path") or self._get_default_download_path()
        self._download_path_set = bool(self._config.get("download_path"))
        self._active_threads = []  # Keep references to running threads to prevent GC crashes
        self._is_downloading = False  # Track download state for stop button
        self._cancel_event = threading.Event()  # Event for cooperative thread cancellation
        self._preview_meta = {}
        self._preview_id = 0
        self._preview_source = 0
        self._preview_cover_url = ""
        self._displayed_cover_url = ""
        self._last_progress_revision = 0
        self._last_progress_snapshot = None

        self.SONGINFORMATION.setGraphicsEffect(
            QGraphicsDropShadowEffect(blurRadius=25, xOffset=2, yOffset=2)
        )
        self.PlaylistLink.returnPressed.connect(self.on_returnButton)
        self.DownloadBtn.clicked.connect(self.on_returnButton)

        self.showPreviewCheck.stateChanged.connect(self.show_preview)

        self.Closed.clicked.connect(self.exitprogram)
        self.Select_Home.clicked.connect(self.Linkedin)
        self.SettingsBtn.clicked.connect(self.open_settings)

        self.label_8.show()
        self.AlbumText.show()

        # check for a newer release in the background; fail-silent, shows a toast only if found
        self._update_thread = UpdateCheckThread(__version__)
        self._update_thread.update_available.connect(self._show_update_notifier)
        self._active_threads.append(self._update_thread)
        self._update_thread.start()

    @pyqtSlot(str, str)
    def _show_update_notifier(self, latest: str, url: str):
        if QApplication.activeModalWidget() is not None:
            # never stack on another toast; the check simply runs again next launch
            log.info("update notifier skipped: another modal dialog is active")
            return
        log.info("update available: %s -> %s; showing notifier", __version__, latest)
        UpdateNotifier(self, __version__, latest, url).exec()

    @pyqtSlot(int)
    def _maybe_show_star_prompt(self, count: int):
        """One-time star ask the moment the first song of the user's first
        run lands on disk. Skipped after a Stop (a beg right then reads as
        nagging). Deferred cases (update toast on screen) retry naturally
        when the next song lands - the flag only persists once the dialog
        actually shows, so the one shot is never burned silently."""
        if self._config.get("star_prompt_shown"):
            return
        # Queue snapshots already count saved files across all URLs. The
        # legacy counter counts finished tracks, including failures.
        snapshot = self._last_progress_snapshot
        saved = (
            snapshot["downloaded"] if snapshot is not None else count - self._failed_track_count()
        )
        if saved < 1:
            return
        if self._cancel_event.is_set():
            return
        if QApplication.activeModalWidget() is not None:
            # never stack on the update notifier; the next landed song retries
            log.info("star prompt deferred: another modal dialog is active")
            return
        # persist before showing so a crash mid-dialog can never re-prompt
        self._config["star_prompt_shown"] = True
        save_config(self._config)
        log.info("first song landed - showing one-time star prompt")
        StarPromptNotifier(self).exec()

    def _get_default_download_path(self):
        """Get a sensible default download path that's writable."""
        # Try user's Music folder first
        home = os.path.expanduser("~")
        music_folder = os.path.join(home, "Music", "Sunnify")

        # On Windows, Music might be in a different location
        if sys.platform == "win32":
            try:
                import winreg

                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
                )
                music_folder = os.path.join(winreg.QueryValueEx(key, "My Music")[0], "Sunnify")
                winreg.CloseKey(key)
            except Exception:
                music_folder = os.path.join(home, "Music", "Sunnify")

        return music_folder

    def _ensure_download_path(self):
        """Ensure download path exists and is writable. Returns True if valid."""
        try:
            os.makedirs(self.download_path, exist_ok=True)
            # Test write access
            test_file = os.path.join(self.download_path, ".sunnify_test")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            return True
        except OSError as exc:
            log.error("download path not writable: %s (%s)", self.download_path, exc)
            return False

    def _prompt_download_location(self):
        """Prompt user to select download location. Returns True if selected."""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Download Folder",
            os.path.expanduser("~"),
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks,
        )
        if folder:
            # Keep downloads contained in a "Sunnify" subfolder, but avoid
            # creating nested Sunnify/Sunnify/... paths when the user picked
            # a folder that's already named Sunnify.
            if os.path.basename(folder.rstrip(os.sep)) == "Sunnify":
                self.download_path = folder
            else:
                self.download_path = os.path.join(folder, "Sunnify")
            self._download_path_set = True
            self._config["download_path"] = self.download_path
            save_config(self._config)
            return True
        return False

    def open_settings(self):
        """Full settings dialog: folder + audio format + bitrate."""
        cfg_for_dialog = dict(self._config)
        cfg_for_dialog["download_path"] = self.download_path
        dialog = SettingsDialog(self, cfg_for_dialog)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new = dialog.result_config()
            if new.get("download_path"):
                self.download_path = new["download_path"]
                self._download_path_set = True
            self._config.update({s.key: s.coerce(new.get(s.key, s.default)) for s in SETTINGS})
            self._config["download_path"] = self.download_path
            save_config(self._config)
            self.statusMsg.setText("Settings saved")

    @pyqtSlot()
    def open_log_file(self):
        import subprocess

        path = log_file_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8"):
                pass
            for handler in log.handlers:
                handler.flush()
            if sys.platform == "win32":
                subprocess.Popen(["notepad.exe", path])
            else:
                from PyQt6.QtCore import QUrl
                from PyQt6.QtGui import QDesktopServices

                if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                    raise OSError("No application could open the log file")
        except OSError as exc:
            self.statusMsg.setText(f"Could not open logs: {exc}")
            log.warning("could not open log file: %s", exc)

    @pyqtSlot()
    def edit_multiple_urls(self):
        current = "\n".join(filter(None, re.split(r"[\s,]+", self.PlaylistLink.text())))
        text, accepted = QInputDialog.getMultiLineText(
            self,
            "Multiple Spotify URLs",
            "One URL per line (artists, albums, playlists, or tracks):",
            current,
        )
        if accepted:
            self.PlaylistLink.setText(" ".join(text.split()))

    @pyqtSlot()
    def on_returnButton(self):
        # If already downloading, stop the download
        if self._is_downloading:
            self._stop_download()
            return

        try:
            spotify_urls = parse_spotify_urls(self.PlaylistLink.text())
        except ValueError as exc:
            self.statusMsg.setText(str(exc))
            return

        if not get_ffmpeg_path():
            if sys.platform == "win32":
                instructions = (
                    "Install FFmpeg on Windows using:\n"
                    "  winget install Gyan.FFmpeg\n"
                    "or\n"
                    "  choco install ffmpeg\n\n"
                    "Then restart Sunnify."
                )
            elif sys.platform == "darwin":
                instructions = "Install FFmpeg on macOS using:\n  brew install ffmpeg"
            else:
                instructions = "Install FFmpeg on Linux using:\n  sudo apt install ffmpeg"
            self.statusMsg.setText("FFmpeg not found")
            QMessageBox.critical(
                self,
                "FFmpeg Required",
                f"FFmpeg is required for audio downloads and conversion but was not found on your system.\n\n{instructions}",
            )
            return

        # ALWAYS prompt for download location on first download
        if not self._download_path_set:
            self.statusMsg.setText("Select download location...")
            if not self._prompt_download_location():
                self.statusMsg.setText("Download cancelled - no folder selected")
                return

        # Verify the selected path is still writable
        if not self._ensure_download_path():
            self.statusMsg.setText("Cannot write to download folder")
            QMessageBox.warning(
                self,
                "Invalid Download Location",
                f"Cannot write to:\n{self.download_path}\n\nPlease select a different folder.",
            )
            if not self._prompt_download_location():
                return

        try:
            self.statusMsg.setText(f"Queued {len(spotify_urls)} URL(s)")

            # Reset cancel event and set downloading state
            self._cancel_event = threading.Event()
            self._is_downloading = True
            self.DownloadBtn.setText("Stop")
            self._preview_id = 0
            self._preview_source = 0
            self._last_progress_revision = 0
            self._last_progress_snapshot = None

            self.scraper_thread = ScraperThread(
                spotify_urls,
                self.download_path,
                cancel_event=self._cancel_event,
                write_metadata=self.AddMetaDataCheck.isChecked(),
                **scraper_kwargs_from(self._config),
            )
            self.scraper_thread.progress_update.connect(self.update_progress)
            self.scraper_thread.source_started.connect(self.preview_source_started)
            self.scraper_thread.finished.connect(self.thread_finished)
            self.scraper_thread.scraper.song_Album.connect(self.update_AlbumName)
            self.scraper_thread.scraper.song_meta.connect(self.update_song_META)
            self.scraper_thread.scraper.add_song_meta.connect(self.add_song_META)
            self.scraper_thread.scraper.progress_snapshot.connect(self.update_queue_progress)
            self.scraper_thread.scraper.PlaylistCompleted.connect(
                lambda x: self.statusMsg.setText(x)
            )
            self.scraper_thread.scraper.error_signal.connect(lambda x: self.statusMsg.setText(x))

            self.scraper_thread.start()

        except ValueError as e:
            self.statusMsg.setText(str(e))
            self._is_downloading = False
            self.DownloadBtn.setText("Download")

    def _stop_download(self):
        """Stop the current download gracefully using cooperative cancellation."""
        self.statusMsg.setText("Stopping download...")
        self.DownloadBtn.setEnabled(False)

        # Signal cancellation via event (thread checks this periodically)
        self._cancel_event.set()

        if hasattr(self, "scraper_thread") and self.scraper_thread.isRunning():
            self.scraper_thread.request_cancel()
            # Thread will finish current track and exit; UI resets via thread_finished signal

    def thread_finished(self):
        """Reset UI state when download thread finishes."""
        self._is_downloading = False
        self.DownloadBtn.setText("Download")
        self.DownloadBtn.setEnabled(True)
        if hasattr(self, "scraper_thread"):
            self.scraper_thread.deleteLater()  # Clean up the thread properly

    def update_progress(self, message):
        self.statusMsg.setText(message)

    @pyqtSlot(dict)
    def update_song_META(self, song_meta):
        """Update UI with current track info (called BEFORE download starts)."""
        preview_id = song_meta.get("_preview_id", self._preview_id)
        source = song_meta.get("_url_index", self._preview_source)
        if preview_id < self._preview_id or source < self._preview_source:
            return
        self._preview_id = preview_id
        self._preview_source = source
        self._preview_meta = dict(song_meta)
        cover_url = song_meta.get("cover", "")
        if cover_url != self._preview_cover_url:
            self.CoverImg.clear()
            self._displayed_cover_url = ""
        self._preview_cover_url = cover_url
        self._start_preview_thumbnail()
        artists_full = song_meta.get("artists", "")
        artist_list = [a.strip() for a in artists_full.split(",") if a.strip()]
        artists_display = (
            f"{artist_list[0]}, {artist_list[1]} +{len(artist_list) - 2}"
            if len(artist_list) > 2
            else artists_full
        )
        self.ArtistNameText.setText(artists_display)
        self.ArtistNameText.setToolTip(artists_full)
        self.AlbumText.setText(song_meta.get("album", ""))
        self.SongName.setText(song_meta.get("title", ""))
        self.YearText.setText(song_meta.get("releaseDate", ""))
        self.MainSongName.setText(song_meta.get("title", "") + " - " + song_meta.get("artists", ""))

    @pyqtSlot(int, int)
    def preview_source_started(self, index, total):
        if index < self._preview_source:
            return
        self._preview_source = index
        self._preview_meta = {}
        self._preview_cover_url = ""
        self._displayed_cover_url = ""
        for widget in (
            self.CoverImg,
            self.ArtistNameText,
            self.AlbumText,
            self.SongName,
            self.YearText,
            self.MainSongName,
        ):
            widget.clear()
        self.AlbumName.setText(f"URL {index}/{total}: loading metadata…")

    def _start_preview_thumbnail(self):
        url = self._preview_cover_url
        if not url or not self.showPreviewCheck.isChecked() or url == self._displayed_cover_url:
            return
        active = [
            thread for thread in self._active_threads if isinstance(thread, DownloadThumbnail)
        ]
        if len(active) >= _MAX_THUMBNAIL_THREADS or any(thread.url == url for thread in active):
            return
        thread = DownloadThumbnail(url, self)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def apply_preview_cover(self, url, data):
        if url != self._preview_cover_url:
            return
        pic = QImage()
        if pic.loadFromData(data):
            self.CoverImg.setPixmap(QPixmap.fromImage(pic))
            self._displayed_cover_url = url

    @pyqtSlot(dict)
    def update_queue_progress(self, snapshot):
        if snapshot["revision"] <= self._last_progress_revision:
            return
        self._last_progress_revision = snapshot["revision"]
        self._last_progress_snapshot = dict(snapshot)
        saved, skipped, failed = (snapshot[key] for key in ("downloaded", "skipped", "failed"))
        total = snapshot["total"]
        current = (
            f"{snapshot['processed']}/{total} tracks" if total is not None else "loading metadata"
        )
        self.CounterLabel.setText(
            f"{saved} saved · {skipped} skipped · {failed} failed\n"
            f"URL {snapshot['url_index']}/{snapshot['url_count']} · {current}"
        )
        self.CounterLabel.setToolTip(
            f"Totals across the entire queue.\n{snapshot['reused']} saved files reused audio from an earlier URL."
        )
        progress = min(100, int(snapshot["processed"] / total * 100)) if total else 0
        self.update_song_progress(progress)
        if saved > 0:
            self._maybe_show_star_prompt(saved)

    @pyqtSlot(dict)
    def add_song_META(self, song_meta):
        if self.AddMetaDataCheck.isChecked():
            completed_status = song_meta.get("_metadata_status")
            if completed_status:
                self.statusMsg.setText(completed_status)
                return
            meta_thread = WritingMetaTagsThread(song_meta, song_meta["file"])
            meta_thread.tags_success.connect(lambda x: self.statusMsg.setText(f"{x}"))
            self._active_threads.append(meta_thread)
            meta_thread.finished.connect(lambda: self._cleanup_thread(meta_thread))
            meta_thread.start()

    def _cleanup_thread(self, thread):
        """Remove finished thread from active list."""
        if thread in self._active_threads:
            self._active_threads.remove(thread)
        if isinstance(thread, DownloadThumbnail) and thread.url != self._preview_cover_url:
            # A full thumbnail pool must not drop the newest requested cover.
            self._start_preview_thumbnail()

    @pyqtSlot(str)
    def update_AlbumName(self, AlbumName):
        self.AlbumName.setText(AlbumName)
        self.AlbumName.setToolTip(AlbumName)

    def _live_scraper(self):
        """The running scraper, or None once the thread is gone (qt raises
        RuntimeError on a deleted object, which outlives the python ref)."""
        try:
            return getattr(getattr(self, "scraper_thread", None), "scraper", None)
        except RuntimeError:
            return None

    def _failed_track_count(self) -> int:
        return len(getattr(self._live_scraper(), "_failed_tracks", []) or [])

    @pyqtSlot(int)
    def update_counter(self, count):
        scraper = self._live_scraper()
        total = getattr(scraper, "_total_tracks", 0) or 0
        failed = self._failed_track_count()
        # count is "tracks finished", not "tracks saved": a failure ticks it too
        text = f"Songs downloaded {max(0, count - failed)}"
        if total:
            text += f" of {total}"
        if failed:
            text += f" ({failed} failed)"
        self.CounterLabel.setText(text)

    @pyqtSlot(int)
    def update_song_progress(self, progress):
        self.SongDownloadprogressBar.setValue(progress)
        self.SongDownloadprogress.setValue(progress)

    @pyqtSlot(int)
    def Reset_song_progress(self, progress):
        self.SongDownloadprogressBar.setValue(0)
        self.SongDownloadprogress.setValue(0)

    # DRAGGLESS INTERFACE
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.m_drag = True
            self.m_DragPosition = event.globalPosition().toPoint() - self.pos()
            event.accept()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))

    def mouseMoveEvent(self, QMouseEvent):
        try:
            if Qt.MouseButton.LeftButton and self.m_drag:
                self.move(QMouseEvent.globalPosition().toPoint() - self.m_DragPosition)
                QMouseEvent.accept()
        except AttributeError:
            pass

    def mouseReleaseEvent(self, QMouseEvent):
        self.m_drag = False
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def CloseSongInformation(self):
        self.animation = QPropertyAnimation(self.SONGINFORMATION, b"size")
        self.animation.setDuration(250)
        self.animation.setEndValue(QSize(0, 440))
        self.animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.animation.start()

    def OpenSongInformation(self):
        self.animation = QPropertyAnimation(self.SONGINFORMATION, b"size")
        self.animation.setDuration(1000)
        self.animation.setEndValue(QSize(350, 440))
        self.animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.animation.start()

    def show_preview(self, state):
        if state == 2:  # 2 corresponds to checked state
            self.preview_window = self.OpenSongInformation()
            self._start_preview_thumbnail()
        else:
            self.CloseSongInformation()

    def exitprogram(self):
        # QApplication.quit() unwinds the event loop cleanly so app.exec()
        # returns and the atexit session-end runs; sys.exit() inside a slot
        # raised SystemExit into qt's excepthook and logged a false crash
        QApplication.quit()

    def Linkedin(self):
        webbrowser.open("https://www.linkedin.com/in/sunny-patel-30b460204/")


# Main
if __name__ == "__main__":
    # Headless CLI dispatch (before any Qt/logging setup): a known first arg
    # routes to sunnify_cli; anything else - including a bare double-click -
    # is the GUI, byte-identical to before. The alias makes the frozen
    # binary's __main__ and `import Spotify_Downloader` the same module.
    _cli_commands = ("download", "info", "status", "config", "doctor", "help")
    _arg1 = sys.argv[1] if len(sys.argv) > 1 else ""
    if _arg1 in _cli_commands or _arg1 in ("--version", "-V", "--help", "-h"):
        sys.modules.setdefault("Spotify_Downloader", sys.modules[__name__])
        import sunnify_cli

        sys.exit(sunnify_cli.main(sys.argv[1:]))
    if _arg1.isascii() and _arg1.isalpha():
        # a bare word is a command attempt, not launcher argv (macOS -psn_*,
        # file paths all carry non-letters); don't swallow it into a GUI launch
        import difflib

        close = difflib.get_close_matches(_arg1.lower(), _cli_commands, n=1)
        hint = f" (did you mean '{close[0]}'?)" if close else ""
        print(
            f"sunnify: unknown command '{_arg1}'{hint}\nrun 'sunnify --help' for usage",
            file=sys.stderr,
        )
        sys.exit(2)

    # Logging must never stop the app from launching (e.g. a locked-down or
    # read-only log dir). Failure here just means no log file this session.
    with contextlib.suppress(Exception):
        setup_logging()

    # sigint/sigterm: log WHICH signal killed us, then re-raise under
    # SIG_DFL. A default-action signal skips atexit and every hook (why
    # signal deaths never left a log line); this keeps instant-kill
    # semantics with no event-loop dependence, plus one forensic line.
    def _fatal_signal(sig, _frame):
        with contextlib.suppress(Exception):
            log.info("terminated by signal %s (ctrl+c or external)", signal.Signals(sig).name)
            logging.shutdown()
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)

    with contextlib.suppress(Exception):
        signal.signal(signal.SIGINT, _fatal_signal)
        signal.signal(signal.SIGTERM, _fatal_signal)
    # fixed-pixel ui overflows on fractional dpi without this (#64); PassThrough avoids
    # rounding 150%->100%. must precede QApplication. ref: doc.qt.io/qt-6/highdpi.html
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception as exc:  # log so a scaling failure (rendering bugs) is diagnosable
        log.debug("high-dpi setup skipped: %s", exc)
    app = QApplication(sys.argv)
    # wake python every 200ms during exec() so a pending signal's handler runs
    # promptly (qt's c++ loop otherwise defers python signals until a qt slot)
    _signal_waker = QTimer()
    _signal_waker.start(200)
    _signal_waker.timeout.connect(lambda: None)
    Screen = MainWindow()
    Screen.setFixedHeight(500)
    Screen.setFixedWidth(750)
    Screen.setWindowFlags(Qt.WindowType.FramelessWindowHint)
    Screen.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    Screen.show()
    sys.exit(app.exec())
