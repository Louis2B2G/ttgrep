"""The invoice you didn't pay.

Aggregates the local cache and prices it against published 2026 rates for
the closest purchasable alternatives. Reference rates only — sources are
cited in the README; edit RATES here if they drift.
"""

from __future__ import annotations

from ttgrep import store

# Published rates, checked 2026-08 (see README "cost" section for sources).
RATES = {
    # OpenAI audio transcription (whisper-1 / gpt-4o-transcribe), USD per minute.
    "transcription_per_min": 0.006,
    # Supadata (sells a "TikTok Transcript API"; 1 transcript = 1 credit).
    # Pro: $17/mo for 3,000 credits. Mega: $47/mo for 30,000.
    "supadata_pro_monthly": 17.0,
    "supadata_pro_credits": 3000,
    "supadata_mega_monthly": 47.0,
}


def collect(accounts: list[str] | None = None) -> dict:
    rows = []
    for handle in accounts if accounts is not None else store.list_accounts():
        idx = store.load_index(handle)
        if idx is None:
            continue
        row = {"account": handle, "videos": 0, "transcripts": 0,
               "captioned": 0, "whispered": 0, "speech_seconds": 0}
        for v in idx["videos"]:
            row["videos"] += 1
            status = v.get("transcript")
            if status in ("ok", "asr"):
                row["transcripts"] += 1
                row["captioned" if status == "ok" else "whispered"] += 1
                row["speech_seconds"] += v.get("duration") or 0
        rows.append(row)
    total = {k: sum(r[k] for r in rows) for k in
             ("videos", "transcripts", "captioned", "whispered", "speech_seconds")}
    total["accounts"] = len(rows)
    return {"accounts": rows, "total": total, "rates": RATES}


def priced(report: dict) -> dict:
    t = report["total"]
    minutes = t["speech_seconds"] / 60
    # Which Supadata tier this cache would need (agents re-pull, so this is
    # the floor: the plan whose monthly credits cover the transcript count).
    tier = ("Pro", RATES["supadata_pro_monthly"]) \
        if t["transcripts"] <= RATES["supadata_pro_credits"] \
        else ("Mega", RATES["supadata_mega_monthly"])
    return {
        "speech_hours": t["speech_seconds"] / 3600,
        "transcription_api_usd": minutes * RATES["transcription_per_min"],
        "supadata_tier": tier[0],
        "supadata_monthly_usd": tier[1],
    }


def _money(x: float) -> str:
    return f"${x:,.2f}"


def format_report(report: dict, per_account: bool = False) -> str:
    t = report["total"]
    if not t["videos"]:
        return "cache empty — run `ttgrep sync <account>` first, then come back for the invoice"
    p = priced(report)
    lines = [
        f"your cache: {t['accounts']} accounts · {t['videos']:,} videos · "
        f"{t['transcripts']:,} transcripts · {p['speech_hours']:.1f} hours of speech",
        f"how: TikTok captions ({t['captioned']:,}) + whisper on this machine "
        f"({t['whispered']:,})",
    ]
    if per_account:
        for r in sorted(report["accounts"], key=lambda r: -r["speech_seconds"]):
            lines.append(f"  @{r['account']}: {r['videos']:,} videos, "
                         f"{r['transcripts']:,} transcripts, {r['speech_seconds'] / 3600:.1f} h")
    lines += [
        "the same thing, bought (published 2026 prices):",
        f"  Supadata \"TikTok Transcript API\" ....... ${p['supadata_monthly_usd']:,.0f}/month"
        f" ({p['supadata_tier']} plan), metered",
        f"  OpenAI transcription, {p['speech_hours']:.1f} h of audio ... "
        f"{_money(p['transcription_api_usd'])}, plus you build the rest",
        "  social listening tools ................. can't search speech at all",
        "your bill .............................. $0.00",
        "your next 10,000 searches .............. $0.00",
    ]
    return "\n".join(lines)
