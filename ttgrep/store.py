"""On-disk cache. One directory per account, JSON only, atomic writes.

Layout:
    $TTGREP_HOME (default ~/.ttgrep)/
        accounts/<handle>/index.json            account + per-video metadata & status
        accounts/<handle>/transcripts/<id>.json all caption tracks for one video
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1

_HANDLE_URL_RE = re.compile(r"tiktok\.com/@([\w.\-]+)")
_VIDEO_URL_RE = re.compile(r"tiktok\.com/@[\w.\-]*/(?:video|photo)/(\d+)")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def home() -> Path:
    env = os.environ.get("TTGREP_HOME")
    return Path(env) if env else Path.home() / ".ttgrep"


def accounts_root() -> Path:
    return home() / "accounts"


def normalize_account(ref: str) -> str:
    """Accept '@handle', 'handle', or a tiktok.com profile/video URL."""
    ref = ref.strip()
    m = _HANDLE_URL_RE.search(ref)
    if m:
        return m.group(1).lower()
    return ref.lstrip("@").lower()


def parse_video_ref(ref: str) -> str | None:
    """Return the numeric video id from a URL or bare id, else None."""
    ref = ref.strip()
    if ref.isdigit():
        return ref
    m = _VIDEO_URL_RE.search(ref)
    return m.group(1) if m else None


def account_dir(handle: str) -> Path:
    return accounts_root() / handle


def index_path(handle: str) -> Path:
    return account_dir(handle) / "index.json"


def transcript_path(handle: str, video_id: str) -> Path:
    return account_dir(handle) / "transcripts" / f"{video_id}.json"


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def new_index(handle: str) -> dict:
    return {
        "schema": SCHEMA,
        "account": handle,
        "url": f"https://www.tiktok.com/@{handle}",
        "channel": None,
        "listed_at": None,
        "listing_complete": False,
        "videos": [],
    }


def load_index(handle: str) -> dict | None:
    p = index_path(handle)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_index(idx: dict) -> None:
    atomic_write_json(index_path(idx["account"]), idx)


def load_transcript(handle: str, video_id: str) -> dict | None:
    p = transcript_path(handle, video_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_transcript(handle: str, obj: dict) -> None:
    atomic_write_json(transcript_path(handle, obj["id"]), obj)


def list_accounts() -> list[str]:
    root = accounts_root()
    if not root.exists():
        return []
    return sorted(d.name for d in root.iterdir() if (d / "index.json").exists())


def video_map(idx: dict) -> dict[str, dict]:
    return {v["id"]: v for v in idx["videos"]}


def find_video(video_id: str):
    """Search every cached account for a video id -> (handle, index, entry)."""
    for handle in list_accounts():
        idx = load_index(handle)
        for v in idx["videos"]:
            if v["id"] == video_id:
                return handle, idx, v
    return None, None, None
