# ttgrep

grep for TikTok.

TikTok has no public API, no transcripts, and no text search. ttgrep fixes that. It downloads everything an account has posted, turns every video into text, and lets you search all of it. Captions come from TikTok when they exist. When they don't, Whisper runs on your machine. Everything is saved to disk and never fetched twice. Nothing costs money.

Here is an AI agent using it. Real recording: one question in, receipts out ([full-speed video](docs/agent-demo.mp4), [second example](docs/agent-demo-2.mp4)):

![An AI agent answers "what's the worst product MKBHD talked about?" using ttgrep](docs/agent-demo.gif)

It answers three questions:

1. **What has this account posted?** Use `ttgrep videos`.
2. **What was said in this video?** Use `ttgrep transcript`.
3. **In which videos do they talk about X?** Use `ttgrep search`.

It only retrieves. Deciding what the words mean is your job, or your AI agent's. ttgrep is built for agents: the output is plain text that reads well in a context window, it ships an MCP server (`ttgrep mcp`), and there is a ready-made CLAUDE.md snippet at the bottom of this page so your agent knows when to reach for it.

## Install

```bash
uv tool install ttgrep
```

`pipx install ttgrep` works too, and so does installing from source with `uv tool install git+https://github.com/Louis2B2G/ttgrep.git`. You get a `ttgrep` command with everything bundled: a current yt-dlp with `curl_cffi` (TikTok blocks plain clients) and a local Whisper. On Apple Silicon the Whisper backend is `mlx-whisper`, which also needs `ffmpeg` on your PATH (`brew install ffmpeg`). Everywhere else it is `faster-whisper`, which needs nothing extra. Run `ttgrep doctor` to check all of it.

## How to use it

One slow command, then everything is instant:

```bash
ttgrep sync @handle --limit 50    # fetch listing + transcripts (slow once, resumable)
ttgrep videos @handle             # instant, from cache
ttgrep search @handle "topic"     # instant, from cache
ttgrep transcript <url-or-id>     # cached, or fetched on demand
ttgrep cost                       # what all of this would have cost elsewhere
```

`sync` is the only command that does real network work: one request for the post list, then one page per video (about 2-3 seconds each, politely spaced). You can interrupt it any time. It saves as it goes and picks up where it left off. Run it again later and it only fetches new posts. `--limit N` covers just the N most recent posts; leave it off for full history.

### `sync ACCOUNT`

```
$ ttgrep sync @hankgreen1 --limit 40
listing @hankgreen1 (top 40) ...              # progress goes to stderr
fetching captions for 25 videos ...           # 15 of the 40 were already cached
[1/25] 7653172464536538382 ok eng-US
[2/25] 7652512813184781581 ok eng-US
[15/25] 7626519681100188958 none              # no captions, whisper handles it below
...
transcribing 1 captionless videos locally (whisper-small on mlx; first ever run downloads the model)
[1/1] 7626519681100188958 asr en (3.6s)
account: hankgreen1                           # summary goes to stdout
videos: 40
listing: partial
transcripts_ok: 39
transcripts_asr: 1
no_captions: 0
pending: 0
errors: 0
fetched_this_run: 25
transcribed_this_run: 1
```

Videos without TikTok captions get their audio transcribed on your machine with Whisper (the model downloads once, about 500MB for `small`). If Whisper finds no speech at all, like music-only clips, the video is marked and never tried again. That is an answer, not an error.

Flags: `--limit N` (N most recent only), `--sleep SECS` (pause between fetches, default 1.0), `--retry-errors` (retry failed videos), `--listing-only` (post list only), `--no-listing` (skip the list, just fill missing transcripts), `--no-asr` (skip Whisper), `--asr-model MODEL` (default `small`).

### `videos ACCOUNT`

What they posted. TSV, newest first. The title on TikTok is the post description, hashtags and all.

```
$ ttgrep videos @hankgreen1 --limit 3
id	date	dur	views	likes	comments	transcript	title
7678870202494340383	2026-08-28	173	24700	2182	53	ok	Longer video getting into the very very weird details of how the immune system ...
7678484247363030302	2026-08-26	58	53300	4438	90	ok	#stitch with @kyro_frenchbulldogs #greenscreen
7676649513565703437	2026-08-22	67	15700	337	22	ok	How do you get a voter’s attention? Listen to the full conversation wherever y...
```

