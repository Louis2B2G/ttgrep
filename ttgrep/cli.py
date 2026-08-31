"""ttgrep — agent-first retrieval of TikTok account content.

Commands:
    sync        fetch/refresh an account (the only slow command; resumable)
    videos      list an account's posts from cache
    transcript  print what was said in one video
    search      find which of an account's videos mention something
    status      cache overview
    doctor      self-check (versions, cache, live TikTok probe)

Exit codes: 0 = ok, 1 = search found no matches, 2 = error.
All data goes to stdout; progress/warnings go to stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from bisect import bisect_right
from collections import Counter
from pathlib import Path

from ttgrep import __version__, asr, fetch, store


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _fail(msg: str) -> int:
    _err(f"error: {msg}")
    return 2


def _num(x) -> str:
    return "-" if x is None else str(x)


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _sorted_videos(idx: dict) -> list[dict]:
    return sorted(idx["videos"], key=lambda v: (v.get("timestamp") or 0), reverse=True)


def _status_counts(idx: dict) -> Counter:
    return Counter(v.get("transcript") for v in idx["videos"])


# --- language choice ---------------------------------------------------

def _dominant_langs(idx: dict | None) -> list[str]:
    """Caption languages of an account, most plausible original language first.

    The original language is the one present on the most videos, and
    especially the one that appears *alone* (translations only ever appear
    alongside the original).
    """
    if not idx:
        return []
    count: Counter = Counter()
    alone: Counter = Counter()
    for v in idx["videos"]:
        langs = v.get("available_langs") or []
        for lang in langs:
            count[lang] += 1
        if len(langs) == 1:
            alone[langs[0]] += 1
    return sorted(count, key=lambda L: (-count[L], -alone[L], L))


def _choose_lang(tracks: dict, idx: dict | None, override: str | None) -> str | None:
    if not tracks:
        return None
    if override:
        for lang in tracks:
            if lang.lower() == override.lower():
                return lang
        for lang in sorted(tracks):
            if lang.lower().startswith(override.lower()):
                return lang
        return None
    if len(tracks) == 1:
        return next(iter(tracks))
    for lang in _dominant_langs(idx):
        if lang in tracks:
            return lang
    return sorted(tracks)[0]


# --- listing / sync ----------------------------------------------------

def _merge_listing(idx: dict, entries: list[dict], complete: bool) -> None:
    old = store.video_map(idx)
    merged, seen = [], set()
    for e in entries:
        ne = fetch.entry_from_listing(e)
        oe = old.get(ne["id"])
        if oe:
            for k in ("transcript", "available_langs", "checked_at", "error"):
                if k in oe:
                    ne[k] = oe[k]
            if not ne["title"]:
                ne["title"] = oe.get("title", "")
        seen.add(ne["id"])
        merged.append(ne)
    for v in idx["videos"]:
        if v["id"] not in seen:
            if complete:
                v["listed"] = False  # gone from the profile, but keep what we know
            merged.append(v)
    merged.sort(key=lambda v: (v.get("timestamp") or 0), reverse=True)
    idx["videos"] = merged
    if entries and entries[0].get("channel"):
        idx["channel"] = entries[0]["channel"]
    idx["listed_at"] = store.utcnow()
    if complete:
        idx["listing_complete"] = True


def _refresh_listing(idx: dict, handle: str, limit: int | None) -> int:
    _err(f"listing @{handle}" + (f" (top {limit})" if limit else " (full)") + " ...")
    try:
        _, entries = fetch.list_account(handle, limit)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        raise RuntimeError(fetch.clean_error(exc)) from exc
    _merge_listing(idx, entries, complete=limit is None)
    store.save_index(idx)
    _err(f"listing: {len(entries)} posts")
    return len(entries)


def _apply_fetch_result(handle: str, v: dict, info: dict, tracks: dict) -> tuple[str, str]:
    """Record one fetched video (entry updated in place). Returns (status, detail)."""
    fetch.refresh_entry_from_info(v, info)
    v["checked_at"] = store.utcnow()
    v["available_langs"] = sorted(fetch.caption_sources(info))
    if tracks:
        store.save_transcript(handle, {
            "id": v["id"],
            "account": handle,
            "fetched_at": store.utcnow(),
            "tracks": tracks,
        })
        v["transcript"] = "ok"
        v.pop("error", None)
        return "ok", ",".join(sorted(tracks))
    v["transcript"] = "none"  # a normal outcome: the video has no captions
    v.pop("error", None)
    return "none", ""


def _fetch_transcript_for(fetcher: fetch.VideoFetcher, handle: str, v: dict) -> tuple[str, str]:
    """Fetch captions for one video with retries. Returns (status, detail)."""
    attempts = 0
    while True:
        attempts += 1
        try:
            info, tracks = fetcher.fetch(v["url"])
            return _apply_fetch_result(handle, v, info, tracks)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            msg = fetch.clean_error(exc)
            if fetch.is_image_post_error(msg):
                v["kind"] = "photo"  # slideshow served under a /video/ URL
                v["transcript"] = "none"
                v["checked_at"] = store.utcnow()
                v.pop("error", None)
                return "none", "(image post)"
            if fetch.is_permanent_error(msg) or attempts >= 3:
                v["transcript"] = "error"
                v["error"] = msg
                return "error", msg
            time.sleep(4 * attempts)  # transient (network/throttle): back off, retry


def _print_sync_summary(idx: dict, fetched: int, transcribed: int = 0) -> None:
    c = _status_counts(idx)
    print(f"account: {idx['account']}")
    print(f"videos: {len(idx['videos'])}")
    print(f"listing: {'complete' if idx.get('listing_complete') else 'partial'}")
    print(f"transcripts_ok: {c.get('ok', 0)}")
    print(f"transcripts_asr: {c.get('asr', 0)}")
    print(f"no_captions: {c.get('none', 0)}")
    print(f"pending: {c.get('pending', 0)}")
    print(f"errors: {c.get('error', 0)}")
    print(f"fetched_this_run: {fetched}")
    print(f"transcribed_this_run: {transcribed}")


# --- local ASR for captionless videos ----------------------------------

def _asr_one(handle: str, v: dict, workdir: Path, model: str) -> tuple[str, str]:
    """Download one video's audio and transcribe it locally.

    Returns (status, detail); flips the entry to 'asr' when speech was found.
    """
    audio = fetch.download_audio(v["url"], workdir)
    try:
        lang, segments = asr.transcribe(audio, model)
    finally:
        audio.unlink(missing_ok=True)
    v["asr_checked"] = store.utcnow()
    if not segments:
        return "no_speech", ""  # music-only etc.; stays 'none', never retried
    t = store.load_transcript(handle, v["id"]) or {"id": v["id"], "account": handle, "tracks": {}}
    t["fetched_at"] = store.utcnow()
    t["tracks"][lang or "und"] = {
        "segments": segments,
        "text": " ".join(s["text"] for s in segments),
        "source": f"whisper-{model}",
    }
    store.save_transcript(handle, t)
    v["transcript"] = "asr"
    v["lang"] = lang
    return "asr", lang or ""


def _run_asr_pass(idx: dict, handle: str, limit: int | None, model: str, sleep: float) -> int:
    videos = _sorted_videos(idx)
    pool = videos[: limit] if limit else videos
    targets = [v for v in pool
               if v.get("transcript") == "none" and v.get("kind") != "photo"
               and not v.get("asr_checked")]
    if not targets:
        return 0
    if asr.backend() is None:
        _err(f"note: {len(targets)} captionless videos could be transcribed locally, "
             f"but ASR is unavailable ({asr.unavailable_reason()})")
        return 0
    _err(f"transcribing {len(targets)} captionless videos locally "
         f"(whisper-{model} on {asr.backend()}; first ever run downloads the model)")
    done = 0
    with tempfile.TemporaryDirectory(prefix="ttgrep-asr-") as td:
        for n, v in enumerate(targets, 1):
            t0 = time.time()
            try:
                status, detail = _asr_one(handle, v, Path(td), model)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                msg = fetch.clean_error(exc)
                if fetch.is_image_post_error(msg):
                    v["kind"] = "photo"
                    v["asr_checked"] = store.utcnow()
                    status, detail = "skip", "(image post)"
                else:
                    status, detail = "asr_failed", msg  # stays 'none'
            done += 1
            store.save_index(idx)
            _err(f"[{n}/{len(targets)}] {v['id']} {status}"
                 + (f" {detail}" if detail else "") + f" ({time.time() - t0:.1f}s)")
            if n < len(targets):
                fetch.polite_sleep(sleep)
    return done


def cmd_sync(args) -> int:
    handle = store.normalize_account(args.account)
    idx = store.load_index(handle) or store.new_index(handle)

    if not args.no_listing:
        try:
            _refresh_listing(idx, handle, args.limit)
        except RuntimeError as exc:
            return _fail(f"listing @{handle} failed: {exc}")
    elif not idx["videos"]:
        return _fail(f"no cached listing for @{handle}; run sync without --no-listing first")

    fetched = transcribed = 0
    if not args.listing_only:
        videos = _sorted_videos(idx)
        pool = videos[: args.limit] if args.limit else videos
        wanted = ("pending", "error") if args.retry_errors else ("pending",)
        targets = [v for v in pool if v.get("transcript") in wanted and v.get("kind") != "photo"]
        if targets:
            _err(f"fetching captions for {len(targets)} videos "
                 f"(~{args.sleep:.0f}s+fetch each; safe to interrupt, resumes where it left off)")
        fetcher = fetch.VideoFetcher()
        try:
            for n, v in enumerate(targets, 1):
                status, detail = _fetch_transcript_for(fetcher, handle, v)
                fetched += 1
                store.save_index(idx)
                _err(f"[{n}/{len(targets)}] {v['id']} {status}" + (f" {detail}" if detail else ""))
                if n < len(targets):
                    fetch.polite_sleep(args.sleep)
            if not args.no_asr:
                transcribed = _run_asr_pass(idx, handle, args.limit, args.asr_model, args.sleep)
        except KeyboardInterrupt:
            store.save_index(idx)
            _err("interrupted; progress saved (re-run sync to continue)")
            _print_sync_summary(idx, fetched, transcribed)
            return 130

    _print_sync_summary(idx, fetched, transcribed)
    return 0


# --- videos ------------------------------------------------------------

def cmd_videos(args) -> int:
    handle = store.normalize_account(args.account)
    idx = store.load_index(handle)
    if idx is None:
        _err(f"no cache for @{handle}; fetching listing first (transcripts stay unfetched)")
        idx = store.new_index(handle)
        try:
            _refresh_listing(idx, handle, args.limit)
        except RuntimeError as exc:
            return _fail(f"listing @{handle} failed: {exc}")

    videos = _sorted_videos(idx)
    if args.since:
        videos = [v for v in videos if (v.get("date") or "") >= args.since]
    if args.limit:
        videos = videos[: args.limit]

    if args.json:
        for v in videos:
            print(json.dumps(v, ensure_ascii=False))
    else:
        print("id\tdate\tdur\tviews\tlikes\tcomments\ttranscript\ttitle")
        for v in videos:
            tr = "photo" if v.get("kind") == "photo" else v.get("transcript", "pending")
            title = v.get("title") or ""
            if not args.full:
                title = _trunc(title, 200)
            print("\t".join([
                v["id"], v.get("date") or "-", _num(v.get("duration")),
                _num(v.get("views")), _num(v.get("likes")), _num(v.get("comments")),
                tr, title,
            ]))
    c = _status_counts(idx)
    _err(f"@{handle}: {len(idx['videos'])} posts cached "
         f"({'complete' if idx.get('listing_complete') else 'partial'} listing), "
         f"transcripts: {c.get('ok', 0)} ok, {c.get('asr', 0)} asr, {c.get('none', 0)} none, "
         f"{c.get('pending', 0)} pending, {c.get('error', 0)} error")
    return 0


# --- transcript --------------------------------------------------------

def _resolve_or_fetch_video(ref: str) -> tuple[str, dict, dict] | int:
    """Return (handle, idx, entry) for a video ref, fetching on demand if unknown."""
    vid = store.parse_video_ref(ref)
    if vid is None:
        return _fail(f"not a video URL or id: {ref!r}")
    handle, idx, v = store.find_video(vid)
    if v is not None and v.get("transcript") in ("ok", "asr", "none"):
        return handle, idx, v

    # Unknown video (or a previous fetch failed): fetch it now.
    url = ref if "tiktok.com/" in ref else fetch.video_url_for_id(vid)
    if v is not None:
        url = v.get("url") or url
    _err(f"fetching video {vid} ...")
    fetcher = fetch.VideoFetcher()
    info = tracks = None
    for attempt in (1, 2):
        try:
            info, tracks = fetcher.fetch(url)
            break
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            msg = fetch.clean_error(exc)
            if fetch.is_permanent_error(msg) or attempt == 2:
                return _fail(f"could not fetch video {vid}: {msg}")
            time.sleep(3)

    handle = store.normalize_account(info.get("uploader") or handle or "_unknown")
    idx = store.load_index(handle) or store.new_index(handle)
    v = store.video_map(idx).get(vid)
    if v is None:
        v = fetch.entry_from_info(info)
        idx["videos"].append(v)
    _apply_fetch_result(handle, v, info, tracks)
    store.save_index(idx)
    return handle, idx, v


def _transcript_header(handle: str, v: dict, langs: list[str], lang: str | None) -> list[str]:
    return [
        f"id: {v['id']}",
        f"account: {handle}",
        f"date: {v.get('date') or '-'}",
        f"duration: {_num(v.get('duration'))}",
        f"views: {_num(v.get('views'))}",
        f"url: {v.get('url')}",
        f"langs: {','.join(langs) if langs else '-'}",
        f"lang: {lang or '-'}",
        f"title: {v.get('title') or '-'}",
    ]


def cmd_transcript(args) -> int:
    res = _resolve_or_fetch_video(args.video)
    if isinstance(res, int):
        return res
    handle, idx, v = res

    # No captions? Fall back to transcribing the audio locally, once.
    if (v.get("transcript") == "none" and v.get("kind") != "photo"
            and not args.no_asr and not v.get("asr_checked")):
        if asr.backend() is None:
            _err(f"no captions, and local ASR is unavailable ({asr.unavailable_reason()})")
        else:
            _err(f"no captions; transcribing audio locally (whisper-{args.asr_model}) ...")
            with tempfile.TemporaryDirectory(prefix="ttgrep-asr-") as td:
                try:
                    _asr_one(handle, v, Path(td), args.asr_model)
                except Exception as exc:
                    _err(f"asr failed: {fetch.clean_error(exc)}")
            store.save_index(idx)

    langs = v.get("available_langs") or []
    t = store.load_transcript(handle, v["id"]) if v.get("transcript") in ("ok", "asr") else None

    if t is None:  # no captions on this video — a normal outcome, not an error
        if args.json:
            print(json.dumps({**v, "account": handle, "transcript": "none", "tracks": {}},
                             ensure_ascii=False))
        else:
            print("\n".join(_transcript_header(handle, v, langs, None)))
            print("---")
            print("transcript: none (video has no captions)")
        return 0

    lang = _choose_lang(t["tracks"], idx, args.lang)
    if lang is None:
        return _fail(f"language {args.lang!r} not available; "
                     f"available: {','.join(sorted(t['tracks']))}")

    if args.json:
        print(json.dumps({**v, "account": handle, "lang_default": lang,
                          "tracks": t["tracks"]}, ensure_ascii=False))
        return 0

    print("\n".join(_transcript_header(handle, v, langs, lang)))
    print("---")
    track = t["tracks"][lang]
    if args.timestamps:
        for s in track["segments"]:
            print(f"[{fetch.fmt_time(s['start'])}] {s['text']}")
    else:
        print(track["text"])
    return 0


# --- search ------------------------------------------------------------

def _match_spans(text: str, query: str, regex: re.Pattern | None) -> list[tuple[int, int]]:
    if regex is not None:
        return [m.span() for m in regex.finditer(text)]
    spans, low, q = [], text.lower(), query.lower()
    i = low.find(q)
    while i != -1:
        spans.append((i, i + len(q)))
        i = low.find(q, i + 1)
    return spans


def _segment_hits(segments: list[dict], query: str, regex: re.Pattern | None,
                  max_hits: int) -> tuple[list[dict], int]:
    """Match against the joined text (so phrases can cross cue boundaries),
    then map matches back to segments and emit merged context windows."""
    starts, off = [], 0
    for s in segments:
        starts.append(off)
        off += len(s["text"]) + 1
    joined = " ".join(s["text"] for s in segments)

    idxs = sorted({max(0, bisect_right(starts, a) - 1)
                   for a, _ in _match_spans(joined, query, regex)})
    if not idxs:
        return [], 0

    groups: list[list[int]] = []
    for i in idxs:
        if groups and i - groups[-1][-1] <= 2:
            groups[-1].append(i)
        else:
            groups.append([i])

    hits = []
    for g in groups[:max_hits]:
        a, b = max(0, g[0] - 1), min(len(segments) - 1, g[-1] + 1)
        hits.append({
            "start": segments[g[0]]["start"],
            "ts": fetch.fmt_time(segments[g[0]]["start"]),
            "text": " ".join(s["text"] for s in segments[a:b + 1]),
        })
    return hits, max(0, len(groups) - max_hits)


def cmd_search(args) -> int:
    # Both positionals are optional in argparse so that `search --all QUERY`
    # parses; sort out which form was used here.
    if args.query is None and args.account is not None:
        args.query, args.account = args.account, None
    if args.query is None:
        return _fail('usage: search ACCOUNT QUERY, or search --all QUERY')
    if args.all:
        accounts = store.list_accounts()
        if not accounts:
            return _fail("no cached accounts; run `ttgrep sync <account>` first")
    else:
        if not args.account:
            return _fail("give an account, or use --all to search every cached account")
        accounts = [store.normalize_account(args.account)]

    regex = None
    if args.regex:
        try:
            regex = re.compile(args.query, re.IGNORECASE)
        except re.error as exc:
            return _fail(f"bad regex: {exc}")

    blocks, results = [], []
    searched = candidates = 0
    skipped: Counter = Counter()
    for handle in accounts:
        idx = store.load_index(handle)
        if idx is None:
            return _fail(f"no cache for @{handle}; run `ttgrep sync @{handle}` first")
        for v in _sorted_videos(idx):
            candidates += 1
            title = v.get("title") or ""
            title_spans = _match_spans(title, args.query, regex)
            hits, more = [], 0
            lang = None
            if v.get("transcript") in ("ok", "asr"):
                t = store.load_transcript(handle, v["id"])
                if t and t.get("tracks"):
                    lang = _choose_lang(t["tracks"], idx, args.lang) \
                        or _choose_lang(t["tracks"], idx, None)
                    searched += 1
                    hits, more = _segment_hits(t["tracks"][lang]["segments"],
                                               args.query, regex, args.max_hits)
            else:
                skipped[v.get("transcript", "pending")] += 1
            if not hits and not title_spans:
                continue
            results.append({
                "id": v["id"], "account": handle, "date": v.get("date"),
                "views": v.get("views"), "url": v.get("url"), "lang": lang,
                "title": title, "title_match": bool(title_spans),
                "hits": hits, "more_hits": more,
            })
            lines = [f"== {v['id']}  {v.get('date') or '-'}  @{handle}  "
                     f"views={_num(v.get('views'))}  lang={lang or '-'}"]
            lines.append(f"   {_trunc(title, 180) or '(no title)'}")
            if title_spans and not hits:
                lines.append("   (match in title only" +
                             (")" if v.get("transcript") == "ok" else "; no transcript)"))
            for h in hits:
                lines.append(f"   [{h['ts']}] {h['text']}")
            if more:
                lines.append(f"   … +{more} more; see: ttgrep transcript {v['id']} --timestamps")
            blocks.append("\n".join(lines))

    summary = (f"# query={args.query!r} accounts={len(accounts)} posts={candidates} "
               f"transcripts_searched={searched} matched={len(results)} "
               f"skipped: no_captions={skipped.get('none', 0)} "
               f"pending={skipped.get('pending', 0)} error={skipped.get('error', 0)}")
    if args.json:
        print(summary)
        for r in results:
            print(json.dumps(r, ensure_ascii=False))
    else:
        print(summary)
        for b in blocks:
            print(b)
    if skipped.get("pending"):
        _err(f"note: {skipped['pending']} videos have unfetched transcripts; "
             f"run `ttgrep sync` on the account for full coverage")
    return 0 if results else 1


# --- status / doctor ---------------------------------------------------

def _account_line(idx: dict) -> str:
    c = _status_counts(idx)
    return (f"{idx['account']}: {len(idx['videos'])} posts "
            f"({'complete' if idx.get('listing_complete') else 'partial'} listing), "
            f"transcripts {c.get('ok', 0)} ok / {c.get('asr', 0)} asr / {c.get('none', 0)} none / "
            f"{c.get('pending', 0)} pending / {c.get('error', 0)} error, "
            f"listed_at {idx.get('listed_at') or '-'}")


def cmd_status(args) -> int:
    if args.account:
        handle = store.normalize_account(args.account)
        idx = store.load_index(handle)
        if idx is None:
            return _fail(f"no cache for @{handle}")
        print(_account_line(idx))
        print(f"channel: {idx.get('channel') or '-'}")
        print(f"url: {idx.get('url')}")
        print(f"cache_dir: {store.account_dir(handle)}")
        langs: Counter = Counter()
        for v in idx["videos"]:
            for lang in v.get("available_langs") or []:
                langs[lang] += 1
        print("caption_langs: " + (", ".join(f"{L}={n}" for L, n in langs.most_common()) or "-"))
        errors = [v for v in idx["videos"] if v.get("transcript") == "error"]
        for v in errors[:10]:
            print(f"error {v['id']} ({v.get('date') or '-'}): {v.get('error', '?')}")
        if len(errors) > 10:
            print(f"… +{len(errors) - 10} more errors")
        return 0
    accounts = store.list_accounts()
    if not accounts:
        print(f"cache empty ({store.home()}); run `ttgrep sync <account>` to start")
        return 0
    for handle in accounts:
        print(_account_line(store.load_index(handle)))
    return 0


def cmd_doctor(args) -> int:
    ok = True
    print(f"ttgrep: {__version__}")
    print(f"python: {sys.version.split()[0]}")
    try:
        import yt_dlp
        print(f"yt-dlp: {yt_dlp.version.__version__}")
    except Exception as exc:
        print(f"yt-dlp: MISSING ({exc})")
        ok = False
    try:
        import curl_cffi  # noqa: F401
        print("curl_cffi: present (TikTok impersonation available)")
    except Exception:
        print("curl_cffi: MISSING — TikTok may block requests; reinstall with the [curl-cffi] extra")
        ok = False
    if asr.backend():
        print(f"asr: {asr.backend()} backend available (local whisper for captionless videos)")
    else:
        print(f"asr: unavailable ({asr.unavailable_reason()}) — captionless videos won't be transcribed")
        ok = False
    home = store.home()
    try:
        store.accounts_root().mkdir(parents=True, exist_ok=True)
        probe = home / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        n_acc = len(store.list_accounts())
        print(f"cache: {home} (writable, {n_acc} accounts)")
    except Exception as exc:
        print(f"cache: {home} NOT WRITABLE ({exc})")
        ok = False
    handle = store.normalize_account(args.probe)
    t0 = time.time()
    try:
        _, entries = fetch.list_account(handle, limit=1)
        print(f"network probe: listed @{handle} ok ({len(entries)} entry, {time.time() - t0:.1f}s)")
    except Exception as exc:
        print(f"network probe: FAILED for @{handle}: {fetch.clean_error(exc)}")
        print("hint: TikTok breaks extractors regularly — upgrading yt-dlp usually fixes this "
              "(see README troubleshooting)")
        ok = False
    print(f"verdict: {'ok' if ok else 'problems found'}")
    return 0 if ok else 2


def cmd_cost(args) -> int:
    from ttgrep import cost

    report = cost.collect()
    if args.json:
        out = {**report, "priced": cost.priced(report)} if report["total"]["videos"] else report
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(cost.format_report(report, per_account=args.per_account))
    return 0


def cmd_mcp(args) -> int:
    from ttgrep import mcp_server  # lazy: keep base CLI startup light

    return mcp_server.serve()


# --- entry point -------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ttgrep",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Retrieve TikTok account content: listings, transcripts, transcript search.\n"
                    "Everything is cached in ~/.ttgrep ($TTGREP_HOME); nothing is fetched twice.",
        epilog="typical flow:\n"
               "  ttgrep sync @handle --limit 50   # slow once; resumable; incremental after\n"
               "  ttgrep videos @handle            # instant, from cache\n"
               "  ttgrep search @handle \"topic\"    # instant, from cache\n"
               "  ttgrep transcript <url-or-id>    # cached, or fetched on demand\n"
               "  ttgrep cost                      # what the cache would have cost via APIs\n"
               "exit codes: 0 ok, 1 no search matches, 2 error",
    )
    p.add_argument("--version", action="version", version=f"ttgrep {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sync", help="fetch/refresh an account's listing + missing transcripts (slow, resumable)")
    s.add_argument("account", help="@handle, handle, or profile URL")
    s.add_argument("--limit", type=int, metavar="N",
                   help="only cover the N most recent posts (cheap incremental refresh)")
    s.add_argument("--sleep", type=float, default=1.0, metavar="SECS",
                   help="base pause between video fetches (default 1.0, plus jitter)")
    s.add_argument("--retry-errors", action="store_true",
                   help="also re-attempt videos that previously failed")
    s.add_argument("--listing-only", action="store_true",
                   help="refresh the post listing but fetch no transcripts")
    s.add_argument("--no-listing", action="store_true",
                   help="skip the listing refresh; only fill in missing transcripts")
    s.add_argument("--no-asr", action="store_true",
                   help="don't transcribe captionless videos locally")
    s.add_argument("--asr-model", default=asr.DEFAULT_MODEL, metavar="MODEL",
                   help=f"whisper model for captionless videos: {', '.join(asr.MODELS)}, "
                        f"or a HF repo id (default {asr.DEFAULT_MODEL})")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("videos", help="list an account's posts (TSV; from cache)")
    s.add_argument("account")
    s.add_argument("--limit", type=int, metavar="N", help="show only the N most recent")
    s.add_argument("--since", metavar="YYYY-MM-DD", help="only posts on/after this date")
    s.add_argument("--full", action="store_true", help="don't truncate titles")
    s.add_argument("--json", action="store_true", help="one JSON object per line")
    s.set_defaults(func=cmd_videos)

    s = sub.add_parser("transcript", help="print a video's transcript (cached, else fetched on demand)")
    s.add_argument("video", help="video URL or bare numeric id")
    s.add_argument("--timestamps", action="store_true", help="one [m:ss] line per caption cue")
    s.add_argument("--lang", metavar="CODE",
                   help="caption language (TikTok codes like eng-US; prefixes like 'en' work)")
    s.add_argument("--json", action="store_true", help="full record incl. all language tracks")
    s.add_argument("--no-asr", action="store_true",
                   help="don't transcribe locally when the video has no captions")
    s.add_argument("--asr-model", default=asr.DEFAULT_MODEL, metavar="MODEL",
                   help=f"whisper model for the no-caption fallback (default {asr.DEFAULT_MODEL})")
    s.set_defaults(func=cmd_transcript)

    s = sub.add_parser("search", help="find which videos mention something (searches cached transcripts + titles)")
    s.add_argument("account", nargs="?", help="@handle (omit with --all)")
    s.add_argument("query", nargs="?", help="case-insensitive substring, or a pattern with --regex")
    s.add_argument("--all", action="store_true", help="search every cached account")
    s.add_argument("--regex", action="store_true", help="treat query as a regex (e.g. 'solar|renewable')")
    s.add_argument("--lang", metavar="CODE", help="search this caption language where available")
    s.add_argument("--max-hits", type=int, default=8, metavar="N",
                   help="max quoted matches per video (default 8)")
    s.add_argument("--json", action="store_true", help="summary line + one JSON object per match")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("status", help="cache overview (all accounts, or one in detail)")
    s.add_argument("account", nargs="?")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("doctor", help="self-check: versions, cache, live TikTok probe")
    s.add_argument("--probe", default="tiktok", metavar="ACCOUNT",
                   help="account used for the live probe (default: @tiktok)")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("cost", help="the invoice you didn't pay: price the local cache at published API rates")
    s.add_argument("--per-account", action="store_true",
                   help="include per-account lines (shows account names)")
    s.add_argument("--json", action="store_true", help="raw numbers and rates")
    s.set_defaults(func=cmd_cost)

    s = sub.add_parser("mcp", help="serve the same commands as MCP tools over stdio")
    s.set_defaults(func=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        sys.exit(args.func(args))
    except BrokenPipeError:
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
