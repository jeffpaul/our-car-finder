#!/usr/bin/env python3
"""Daily used-car search: scrape, filter, track, notify, build the report, publish it.

Usage:
  run.py                 full run (scrape + notify + report + publish)
  run.py --no-publish    skip pushing the report to GitHub Pages
  run.py --no-notify     skip ntfy notifications
  run.py --report-only   rebuild the report from the database without scraping
  run.py --sources carscom,craigslist   limit to some sources (testing)
"""
import argparse
import datetime as dt
import hashlib
import io
import json
import logging
import re
import sqlite3
import sys
import time
import tomllib
import traceback
from pathlib import Path

from curl_cffi import requests
from PIL import Image

from finder import models as M
from finder.deals import Market, record_market, thresholds, update_thresholds
from finder import sources as S
from finder.geo import Geo
from finder.reliability import Reliability
from finder.notify import Notifier
from finder.publish import publish
from finder.report import build_report

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
THUMBS = DATA / "thumbs"
# Phrases that mean a private buyer can't title and register the car. These listings are dropped.
EXCLUDE_PATTERNS = re.compile(
    r"\b(?:only\s+for|for)\s+export\b|\bexport\s+only\b|\bdealers?\s+only\b|\bdealer\s+sales?\b"
    r"|\bno\s+title\b|\bwithout\s+(?:a\s+)?title\b|\bbill\s+of\s+sale\s+only\b|\bparts\s+only\b"
    r"|\b(?:lost|missing)\s+title\b|\bmexican\s+(?:title|plates)\b", re.I)

# Phrases in a listing's title or description that mean "look closer before calling it a deal".
TROUBLE_PATTERNS = [
    (r"\b(salvage|rebuilt|branded|lien|bonded)\s+title\b", "{0}"),
    (r"\b(salvage|flood(?:ed)?|hail damage|frame damage)\b", "{0}"),
    (r"\b(transmission|engine|motor|ac|a/c)\s+(?:needs|has issues|issue|problem|slipping|knock)", "{0} trouble"),
    (r"\bneeds?\s+(?:a\s+)?(?:new\s+)?(transmission|engine|motor|work|repairs?|attention|tlc)\b", "needs {0}"),
    (r"\b(check engine light|cel on|not running|doesn'?t run|won'?t start|non[- ]?runner)\b", "{0}"),
    (r"\b(mechanic(?:'?s)? special|parts only)\b", "{0}"),
]

log = logging.getLogger("finder")


def open_db():
    db = sqlite3.connect(DATA / "finder.db")
    db.execute("""CREATE TABLE IF NOT EXISTS listings (
        key TEXT PRIMARY KEY, data TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        gone_on TEXT, notified INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS source_health (
        source TEXT PRIMARY KEY, failures INTEGER DEFAULT 0, last_ok TEXT, last_error TEXT,
        last_count INTEGER, alerted INTEGER DEFAULT 0)""")
    db.execute("CREATE TABLE IF NOT EXISTS published (path TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS runs (at TEXT PRIMARY KEY, summary TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS fb_details (id TEXT PRIMARY KEY, data TEXT, fetched TEXT)")
    return db


def enrich_facebook(cfg, models, db, browser, rows):
    """Fills mileage/VIN/title status on Facebook rows from cached or freshly opened listing pages."""
    s = cfg["search"]
    fb = [r for r in rows if r["source"] == "Facebook"]
    cached = {i: json.loads(d) for i, d in db.execute("SELECT id, data FROM fb_details")}
    todo = [r["id"] for r in fb if r["id"] not in cached and M.identify(r["title"], models)
            and (not r["year"] or r["year"] >= s["min_year"])
            and r["price"] and r["price"] >= s["price_min"] - s["near_miss_margin"]]
    if todo:
        log.info("facebook: opening %d new listing pages for mileage", len(todo))
        fetched = S.facebook_details(browser, todo)
        now = dt.datetime.now().isoformat()
        db.executemany("INSERT OR REPLACE INTO fb_details VALUES (?,?,?)",
                       [(i, json.dumps(d), now) for i, d in fetched.items()])
        db.commit()
        cached.update(fetched)
    for r in fb:
        d = cached.get(r["id"])
        if d:
            r["mileage"] = r["mileage"] or d["mileage"]
            r["vin"] = r["vin"] or d["vin"]
            r["extra_text"] = S.fb_listing_text(d.get("text") or "")
            if d.get("title_status") and d["title_status"] != "clean":
                r["extra_text"] += f' {d["title_status"]} title'