The `transcript` column: `ok` means TikTok captions, `asr` means Whisper, `none` means no captions and no speech, `pending` means not fetched yet, `error` means the fetch failed, `photo` means an image post (no audio exists). Flags: `--limit`, `--since YYYY-MM-DD`, `--full` (don't shorten titles), `--json`.

### `transcript VIDEO`

What was said in one video. Takes a URL or a bare id. Cached videos print instantly. Unknown videos are fetched on the spot, even from accounts you never synced.

```
$ ttgrep transcript 7678484247363030302
id: 7678484247363030302
account: hankgreen1
date: 2026-08-26
duration: 58
views: 53300
url: https://www.tiktok.com/@hankgreen1/video/7678484247363030302
langs: eng-US
lang: eng-US
title: #stitch with @kyro_frenchbulldogs #greenscreen
---
The butterflies do this thing called puddling, where they drink water specifically from muddy puddles, because their food source, nectar, doesn't contain a lot ...
```

Flags: `--timestamps` (one `[m:ss]` line per caption), `--lang CODE` (`en` works as a prefix for `eng-US`), `--json` (every language track, with timing), `--no-asr`, `--asr-model`. No captions? It transcribes the audio right there, once, in 5 to 30 seconds. Only a video with no speech at all prints `transcript: none`, and it exits 0, because that is an answer.

### `search [ACCOUNT] QUERY`

Which videos say X. Searches every cached transcript and title. Case doesn't matter. `--regex` for patterns like `'solar|renewable'`. `--all` searches every account you have cached.

```
$ ttgrep search @hankgreen1 "immune"
# query='immune' accounts=1 posts=40 transcripts_searched=40 matched=2 skipped: no_captions=0 pending=0 error=0
== 7678870202494340383  2026-08-28  @hankgreen1  views=24700  lang=eng-US
   Longer video getting into the very very weird details of how the immune system ...
   [0:42] like, wake your immune system up to the fact that you might get this cancer. And that would decrease your risk of getting that cancer
   [2:05] and then training with a vaccine, the immune system, to go after it. Like, be on the lookout for those specific weird proteins.
   ...
```

The first line always tells you how much was searchable, so you know what a "no" means. Matches are quoted with timestamps. Phrases that cross caption boundaries still match. Exit code 1 means "searched fine, found nothing". Check the `pending` count before you claim someone never said something.

### `cost`

What your cache would have cost if you had bought it.

![ttgrep cost](docs/demo.gif)

```
$ ttgrep cost
your cache: 8 accounts · 2,103 videos · 1,892 transcripts · 35.5 hours of speech
how: TikTok captions (1,557) + whisper on this machine (335)
the same thing, bought (published 2026 prices):
  Supadata "TikTok Transcript API" ....... $17/month (Pro plan), metered
  OpenAI transcription, 35.5 h of audio ... $12.77, plus you build the rest
  social listening tools ................. can't search speech at all
your bill .............................. $0.00
your next 10,000 searches .............. $0.00
```

The last line is the point. When every question costs money, an agent learns to stop asking. When questions are free, it can grep everything, twice.

Prices, checked August 2026: [Supadata](https://supadata.ai/pricing) sells TikTok transcripts at 1 credit each ($17/month for 3,000, $47/month for 30,000). [OpenAI transcription](https://platform.openai.com/pricing) is $0.006 per minute. [ScrapeCreators](https://scrapecreators.com/tiktok-transcript-api) is similar ($10 per 5,000). The numbers live in `ttgrep/cost.py`; fix them when they drift. `--per-account` shows a per-account breakdown. `--json` gives raw numbers.

### `status`, `doctor`, `mcp`

`ttgrep status` shows what you have cached. `ttgrep doctor` checks your install and does one live probe against TikTok. Run it first when something breaks.

`ttgrep mcp` runs the whole thing as an MCP server over stdio. Same cache, same output, six tools (`tiktok_sync`, `tiktok_videos`, `tiktok_transcript`, `tiktok_search`, `tiktok_status`, `tiktok_cost`):

```json
{ "mcpServers": { "ttgrep": { "command": "ttgrep", "args": ["mcp"] } } }
```

## The test

The acceptance test for this tool was not a test suite. It was handing a fresh AI agent one sentence ("ttgrep is installed, figure it out with --help") plus a question about a synced account. No docs, no hints. The agent found the commands, searched, quoted timestamped evidence, ranked its findings, and correctly refused to guess about one music-only video because the coverage line told it there was nothing to search. If your agent can read `--help`, it can use this.

That test shaped the output: coverage before results, "no captions" kept separate from "not fetched yet", exit 1 for "found nothing", and stderr kept out of the data.

## Output rules

- stdout is data. stderr is progress and hints. Pipe stdout safely.
- Exit codes: 0 ok, 1 search found nothing, 2 real error.
- `--json` everywhere it matters. Default output is compact text, to save tokens.
- Dates are UTC `YYYY-MM-DD`. Durations are seconds. Counts are plain integers.

## Where things live

```
~/.ttgrep/                                # change with $TTGREP_HOME
  accounts/<handle>/index.json            # post list + per-video status
  accounts/<handle>/transcripts/<id>.json # all language tracks, with timing
```

Plain JSON. You can grep it directly. Writes are atomic, so an interrupted sync never corrupts anything. `ok` and `none` are final and never re-fetched. Videos deleted from a profile stay in your cache, marked `listed: false`. Knowledge is not deleted. Full reset for one account: `rm -r ~/.ttgrep/accounts/<handle>`.

## Languages

TikTok uses its own caption codes: `eng-US` and `fra-FR`, not `en` and `fr`. Some videos have several tracks (the original plus translations). `sync` stores all of them; caption files are tiny. When you read, ttgrep picks the account's main language (the one on most of their videos). Override with `--lang` anywhere. Whisper tracks use short codes (`en`, `fr`) and carry a `source` field, so you can always tell captions from local transcription.

One honest warning: Whisper writes down whatever is audible. On a video with a song and no talking, that can be accurately transcribed song lyrics. Hallucinated noise and repeated-word loops are filtered out using Whisper's own confidence numbers, but real lyrics stay in, on purpose. Check whether an `asr` match is speech before you quote it as something the creator said.

## When it breaks

TikTok changes things. yt-dlp chases them. If fetching stops working:

1. `ttgrep doctor` tells you whether it's your install or TikTok.
2. `uv tool upgrade ttgrep` pulls the latest yt-dlp, which is the fix most of the time.
3. Many `error` statuses mid-sync usually means rate limiting. Wait, then re-run with a higher `--sleep` and `--retry-errors`.

### Notes for whoever maintains this (learned the hard way)

- yt-dlp only fills in `subtitles` when the `writesubtitles` (or `listsubtitles`) option is set. A plain `extract_info()` reports no captions for videos that have them.
- TikTok captions appear under `subtitles`, not `automatic_captions`, even though they are auto-generated.
- Fetching caption URLs yourself can get a 403. Going through yt-dlp's own subtitle download machinery works. ttgrep does the latter.
- One extractor path returns SRT data instead of VTT files. The parser accepts both.
- TikTok serves many image posts under `/video/` URLs. They fail with "No video formats found". That means "image post", not "error".
- Whisper on music produces two failure modes: hallucinated text (avg_logprob below about -3, versus above -0.9 for real speech) and repetition loops (compression_ratio over 2.4). Both are filtered in `asr.py`.
- A flat-playlist extraction of a profile returns full metadata per video. Listing an account never needs per-video fetches.
- A TikTok video id encodes its post time: `id >> 32` is unix seconds.
- `tiktok.com/@_/video/<id>` resolves for any bare video id. The handle in the URL is ignored.

## CLAUDE.md snippet

Paste this into your global `~/.claude/CLAUDE.md` so your agent knows when to use ttgrep:

```markdown
## TikTok (`ttgrep`)

When a task involves what a TikTok account posts or says (analyzing a creator,
checking if or when they covered a topic, quoting a video), use `ttgrep`. Do
not use ad-hoc yt-dlp or web scraping. Everything is cached in ~/.ttgrep;
never re-fetch what it already has.

- New account: `ttgrep sync @handle --limit 50` first (slow, 2-3s per video,
  resumable). Videos without captions get whisper-transcribed automatically.
- `ttgrep videos @handle` shows what they posted (TSV; `--json` for full records).
- `ttgrep transcript <url-or-id>` shows what was said (fetches if uncached).
  `transcript: none` means the video has no speech. That is an answer.
- `ttgrep search @handle "term"` finds which videos mention it (`--regex 'a|b'`,
  `--all` for every cached account). Exit 1 means no matches. Read the `#`
  coverage line first: if `pending` > 0, sync before claiming they never said it.
- Caption languages are TikTok codes (`eng-US`); default is the account's main
  language, `--lang en` to override. Whisper tracks on music-only videos can be
  song lyrics. Check before quoting them as the creator's words.
- If fetching fails: `ttgrep doctor`, then `uv tool upgrade ttgrep`.
- Missing? `uv tool install ttgrep`
- MCP clients: `ttgrep mcp` serves the same commands as tools over stdio.
```
