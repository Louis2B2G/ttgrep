"""yt-dlp wrappers for TikTok, plus VTT parsing.

Empirical notes (verified 2026-08, yt-dlp 2026.08.19):
- A flat-playlist extraction of a profile URL returns rich entries: id, url,
  full `description` (the `title` field is truncated), `timestamp`, duration,
  view/like/comment/repost/save counts. Listing an account never requires
  per-video fetches.
- Captions live in the info dict's `subtitles` (NOT `automatic_captions`),
  keyed by TikTok-specific codes such as `eng-US` / `fra-FR`, one `vtt`
  format per language. Many videos have no captions at all; some have
  several languages (original + translations).
- A TikTok video id encodes its upload time: (id >> 32) is unix seconds.
- A placeholder handle resolves: tiktok.com/@_/video/<id> works for bare ids.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

_PLACEHOLDER_VIDEO_URL = "https://www.tiktok.com/@_/video/{id}"

# Substrings of yt-dlp error messages that mean "retrying won't help".
_PERMANENT_ERROR_MARKERS = (
    "not available",
    "unavailable",
    "private",
    "unable to find video",
    "doesn't exist",
    "does not exist",
    "10204",  # TikTok status code: video deleted / region blocked
    "10222",  # TikTok status code: private video
    "10231",
    "requested format",
    "unsupported url",
)


class CaptionDownloadError(RuntimeError):
    """Captions exist for the video but none could be downloaded (transient)."""


class _SilentLogger:
    """Swallow yt-dlp chatter; keep the last error/warning line for context."""

    def __init__(self):
        self.last_error = None
        self.last_warning = None

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.last_warning = msg

    def error(self, msg):
        self.last_error = msg


def _base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_retries": 2,
        "logger": _SilentLogger(),
    }


def clean_error(exc: BaseException) -> str:
    msg = str(exc)
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r";?\s*please report this issue.*", "", msg, flags=re.IGNORECASE | re.DOTALL)
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg[:300]


def is_permanent_error(msg: str) -> bool:
    low = msg.lower()
    return any(marker in low for marker in _PERMANENT_ERROR_MARKERS)


def is_image_post_error(msg: str) -> bool:
    """TikTok serves many image/slideshow posts under /video/ URLs; extracting
    them fails with 'No video formats found'. That's content type, not failure:
    an image post has no audio, hence never a transcript."""
    return "no video formats" in msg.lower()


def profile_url(handle: str) -> str:
    return f"https://www.tiktok.com/@{handle}"


def video_url_for_id(video_id: str) -> str:
    return _PLACEHOLDER_VIDEO_URL.format(id=video_id)


def timestamp_from_id(video_id: str) -> int | None:
    try:
        ts = int(video_id) >> 32
    except ValueError:
        return None
    # Sanity window: 2016..2099
    return ts if 1_451_606_400 < ts < 4_102_444_800 else None


def list_account(handle: str, limit: int | None = None) -> tuple[dict, list[dict]]:
    """Flat-list an account's posts, newest first. Returns (playlist_info, entries)."""
    import yt_dlp

    opts = _base_opts()
    opts["extract_flat"] = "in_playlist"
    opts["ignoreerrors"] = True  # skip individual broken entries, keep the rest
    if limit:
        opts["playlistend"] = limit
    logger = opts["logger"]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(profile_url(handle), download=False)
    if info is None:
        # With ignoreerrors, top-level failures surface as None; the real
        # message (e.g. "user doesn't exist") went to the logger.
        raise RuntimeError(clean_error(Exception(logger.last_error or "no data returned")))
    entries = [e for e in (info.get("entries") or []) if e]
    return info, entries


class VideoFetcher:
    """One yt-dlp instance reused across per-video fetches.

    Caption files are downloaded by yt-dlp's own subtitle machinery
    (skip_download + writesubtitles into a scratch dir), not by fetching the
    caption URLs ourselves: the CDN 403s bare requests for some videos, and
    yt-dlp's downloader knows the headers/session tricks. Whatever the CLI
    `--write-subs` can fetch, this fetches.
    """

    def __init__(self):
        import tempfile

        import yt_dlp

        self._tmp = tempfile.TemporaryDirectory(prefix="ttgrep-")
        self.workdir = Path(self._tmp.name)
        self._logger = _SilentLogger()
        opts = _base_opts()
        opts["logger"] = self._logger
        # Extractors only populate subtitles when these params are set
        # (extract_subtitles() is gated on them); without this the info dict
        # silently reports no captions for videos that have them.
        opts.update({
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["all"],
            "outtmpl": str(self.workdir / "%(id)s.%(ext)s"),
        })
        self._ydl = yt_dlp.YoutubeDL(opts)

    def fetch(self, url: str) -> tuple[dict, dict]:
        """Extract one video and download+parse all its caption tracks.

        Returns (info, tracks). Raises CaptionDownloadError when the video
        advertises captions but none could be downloaded.
        """
        info = self._ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError(self._logger.last_error or "no data returned")
        tracks = {}
        vid = str(info.get("id"))
        for f in sorted(self.workdir.glob(f"{vid}.*")):
            parts = f.name.split(".")
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            finally:
                f.unlink(missing_ok=True)
            if len(parts) < 3:  # not <id>.<lang>.<ext>
                continue
            segments = parse_cues(text)
            if segments:
                tracks[".".join(parts[1:-1])] = {
                    "segments": segments,
                    "text": " ".join(s["text"] for s in segments),
                }
        if not tracks and caption_sources(info):
            detail = self._logger.last_warning or self._logger.last_error or "no files written"
            raise CaptionDownloadError(
                f"captions advertised ({','.join(sorted(caption_sources(info)))}) "
                f"but not downloadable: {detail}")
        return info, tracks


