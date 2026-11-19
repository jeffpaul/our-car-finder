#!/usr/bin/env python3
"""Shows what each car site returns right now, to work out why a scraper stopped finding listings.

    .venv/bin/python scripts/diagnose.py

Makes a few spaced-out requests (about a minute in total), changes nothing, and sends no
notifications. Paste the output to whoever is fixing the scraper.
"""
import html
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from curl_cffi import requests  # noqa: E402

from finder import models as M  # noqa: E402
from finder.config import load_config  # noqa: E402
from finder.sources import slugs  # noqa: E402


def database():
    path = ROOT / "data/finder.db"
    if not path.exists():
        print("== no data/finder.db yet (no run has finished)")
        return
    db = sqlite3.connect(path)
    for label, sql in [
        ("sources (name, failures in a row, listings last time, last error)",
         "SELECT source, failures, last_count, substr(coalesce(last_error, ''), 1, 100) FROM source_health"),
        ("price observations (model, oldest year, newest year, count)",
         "SELECT model_key, min(year), max(year), count(*) FROM market GROUP BY 1"),
        ("NHTSA (model, complaints, recalls)",
         "SELECT model_key, sum(complaints), sum(recalls) FROM reliability GROUP BY 1"),
        ("active listings (in budget or near miss, count)",
         "SELECT json_extract(data, '$.model'), json_extract(data, '$.tier'), count(*) FROM listings "
         "WHERE gone_on IS NULL GROUP BY 1, 2"),
    ]:
        try:
            print(f"== {label}:", db.execute(sql).fetchall())
        except sqlite3.Error as e:
            print(f"== {label}: {e}")


def carscom(s, models):
    q = requests.Session(impersonate="chrome")
    first = None
    for mdl in models:
        for slug in slugs(mdl.carscom):
            p = {"makes[]": mdl.make.lower(), "models[]": slug, "maximum_distance": s["radius_miles"],
                 "zip": s["origin_zip"], "stock_type": "used", "page_size": 20, "page": 1}
            try:
                r = q.get("https://www.cars.com/shopping/results/?" + urlencode(p), timeout=40)
                blobs = [json.loads(html.unescape(b)) for b in re.findall(r'data-vehicle-details="([^"]+)"', r.text)]
                names = sorted({str(b.get("model")) for b in blobs})[:4]
                print(f"== cars.com search {slug}: HTTP {r.status_code}, {len(blobs)} on page 1, models {names}")
                if blobs and not first:
                    first = (blobs[0]["listingId"], r.url)
            except Exception as e:
                print(f"== cars.com search {slug}: {type(e).__name__}: {str(e)[:100]}")
            time.sleep(2)
    if not first:
        return
    lid, ref = first
    url = f"https://www.cars.com/vehicledetail/{lid}/"
    for name, session, headers in [
        ("new chrome session + referer (what the scraper does)", requests.Session(impersonate="chrome"), {"Referer": ref}),
        ("search session + referer", q, {"Referer": ref}),
        ("search session, no referer", q, {}),
        ("new safari session + referer", requests.Session(impersonate="safari"), {"Referer": ref}),
    ]:
        try:
            r = session.get(url, headers=headers, timeout=40)
            print(f"== cars.com listing page, {name}: HTTP {r.status_code}, {len(r.text)} chars, "
                  f"history box {'stat__label' in r.text}, mentions AutoCheck {'AutoCheck' in r.text}")
        except Exception as e:
            print(f"== cars.com listing page, {name}: {type(e).__name__}: {str(e)[:100]}")
        time.sleep(3)


def autotrader(s, models):
    q = requests.Session(impersonate="chrome")
    # A plain, common model first: if that fails too, the page format changed, not our model names.
    todo = ["toyota/camry"] + [sl for mdl in models for sl in slugs(mdl.autotrader)]
    for slug in todo[:3]:
        params = {"zip": s["origin_zip"], "searchRadius": s["radius_miles"], "numRecords": 25}
        for site, tmpl in [("autotrader", "https://www.autotrader.com/cars-for-sale/used-cars/{}?{}"),
                           ("kbb", "https://www.kbb.com/cars-for-sale/used/{}?{}")]:
            try:
                r = q.get(tmpl.format(slug, urlencode(params)), timeout=40)
            except Exception as e:
                print(f"== {site} {slug}: {type(e).__name__}: {str(e)[:100]}")
                continue
            t = r.text
            title = re.search(r"<title>(.*?)</title>", t, re.S)
            print(f"== {site} {slug}: HTTP {r.status_code}, {len(t)} chars, title {title.group(1).strip()[:70] if title else None!r}")
            print("   final url:", r.url[:120])
            print("   script ids:", sorted(set(re.findall(r'<script[^>]*\bid="([^"]+)"', t)))[:12])
            marks = ["__NEXT_DATA__", "__eggsState", "__next_f.push", "application/ld+json", '"vin"', '\\"vin\\"',
                     "kbbFppAmount", "listingId", "captcha", "px-captcha"]
            print("   markers:", {k: t.count(k) for k in marks if t.count(k)})
            m = re.search(r".{0,150}vin.{0,4}:.{0,200}", t, re.S)
            print("   sample near a VIN:", repr(m.group(0)) if m else None)
            time.sleep(3)
            if site == "autotrader" and "__NEXT_DATA__" in t:
                break  # Autotrader itself is readable, no need to try KBB for this model


def main():
    cfg = load_config()
    s, models = cfg["search"], M.enabled(cfg)
    database()
    carscom(s, models)
    autotrader(s, models)


if __name__ == "__main__":
    main()
