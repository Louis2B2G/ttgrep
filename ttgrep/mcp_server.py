"""MCP server: the same agent-first commands, served over stdio.

Run with `ttgrep mcp`, or register in an MCP client config:

    {"command": "ttgrep", "args": ["mcp"]}

Thin by design: every tool routes through the CLI's own command functions and
returns their stdout verbatim, so MCP callers see byte-identical output to
terminal callers — one format to learn, one code path to maintain.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

from ttgrep import __version__


def _run(argv: list[str]) -> str:
    from ttgrep import cli

    out, err = io.StringIO(), io.StringIO()
    rc = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            args = cli._build_parser().parse_args(argv)
            rc = args.func(args)
        except SystemExit as exc:  # argparse rejected the arguments
            rc = int(exc.code or 0)
        except Exception as exc:  # surface as tool output, not a dead server
            print(f"error: {exc}", file=err)
            rc = 2
    text = out.getvalue()
    notes = err.getvalue().strip()
    if notes:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"[notes]\n{notes}\n"
    if rc == 1:
        text += "\n(no matches)"
    elif rc:
        text += f"\n(exit code {rc})"
    return text.strip() or "(no output)"


def serve() -> int:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="ttgrep",
        version=__version__,
        instructions=(
            "Retrieve TikTok account content: post listings, transcripts, and "
            "transcript search. Everything is cached on disk and never fetched "
            "twice. Typical flow: tiktok_sync an account once (slow — use limit), "
            "then tiktok_search / tiktok_videos / tiktok_transcript are instant. "
            "A missing transcript means the video has no captions and no "
            "detectable speech — that is an answer, not an error."
        ),
    )

    @server.tool(description=(
        "List a TikTok account's posts (TSV, newest first): id, date, duration, "
        "views, likes, comments, transcript status, title. If the account was "
        "never synced, fetches the listing first (fast; transcripts stay unfetched)."))
    def tiktok_videos(account: str, limit: int | None = None, since: str | None = None) -> str:
        argv = ["videos", account]
        if limit:
            argv += ["--limit", str(limit)]
        if since:
            argv += ["--since", since]
        return _run(argv)

    @server.tool(description=(
        "Search cached transcripts AND titles of one account (or every synced "
        "account if account is omitted) for a case-insensitive substring, or a "
        "pattern with regex=true. The first line reports coverage (how many "
        "transcripts were searchable); matches are quoted with [m:ss] timestamps."))
    def tiktok_search(query: str, account: str | None = None, regex: bool = False,
                      lang: str | None = None) -> str:
        argv = ["search"] + ([account, query] if account else ["--all", query])
        if regex:
            argv += ["--regex"]
        if lang:
            argv += ["--lang", lang]
        return _run(argv)

    @server.tool(description=(
        "Full transcript of one video (URL or bare numeric id). Cached videos "
        "return instantly; unknown ones are fetched on demand, and videos "
        "without captions are transcribed locally with whisper (one-time "
        "~5-30s). timestamps=true prefixes each caption cue with [m:ss]."))
    def tiktok_transcript(video: str, timestamps: bool = False, lang: str | None = None) -> str:
        argv = ["transcript", video]
        if timestamps:
            argv += ["--timestamps"]
        if lang:
            argv += ["--lang", lang]
        return _run(argv)

    @server.tool(description=(
        "Fetch/refresh an account: post listing, TikTok captions, and local "
        "whisper transcription for captionless videos. SLOW the first time "
        "(~2-3s per video — pass limit to cover only the N most recent). "
        "Resumable and incremental: re-running only fetches new posts."))
    def tiktok_sync(account: str, limit: int | None = None) -> str:
        argv = ["sync", account]
        if limit:
            argv += ["--limit", str(limit)]
        return _run(argv)

    @server.tool(description=(
        "Cache overview: which accounts are synced, transcript coverage per "
        "account (ok/asr/none/pending/error), and caption languages when a "
        "specific account is given."))
    def tiktok_status(account: str | None = None) -> str:
        return _run(["status"] + ([account] if account else []))

    @server.tool(description=(
        "The invoice you didn't pay: totals for the local cache (videos, "
        "transcripts, hours of speech) priced at published API rates, versus "
        "the actual bill: $0.00 (local whisper + disk cache)."))
    def tiktok_cost() -> str:
        return _run(["cost"])

    server.run("stdio")
    return 0