def scrape(cfg, models, only, db):
    """Runs every enabled source. Returns (raw listings, {source: error or None})."""
    enabled = {k for k, v in cfg["sources"].items() if v is True}
    if only:
        enabled &= set(only)
    results, status = [], {}

    def run_one(name, fn, *args):
        try:
            rows = fn(cfg, models, *args)
            log.info("%s: %d listings", name, len(rows))
            results.extend(rows)
            status[name] = None if rows else "returned no listings"
        except Exception as e:
            log.error("%s failed: %s", name, e)
            log.debug(traceback.format_exc())
            status[name] = f"{type(e).__name__}: {e}"[:300]

    for name, fn in S.HTTP_SOURCES.items():
        if name in enabled:
            run_one(name, fn)
    browser_names = [n for n in S.BROWSER_SOURCES if n in enabled]
    if browser_names:
        try:
            with S.Browser() as b:
                for name in browser_names:
                    run_one(name, S.BROWSER_SOURCES[name], b)
                    if status[name] is not None:
                        log.info("%s: retrying in 45s", name)
                        time.sleep(45)
                        run_one(name, S.BROWSER_SOURCES[name], b)
                if "facebook" in browser_names:
                    try:
                        enrich_facebook(cfg, models, db, b, results)
                    except Exception as e:
                        log.error("facebook details failed: %s", e)
        except Exception as e:
            log.error("browser failed to start: %s", e)
            for name in browser_names:
                status.setdefault(name, f"browser failed: {e}"[:300])
    return results, status


SOURCE_KEYS = {"Cars.com": "carscom", "Craigslist": "craigslist", "CarMax": "carmax",
               "Autotrader": "autotrader", "Facebook": "facebook"}


def trouble_flags(text, flags=()):
    flags = list(flags)
    for pat, label in TROUBLE_PATTERNS:
        for m in re.finditer(pat, text.lower()):
            f = label.format(m.group(1))
            if f not in flags:
                flags.append(f)
    return flags


def normalize(raw, cfg, models, geo):
    """Filters a raw listing against the search and enriches it. Returns None to drop it."""
    s = cfg["search"]
    mdl = M.identify(raw["title"], models)
    if not mdl:
        return None
    if EXCLUDE_PATTERNS.search((raw["title"] or "") + " " + (raw.get("extra_text") or "")):
        return None
    year, price, miles = raw["year"], raw["price"], raw["mileage"]
    if year and (year < s["min_year"] or year > dt.date.today().year + 1):
        return None
    if miles and miles > s["max_mileage"]:
        return None
    if not price or price < 1000:
        return None
    lo, hi, margin = s["price_min"], s["price_max"], s["near_miss_margin"]
    if lo <= price <= hi:
        tier = "match"
    elif lo - margin <= price <= hi + margin:
        tier = "near"
    else:
        return None

    lat, lon, city = raw["lat"], raw["lon"], raw["city"]
    if (lat is None or not city) and raw["zip"]:
        zlat, zlon, zcity = geo.zip(raw["zip"])
        lat, lon, city = lat if lat is not None else zlat, lon if lon is not None else zlon, city or zcity
    if lat is None and city:
        lat, lon, _ = geo.place(city if "," in city else f"{city}, TX")
    drive_mi, drive_min = geo.drive(lat, lon)
    if drive_mi and drive_mi > s["radius_miles"] * 1.15:
        return None

    text = ((raw["title"] or "") + " " + (raw.get("extra_text") or "")).lower()
    flags = trouble_flags(text)
    vin = (raw["vin"] or "").strip().upper()
    if len(vin) == 17:
        key = f"vin:{vin}"
    elif miles and miles % 1000 and year:
        # Exact odometer readings identify the same car reposted or listed on several sites.
        key = f"car:{mdl.key}:{year}:{miles}"
    else:
        key = f'{SOURCE_KEYS[raw["source"]]}:{raw["id"]}'
    return {
        "key": key, "vin": vin or None, "model_key": mdl.key, "make": mdl.make, "model": mdl.model,
        "body": mdl.body, "seats": mdl.seats, "cargo": mdl.cargo_for(year),
        "title": raw["title"], "year": year, "trim": raw["trim"], "price": price, "mileage": miles,
        "tier": tier, "seller": raw["seller"], "private": raw["private"], "city": city,
        "lat": lat, "lon": lon, "drive_mi": round(drive_mi) if drive_mi else None,
        "drive_min": round(drive_min) if drive_min else None,
        "image_url": raw["image_url"], "links": {raw["source"]: raw["url"]},
        "flags": flags, "kbb": raw.get("kbb"), "deal_indicator": raw.get("deal_indicator"),
    }


