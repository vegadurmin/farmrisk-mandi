#!/usr/bin/env python3
"""
Pull daily APMC mandi prices from data.gov.in into static JSON files.

Resource: 9ef84268-d588-465a-a308-a864a43d0070
          "Current Daily Price of Various Commodities from Various Markets (Mandi)"

The resource is a snapshot with no history, so this script accumulates one.
Each run merges today's snapshot into a per-state file and drops anything
older than WINDOW_DAYS.

Usage:
    DATA_GOV_API_KEY=xxxx python3 fetch_mandi_prices.py
    DATA_GOV_API_KEY=xxxx STATES="Gujarat,Rajasthan" python3 fetch_mandi_prices.py

Writes:
    data/index.json            catalogue of states, dates, record counts
    data/prices-<state>.json   rolling window of records for that state
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

RESOURCE = "9ef84268-d588-465a-a308-a864a43d0070"
BASE = "https://api.data.gov.in/resource/" + RESOURCE
OUT_DIR = os.environ.get("OUT_DIR", "data")
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "15"))
PAGE = 1000
MAX_PAGES = 40
TIMEOUT = 60
RETRIES = 3

API_KEY = os.environ.get("DATA_GOV_API_KEY", "").strip()
STATES = [s.strip() for s in os.environ.get("STATES", "Gujarat").split(",") if s.strip()]

# The live resource exposes lowercase keyword fields. Older mirrors of the same
# data use title-case. Try the first, fall back to the second.
FILTER_FIELDS = ("state.keyword", "State")


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def get_json(url):
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "climateadapt-mandi/1.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SystemExit("API key rejected by data.gov.in (HTTP %d)" % e.code)
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("request failed after %d tries: %s" % (RETRIES, last))


def norm_key(k):
    return re.sub(r"[^a-z]", "", k.lower())


def pick(rec, *names):
    for k, v in rec.items():
        if norm_key(k) in names:
            return v
    return ""


def to_num(v):
    try:
        return round(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def to_iso(v):
    """Arrival_Date arrives as DD/MM/YYYY. Normalise to YYYY-MM-DD."""
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def normalise(rec):
    return {
        "d": str(pick(rec, "district")).strip(),
        "m": str(pick(rec, "market")).strip(),
        "c": str(pick(rec, "commodity")).strip(),
        "v": str(pick(rec, "variety")).strip(),
        "g": str(pick(rec, "grade")).strip(),
        "t": to_iso(pick(rec, "arrivaldate", "date")),
        "a": to_num(pick(rec, "minprice", "minx0020price")),
        "b": to_num(pick(rec, "maxprice", "maxx0020price")),
        "p": to_num(pick(rec, "modalprice", "modalx0020price")),
    }


def fetch_state(state):
    for field in FILTER_FIELDS:
        rows, offset = [], 0
        for _ in range(MAX_PAGES):
            params = {
                "api-key": API_KEY,
                "format": "json",
                "limit": PAGE,
                "offset": offset,
                "filters[%s]" % field: state,
            }
            data = get_json(BASE + "?" + urllib.parse.urlencode(params))
            recs = data.get("records") or []
            rows.extend(normalise(r) for r in recs)
            if len(recs) < PAGE:
                break
            offset += PAGE
        rows = [r for r in rows if r["t"] and r["m"] and r["c"]]
        if rows:
            print("  %s: %d records via filters[%s]" % (state, len(rows), field))
            return rows
        print("  %s: 0 records via filters[%s]" % (state, field))
    return []


def key_of(r):
    return (r["d"], r["m"], r["c"], r["v"], r["g"], r["t"])


def merge(old, new):
    """New records win on conflict — the ministry revises prices during the day."""
    table = {key_of(r): r for r in old}
    for r in new:
        table[key_of(r)] = r
    return list(table.values())


def prune(rows):
    dates = sorted({r["t"] for r in rows if r["t"]})
    if not dates:
        return rows, []
    newest = datetime.strptime(dates[-1], "%Y-%m-%d")
    cutoff = (newest - timedelta(days=WINDOW_DAYS - 1)).strftime("%Y-%m-%d")
    kept = [r for r in rows if r["t"] >= cutoff]
    return kept, sorted({r["t"] for r in kept})


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("records", [])
    except (OSError, ValueError):
        return []


def write(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def main():
    if not API_KEY:
        raise SystemExit("Set DATA_GOV_API_KEY (repo secret, never in the page).")

    os.makedirs(OUT_DIR, exist_ok=True)
    now = datetime.now(timezone.utc)
    catalogue, failures = [], []

    print("Syncing %d state(s), %d-day window" % (len(STATES), WINDOW_DAYS))
    for state in STATES:
        slug = slugify(state)
        path = os.path.join(OUT_DIR, "prices-%s.json" % slug)
        try:
            fresh = fetch_state(state)
        except Exception as e:  # noqa: BLE001
            print("  %s: FAILED — %s" % (state, e), file=sys.stderr)
            failures.append(state)
            fresh = []

        existing = load(path)
        if not fresh and not existing:
            continue

        rows, dates = prune(merge(existing, fresh))
        rows.sort(key=lambda r: (r["t"], r["d"], r["m"], r["c"]), reverse=True)

        write(path, {
            "state": state,
            "resource": RESOURCE,
            "synced": now.isoformat(timespec="seconds"),
            "window_days": WINDOW_DAYS,
            "dates": dates,
            "records": rows,
        })
        catalogue.append({
            "state": state,
            "slug": slug,
            "file": "prices-%s.json" % slug,
            "records": len(rows),
            "latest": dates[-1] if dates else None,
            "dates": dates,
            "added_this_run": len(fresh),
        })
        print("  %s: %d stored, latest %s" % (state, len(rows), dates[-1] if dates else "none"))

    write(os.path.join(OUT_DIR, "index.json"), {
        "generated": now.isoformat(timespec="seconds"),
        "resource": RESOURCE,
        "source": "AGMARKNET / Directorate of Marketing & Inspection via data.gov.in",
        "window_days": WINDOW_DAYS,
        "failed": failures,
        "states": sorted(catalogue, key=lambda c: c["state"]),
    })
    print("Wrote %s/index.json" % OUT_DIR)

    # A run where every state failed should fail the job, so the cron alerts you.
    if failures and len(failures) == len(STATES):
        raise SystemExit("every state failed this run")


if __name__ == "__main__":
    main()
