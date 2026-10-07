"""Live market notes for pitches and the morning note.

Sources (public, free; swap for the firm's own market feed when available):
  * NSE index feed (allIndices): Nifty 50, Bank Nifty, sectors, Midcap / Smallcap, India VIX, breadth
  * Yahoo Finance chart API: Sensex, USD/INR, gold, crude (and the indices if NSE is unreachable)
  * Economic Times markets RSS + Google News: macro / market-wide headlines only

Notes are facts for context. Headlines that read like stock tips or picks are dropped, because GMs must not
pass on recommendations.
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import requests

from .context import Ctx, iso, parse_dt
from .db import get_setting, set_setting

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept": "application/json, text/xml, */*"}
NSE_URL = "https://www.nseindia.com/api/allIndices"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
FEEDS = [
    ("ET Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Google News", "https://news.google.com/rss/search?q=(Nifty+OR+Sensex+OR+RBI+OR+IPO)+when:1d&hl=en-IN&gl=IN&ceid=IN:en"),
]
SECTORS = ["NIFTY IT", "NIFTY AUTO", "NIFTY PHARMA", "NIFTY METAL", "NIFTY FMCG", "NIFTY REALTY", "NIFTY PSU BANK",
           "NIFTY MEDIA", "NIFTY ENERGY", "NIFTY FIN SERVICE"]
YAHOO_INDEX = {"NIFTY 50": "^NSEI", "NIFTY BANK": "^NSEBANK", "INDIA VIX": "^INDIAVIX", "NIFTY IT": "^CNXIT",
               "NIFTY AUTO": "^CNXAUTO", "NIFTY PHARMA": "^CNXPHARMA", "NIFTY METAL": "^CNXMETAL", "NIFTY FMCG": "^CNXFMCG",
               "NIFTY REALTY": "^CNXREALTY", "NIFTY PSU BANK": "^CNXPSUBANK", "NIFTY MEDIA": "^CNXMEDIA", "NIFTY ENERGY": "^CNXENERGY"}

# Keep market-wide / macro news; drop anything that reads like a recommendation.
MACRO = re.compile(r"\b(rbi|repo|rate (cut|hike)|inflation|cpi|wpi|gdp|fii|fpi|dii|ipo|listing|sebi|budget|gst|rupee|"
                   r"crude|gold|fed|monsoon|nifty|sensex|market|results?|earnings|policy|tariff|bond|yield)\b", re.I)
TIPS = re.compile(r"\b(picks?|buy|sell|target|stocks? to|top \d+|\d+ stocks|multibagger|should you|rall(y|ied) up to|"
                  r"upside|brokerages? (see|bet)|recommend|trading ideas?|tips?|technical(ly)? (pick|call)|jackpot)\b", re.I)


def _pct(x) -> str:
    return f"{x:+.1f}%".replace("+-", "−").replace("-", "−")


def fetch_nse() -> dict:
    r = requests.get(NSE_URL, headers=UA, timeout=15)
    r.raise_for_status()
    data = r.json()
    rows = {d["index"]: d for d in data["data"]}
    def count(x):  # NSE sends breadth counts as text
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    return {"as_of": data.get("timestamp"), "rows": {k: {"last": float(v["last"]), "pct": float(v["percentChange"]),
                                                         "adv": count(v.get("advances")), "dec": count(v.get("declines"))}
                                                     for k, v in rows.items()}}


def fetch_yahoo(sym: str) -> dict | None:
    """Latest price and change vs the previous trading day's close.
    (meta.chartPreviousClose is the close before the whole chart range, not yesterday, so it is not used.)"""
    try:
        r = requests.get(YAHOO_URL.format(sym=sym), headers=UA, timeout=15)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        meta = res["meta"]
        last = float(meta["regularMarketPrice"])
        market_time = datetime.fromtimestamp(meta["regularMarketTime"])
        bars = [(datetime.fromtimestamp(t).date(), c) for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"])
                if c is not None]
        prev = next((c for d, c in reversed(bars) if d < market_time.date()), None)
        if prev is None:
            return None
        return {"last": last, "pct": (last - prev) / prev * 100, "prev": prev, "time": market_time}
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as e:
        log.warning("Yahoo %s failed: %s", sym, e)
        return None


def fetch_headlines(ctx: Ctx, limit: int = 3) -> list[dict]:
    seen, out = set(), []
    cutoff = ctx.now() - timedelta(hours=24)
    for source, url in FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except (requests.RequestException, ET.ParseError) as e:
            log.warning("Headlines from %s failed: %s", source, e)
            continue
        for item in root.iter("item"):
            title = re.sub(r"\s+-\s+[^-]+$", "", (item.findtext("title") or "").strip()) if source == "Google News" else (item.findtext("title") or "").strip()
            try:
                when = parsedate_to_datetime(item.findtext("pubDate") or "")
            except (TypeError, ValueError):
                when = None
            if "|" in title:
                title = title.split("|")[-1].strip()
            title = re.sub(r"^(live( updates)?|breaking)\s*:\s*", "", title, flags=re.I).strip()
            key = title.lower()[:60]
            if not title or key in seen or (when and when < cutoff) or not MACRO.search(title) or TIPS.search(title):
                continue
            seen.add(key)
            out.append({"text": title, "source": source, "time": iso(when.astimezone(ctx.tz)) if when else None})
            if len(out) >= limit:
                return out
    return out


BOUNDS = {"index_pct": 10.0, "vix": (5, 90), "usdinr": (60, 150), "gold": (1000, 10000), "crude": (20, 250)}


def sanity_check(ctx: Ctx, idx: dict, as_of: str | None, yahoo: dict) -> list[dict]:
    """Cross-check sources against each other and against plausible ranges. Returns one row per check."""
    checks = []

    def add(name, ok, detail, drop=None):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "drop": drop if not ok else None})

    # 1. NSE vs Yahoo on the same indices: level within 0.5 %, daily change within 0.3 points
    #    (VIX ticks every few seconds, so the two snapshots differ more: 2 % / 2 points)
    for name, sym, lvl_tol, chg_tol in [("NIFTY 50", "^NSEI", 0.5, 0.3), ("NIFTY BANK", "^NSEBANK", 0.5, 0.3),
                                        ("INDIA VIX", "^INDIAVIX", 2.0, 2.0), ("NIFTY IT", "^CNXIT", 0.5, 0.3)]:
        a, b = idx.get(name), yahoo.get(sym)
        if a and b:
            lvl = abs(a["last"] - b["last"]) / a["last"] * 100
            chg = abs(a["pct"] - b["pct"])
            add(f"{name}: NSE vs Yahoo", lvl <= lvl_tol and chg <= chg_tol,
                f"NSE {a['last']:,.2f} ({a['pct']:+.2f}%) vs Yahoo {b['last']:,.2f} ({b['pct']:+.2f}%)", drop=name)
        else:
            add(f"{name}: NSE vs Yahoo", True, "only one source available, not cross-checked")
    # 2. Sensex and Nifty move together (daily changes within 1 point)
    sx, nf = yahoo.get("^BSESN"), idx.get("NIFTY 50")
    if sx and nf:
        add("Sensex moves with Nifty", abs(sx["pct"] - nf["pct"]) <= 1.0,
            f"Sensex {sx['pct']:+.2f}% vs Nifty {nf['pct']:+.2f}%", drop="SENSEX")
    # 3. Plausible ranges
    for name, v in idx.items():
        if name != "INDIA VIX" and abs(v["pct"]) > BOUNDS["index_pct"]:
            add(f"{name}: daily move under {BOUNDS['index_pct']:.0f}%", False, f"{v['pct']:+.2f}%", drop=name)
    add("Index moves within ±10%", not any(abs(v["pct"]) > BOUNDS["index_pct"] for k, v in idx.items() if k != "INDIA VIX"),
        f"largest move {max((abs(v['pct']) for k, v in idx.items() if k != 'INDIA VIX'), default=0):.2f}%")
    for label, sym, key in [("India VIX range", None, "vix"), ("USD/INR range", "INR=X", "usdinr"),
                            ("Gold range ($/oz)", "GC=F", "gold"), ("Crude range ($/bbl)", "CL=F", "crude")]:
        v = idx.get("INDIA VIX", {}).get("last") if sym is None else (yahoo.get(sym) or {}).get("last")
        if v is not None:
            lo, hi = BOUNDS[key]
            add(label, lo <= v <= hi, f"{v:,.2f} (expected {lo}–{hi})", drop=key)
    # 4. Breadth agrees with the index direction (warning only)
    if nf and nf.get("adv") is not None:
        agrees = (nf["pct"] >= 0) == (nf["adv"] >= nf["dec"]) or abs(nf["pct"]) < 0.2
        add("Nifty breadth matches its direction", agrees, f"{nf['adv']} up / {nf['dec']} down, index {nf['pct']:+.2f}%")
    # 5. Freshness: NSE data is from the last trading session; Yahoo quotes are under 4 days old
    if as_of:
        try:
            nse_day = datetime.strptime(as_of, "%d-%b-%Y %H:%M").date()
            last_session = ctx.today() if ctx.is_working_day(ctx.today()) and ctx.now().hour >= 9 else None
            if last_session is None:
                last_session = ctx.today() - timedelta(days=1)
                while not ctx.is_working_day(last_session):
                    last_session -= timedelta(days=1)
            add("NSE data is from the latest session", nse_day >= last_session - timedelta(days=3),
                f"NSE timestamp {as_of}")
        except ValueError:
            add("NSE data is from the latest session", False, f"unreadable timestamp {as_of!r}")
    stale = [s for s, v in yahoo.items() if v and datetime.now() - v["time"] > timedelta(days=4)]
    add("Yahoo quotes are fresh (< 4 days)", not stale, "stale: " + ", ".join(stale) if stale else "all fresh",
        drop=",".join(stale) or None)
    return checks


def build_notes(ctx: Ctx) -> dict:
    """Fetch every source, sanity-check it, and turn it into short factual lines tagged by segment."""
    notes, sources = [], []
    try:
        nse = fetch_nse()
        idx, as_of = nse["rows"], nse["as_of"]
        sources.append("NSE")
    except (requests.RequestException, ValueError, KeyError) as e:  # cloud IPs are sometimes blocked by NSE
        log.warning("NSE feed failed (%s); using Yahoo for indices", e)
        idx = {name: v for name, sym in YAHOO_INDEX.items() if (v := fetch_yahoo(sym))}
        as_of = None
        sources.append("Yahoo Finance")
    yahoo = {sym: fetch_yahoo(sym) for sym in ["^BSESN", "^NSEI", "^NSEBANK", "^INDIAVIX", "^CNXIT", "INR=X", "GC=F", "CL=F"]}
    checks = sanity_check(ctx, idx, as_of, yahoo)
    dropped = {d for c in checks if c["drop"] for d in c["drop"].split(",")}
    idx = {k: v for k, v in idx.items() if k not in dropped}
    sensex = None if "SENSEX" in dropped else yahoo["^BSESN"]

    now = ctx.now()
    closed = now.hour > 15 or (now.hour == 15 and now.minute >= 30) or not ctx.is_working_day(now.date())
    verb = "closed at" if closed else "is at"
    if "NIFTY 50" in idx:
        n = idx["NIFTY 50"]
        line = f"Nifty 50 {verb} {n['last']:,.0f} ({_pct(n['pct'])})"
        if sensex:
            line += f"; Sensex {sensex['last']:,.0f} ({_pct(sensex['pct'])})"
        if n.get("adv") is not None and n.get("dec") is not None:
            line += f". Nifty breadth: {n['adv']} up, {n['dec']} down"
        notes.append({"text": line, "tags": ["equity"]})
    sect = sorted(((k.replace("NIFTY ", "").title().replace("Psu", "PSU").replace("It", "IT").replace("Fmcg", "FMCG"), v["pct"])
                   for k, v in idx.items() if k in SECTORS), key=lambda x: x[1])
    if sect:
        notes.append({"text": f"Sectors: {sect[-1][0]} led ({_pct(sect[-1][1])}), {sect[0][0]} lagged ({_pct(sect[0][1])})",
                      "tags": ["equity"]})
    if "NIFTY BANK" in idx:
        b = idx["NIFTY BANK"]
        notes.append({"text": f"Bank Nifty {b['last']:,.0f} ({_pct(b['pct'])})", "tags": ["fando", "equity"]})
    if "INDIA VIX" in idx:
        v = idx["INDIA VIX"]["last"]
        mood = "low volatility" if v < 13 else "moderate volatility" if v < 18 else "elevated volatility"
        notes.append({"text": f"India VIX {v:.1f} ({_pct(idx['INDIA VIX']['pct'])}): {mood}. For F&O, explain margin and risk first",
                      "tags": ["fando"]})
    if "NIFTY MIDCAP 100" in idx and "NIFTY SMALLCAP 100" in idx:
        notes.append({"text": f"Midcap 100 {_pct(idx['NIFTY MIDCAP 100']['pct'])}, Smallcap 100 {_pct(idx['NIFTY SMALLCAP 100']['pct'])}",
                      "tags": ["equity"]})
    fx = None if {"usdinr", "INR=X"} & dropped else yahoo["INR=X"]
    gold = None if {"gold", "GC=F"} & dropped else yahoo["GC=F"]
    crude = None if {"crude", "CL=F"} & dropped else yahoo["CL=F"]
    comm = [f"gold ${gold['last']:,.0f}/oz ({_pct(gold['pct'])})" if gold else "",
            f"crude ${crude['last']:,.1f}/bbl ({_pct(crude['pct'])})" if crude else ""]
    comm = [c for c in comm if c]
    if comm:
        notes.append({"text": "Global commodities: " + ", ".join(comm), "tags": ["commodity"]})
    if fx:
        notes.append({"text": f"Rupee at ₹{fx['last']:.2f} per US$ ({_pct(-fx['pct'])} for the rupee)", "tags": ["commodity", "equity"]})
    if sensex or fx or gold:
        sources.append("Yahoo Finance")
    heads = fetch_headlines(ctx)
    for h in heads:
        notes.append({"text": f"In the news: {h['text']} ({h['source']})", "tags": ["news"]})
    if heads:
        sources.append("ET / Google News")
    return {"updated": iso(now), "as_of": as_of, "sources": sorted(set(sources)), "notes": notes, "checks": checks,
            "headlines_checked": True}


def refresh(ctx: Ctx) -> dict:
    data = build_notes(ctx)
    if data["notes"]:  # keep the last good set if every source failed
        set_setting(ctx.conn, "market_auto", json.dumps(data, ensure_ascii=False), iso(ctx.now()))
    return {"notes": len(data["notes"]), "sources": data["sources"],
            "checks_failed": [c["check"] for c in data["checks"] if not c["ok"]]}


def live_notes(ctx: Ctx) -> dict:
    raw = get_setting(ctx.conn, "market_auto")
    return json.loads(raw) if raw else {"notes": [], "updated": None, "sources": []}


def due_for_refresh(ctx: Ctx) -> bool:
    every = int((ctx.cfg.get("market") or {}).get("refresh_minutes", 15))
    last = parse_dt(live_notes(ctx).get("updated"), ctx.tz)
    return last is None or ctx.now() - last >= timedelta(minutes=every)


def note_for(ctx: Ctx, segments: list[str]) -> str | None:
    """The most relevant live note for a customer: their research segment first, then the headline index line."""
    notes = live_notes(ctx)["notes"]
    wanted = {"F&O": "fando", "Commodity": "commodity", "Equity": "equity"}
    for seg in segments:
        tag = wanted.get(seg)
        hit = next((n["text"] for n in notes if tag in n["tags"]), None)
        if hit:
            return hit
    return notes[0]["text"] if notes else None