def thumb_for(key, url, http):
    """Downloads and shrinks a listing photo. Returns the file name or None."""
    name = hashlib.sha1(key.encode()).hexdigest()[:16] + ".jpg"
    path = THUMBS / name
    if path.exists():
        return name
    if not url:
        return None
    try:
        r = http.get(url, timeout=30)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im.thumbnail((400, 300))
        im.save(path, "JPEG", quality=68, optimize=True)
        return name
    except Exception as e:
        log.warning("thumbnail failed for %s: %s", key, e)
        return None


def merge(old, new):
    """Combines a stored listing with a fresh sighting (possibly from another site)."""
    links = {**old.get("links", {}), **new["links"]}
    history = old.get("price_history") or [[old.get("first_seen_date"), old["price"]]]
    merged = {**old, **{k: v for k, v in new.items() if v not in (None, "", [])}}
    merged["links"] = links
    if new["price"] != old["price"]:
        history.append([dt.date.today().isoformat(), new["price"]])
    merged["price_history"] = history
    # Prefer the most detailed values across sites.
    for f in ("trim", "mileage", "year", "vin", "image_url", "thumb"):
        if not new.get(f) and old.get(f):
            merged[f] = old[f]
    return merged


def score(market, x, cut):
    d = market.score(x, cut)
    if d and x.get("flags") and d["label"] == "great":
        d["label"] = "good"  # don't oversell a car whose listing mentions title or mechanical trouble
    return d


def rescore(db, rel):
    """Re-rates every active listing against the latest market data and reliability ratings."""
    record_market(db, [], dt.date.today().isoformat())
    market = Market(db)
    rows = [(k, json.loads(d)) for k, d in db.execute("SELECT key, data FROM listings WHERE gone_on IS NULL")]
    raw = [market.score(x) for _, x in rows]
    cut = update_thresholds(db, [d["raw_pct"] for d in raw if d and "raw_pct" in d and d["label"] != "suspect"])
    for key, x in rows:
        x["flags"] = trouble_flags(x.get("title") or "", x.get("flags") or [])
        x["deal"] = score(market, x, cut)
        x["reliability"] = rel.rating(x["model_key"], x.get("year"))
        db.execute("UPDATE listings SET data=? WHERE key=?", (json.dumps(x), key))
    db.commit()