def caption_sources(info: dict) -> dict:
    """Caption tracks by language; `subtitles` wins over `automatic_captions`."""
    return {**(info.get("automatic_captions") or {}), **(info.get("subtitles") or {})}


# --- VTT / SRT ---------------------------------------------------------
# TikTok serves most captions as VTT files, but one extractor path yields
# inline SRT data (comma decimal separator), so accept both timecode styles.

_TS_PART = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})"
_CUE_RE = re.compile(_TS_PART + r"\s*-->\s*" + _TS_PART)
_TAG_RE = re.compile(r"<[^>]+>")


def _ts_seconds(groups, offset: int) -> float:
    h = int(groups[offset] or 0)
    m = int(groups[offset + 1])
    s = int(groups[offset + 2])
    ms = int(groups[offset + 3])
    return h * 3600 + m * 60 + s + ms / 1000.0


def parse_cues(text: str) -> list[dict]:
    """Parse VTT or SRT into [{start, end, text}]. Merges consecutive duplicate cues."""
    segments: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _CUE_RE.search(lines[i])
        if not m:
            i += 1
            continue
        g = m.groups()
        start, end = _ts_seconds(g, 0), _ts_seconds(g, 4)
        i += 1
        buf = []
        while i < len(lines) and lines[i].strip() and not _CUE_RE.search(lines[i]):
            buf.append(_TAG_RE.sub("", lines[i]).strip())
            i += 1
        cue = " ".join(b for b in buf if b).strip()
        if not cue:
            continue
        if segments and segments[-1]["text"] == cue:
            segments[-1]["end"] = end
        else:
            segments.append({"start": round(start, 3), "end": round(end, 3), "text": cue})
    return segments


def fmt_time(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# --- index entries -----------------------------------------------------

def _clean_ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _date_from_ts(ts: int | None, video_id: str) -> str | None:
    from datetime import datetime, timezone

    ts = ts or timestamp_from_id(video_id)
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def entry_from_listing(e: dict) -> dict:
    """Build an index entry from one flat-playlist entry."""
    vid = str(e.get("id"))
    url = e.get("url") or ""
    kind = "photo" if "/photo/" in url else "video"
    ts = e.get("timestamp") or timestamp_from_id(vid)
    return {
        "id": vid,
        "url": url,
        "title": _clean_ws(e.get("description") or e.get("title")),
        "timestamp": ts,
        "date": _date_from_ts(ts, vid),
        "duration": e.get("duration"),
        "views": e.get("view_count"),
        "likes": e.get("like_count"),
        "comments": e.get("comment_count"),
        "kind": kind,
        "listed": True,
        # Photo posts have no audio track, hence never captions.
        "transcript": "none" if kind == "photo" else "pending",
    }


def refresh_entry_from_info(v: dict, info: dict) -> None:
    """Update an index entry in place from a full per-video info dict."""
    ts = info.get("timestamp") or v.get("timestamp") or timestamp_from_id(v["id"])
    v["timestamp"] = ts
    v["date"] = _date_from_ts(ts, v["id"])
    title = _clean_ws(info.get("description") or info.get("title"))
    if title:
        v["title"] = title
    for src, dst in (
        ("duration", "duration"),
        ("view_count", "views"),
        ("like_count", "likes"),
        ("comment_count", "comments"),
    ):
        if info.get(src) is not None:
            v[dst] = info[src]


def entry_from_info(info: dict) -> dict:
    vid = str(info.get("id"))
    v = {
        "id": vid,
        "url": info.get("webpage_url") or video_url_for_id(vid),
        "title": "",
        "timestamp": None,
        "date": None,
        "duration": None,
        "views": None,
        "likes": None,
        "comments": None,
        "kind": "video",
        "listed": False,  # discovered via direct fetch, not via the profile listing
        "transcript": "pending",
    }
    refresh_entry_from_info(v, info)
    return v


def download_audio(url: str, workdir: Path) -> Path:
    """Download a video's audio (or smallest muxed file) for local ASR."""
    import yt_dlp

    opts = _base_opts()
    opts.update({
        "format": "ba/b",  # best audio-only, else best muxed (ffmpeg extracts)
        "outtmpl": str(Path(workdir) / "audio-%(id)s.%(ext)s"),
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError("no data returned")
        path = traverse_download_path(info) or Path(ydl.prepare_filename(info))
    if not path.exists():
        raise RuntimeError("audio download produced no file")
    return path


def traverse_download_path(info: dict) -> Path | None:
    for d in info.get("requested_downloads") or []:
        if d.get("filepath"):
            return Path(d["filepath"])
    return None


def polite_sleep(base: float) -> None:
    import random

    if base > 0:
        time.sleep(base + random.uniform(0, base * 0.5))