def load_config():
    """config.toml, with config.local.toml (not committed: holds the ntfy topic) merged over it."""
    cfg = tomllib.load(open(ROOT / "config.toml", "rb"))
    local = ROOT / "config.local.toml"
    if local.exists():
        for section, values in tomllib.load(open(local, "rb")).items():
            if isinstance(values, dict):
                cfg.setdefault(section, {}).update(values)
            else:
                cfg[section] = values
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--sources", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    THUMBS.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    models = M.enabled(cfg)
    db = open_db()
    now = dt.datetime.now().astimezone()
    today = now.date().isoformat()
    notifier = Notifier(cfg, enabled=not args.no_notify)
    rel = Reliability(db)
    rel.refresh(models, cfg["search"]["min_year"], dt.date.today().year - 2)

    if not args.report_only:
        first_run = db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
        raw, status = scrape(cfg, models, [x for x in args.sources.split(",") if x], db)
        geo = Geo(db, cfg["search"]["origin_lat"], cfg["search"]["origin_lon"])
        http = requests.Session(impersonate="chrome")

        market_rows = []
        for r in raw:
            mdl = M.identify(r["title"], models)
            if mdl and r["year"] and r["mileage"] and r["price"] and 2000 <= r["price"] <= 80000 \
                    and r["year"] >= cfg["search"]["min_year"] - 3 and "rebuilt title" not in (r.get("extra_text") or "").lower():
                market_rows.append((f'{r["source"]}:{r["id"]}', mdl.key, r["year"], r["mileage"], r["price"], today,
                                    int(bool(r["private"]))))
        record_market(db, market_rows, today)
        market = Market(db)
        cut = thresholds(db)
        log.info("deal model: %s", ", ".join(f'{k} n={f["n"]}' for k, f in sorted(market.fits.items())))

        fresh = {}
        for r in raw:
            n = normalize(r, cfg, models, geo)
            if not n:
                continue
            if n["key"] in fresh:
                fresh[n["key"]] = merge(fresh[n["key"]], n)
            else:
                fresh[n["key"]] = n
        log.info("%d raw listings -> %d in range", len(raw), len(fresh))

        new_items, drops = [], []
        for key, n in fresh.items():
            row = db.execute("SELECT data, first_seen, gone_on FROM listings WHERE key=?", (key,)).fetchone()
            if row:
                old = json.loads(row[0])
                merged = merge(old, n)
                if n["price"] < old["price"] and (old["price"] - n["price"] >= 300 or old["tier"] != "match" == n["tier"]):
                    drops.append((merged, old["price"]))
                first_seen = row[1]
            else:
                merged = {**n, "price_history": [[today, n["price"]]], "first_seen_date": today}
                first_seen = now.isoformat()
                new_items.append(merged)
            merged["thumb"] = merged.get("thumb") or thumb_for(key, n["image_url"], http)
            merged["deal"] = score(market, merged, cut)
            merged["reliability"] = rel.rating(merged["model_key"], merged.get("year"))
            db.execute("""INSERT INTO listings (key, data, first_seen, last_seen, gone_on) VALUES (?,?,?,?,NULL)
                          ON CONFLICT(key) DO UPDATE SET data=excluded.data, last_seen=excluded.last_seen, gone_on=NULL""",
                       (key, json.dumps(merged), first_seen, now.isoformat()))

        # Mark listings gone when every site that showed them was checked successfully.
        ok_sources = {name for name, err in status.items() if err is None}
        for key, data in db.execute("SELECT key, data FROM listings WHERE gone_on IS NULL").fetchall():
            if key in fresh:
                continue
            srcs = {SOURCE_KEYS.get(s) for s in json.loads(data).get("links", {})}
            if srcs and srcs <= ok_sources:
                db.execute("UPDATE listings SET gone_on=? WHERE key=?", (today, key))

        for name, err in status.items():
            if err is None:
                db.execute("""INSERT INTO source_health (source, failures, last_ok, last_error, last_count, alerted)
                              VALUES (?,0,?,NULL,?,0) ON CONFLICT(source) DO UPDATE SET failures=0, last_ok=excluded.last_ok,
                              last_error=NULL, last_count=excluded.last_count, alerted=0""",
                           (name, now.isoformat(), sum(1 for r in raw if SOURCE_KEYS[r["source"]] == name)))
            else:
                db.execute("""INSERT INTO source_health (source, failures, last_error) VALUES (?,1,?)
                              ON CONFLICT(source) DO UPDATE SET failures=failures+1, last_error=excluded.last_error""",
                           (name, err))
        db.execute("INSERT OR REPLACE INTO runs VALUES (?,?)", (now.isoformat(), json.dumps(
            {"raw": len(raw), "in_range": len(fresh), "new": len(new_items), "status": status})))
        db.commit()

        if not cfg["ntfy"].get("notify_near_misses", True):
            new_items = [x for x in new_items if x["tier"] == "match"]
        if first_run:
            notifier.first_run(fresh.values())
        else:
            notifier.new_listings(new_items)
            notifier.price_drops(drops)
        failing = db.execute("SELECT source, failures, last_error FROM source_health WHERE failures>=? AND alerted=0",
                             (cfg["ntfy"].get("source_failure_alert_after", 3),)).fetchall()
        if failing:
            notifier.source_failures(failing)
            db.executemany("UPDATE source_health SET alerted=1 WHERE source=?", [(f[0],) for f in failing])
            db.commit()

    rescore(db, rel)
    site = build_report(db, cfg, models, ROOT / "out" / "site", THUMBS, now)
    log.info("report written to %s", site)

    if not args.no_publish and cfg.get("publish", {}).get("repo"):
        publish(cfg, ROOT, site)
    return 0


if __name__ == "__main__":
    sys.exit(main())
