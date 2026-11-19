#!/usr/bin/env python3
"""Shows what each car site returns right now, to work out why a scraper stopped finding listings.

    .venv/bin/python scripts/diagnose.py

Makes spaced-out requests (two to three minutes in total), changes nothing in the database and
sends no notifications. Paste the output to whoever is fixing the scraper. The pages it looked at
are also saved in out/debug/ in case they are asked for.
"""
import html
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from curl_cffi import requests  # noqa: E402

from finder import flight  # noqa: E402
from finder import models as M  # noqa: E402
from finder import sources as S  # noqa: E402
from finder.config import load_config  # noqa: E402
from finder.reliability import NHTSA_NAMES  # noqa: E402

DEBUG = ROOT / "out/debug"

# Other ways each plug-in model might be reachable on Autotrader, tried after the slugs in
# finder/models.py. "PIH" is Autotrader's code for plug-in hybrid.
AUTOTRADER_CANDIDATES = {
    "wrangler4xe": ["jeep/wrangler?fuelTypeGroup=PIH"],
    "grandcherokee4xe": ["jeep/grand-cherokee?fuelTypeGroup=PIH"],
    "outlanderphev": ["mitsubishi/outlander?fuelTypeGroup=PIH"],
    "tucsonphev": ["hyundai/tucson?fuelTypeGroup=PIH"],
    "rav4prime": ["toyota/rav4?fuelTypeGroup=PIH"],
}
BLOCK_MARKERS = ["captcha-delivery", "datadome", "px-captcha", "perimeterx", "cf-chl", "cloudflare", "akamai",
                 "incapsula", "Access Denied", "unusual activity", "captcha"]


def save(name, text):
    DEBUG.mkdir(parents=True, exist_ok=True)
    (DEBUG / name).write_text(text)


def short(v, depth=0):
    """A value cut down to its shape, for printing."""
    if isinstance(v, str):
        return v if len(v) <= 70 else v[:70] + "…"
    if isinstance(v, list):
        return [short(x, depth + 1) for x in v[:2]] + ([f"…{len(v) - 2} more"] if len(v) > 2 else [])
    if isinstance(v, dict):
        if depth >= 4:
            return f"{{…{len(v)} keys}}"
        return {k: short(x, depth + 1) for k, x in list(v.items())[:45]}
    return v


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
        ("NHTSA (model, year, complaints, recalls)",
         "SELECT model_key, year, complaints, recalls FROM reliability ORDER BY 1, 2"),
        ("active listings (model, in budget or near miss, count)",
         "SELECT json_extract(data, '$.model'), json_extract(data, '$.tier'), count(*) FROM listings "
         "WHERE gone_on IS NULL GROUP BY 1, 2"),
        ("history filled in (source of history, count)",
         "SELECT json_extract(data, '$.history_source'), count(*) FROM listings WHERE gone_on IS NULL GROUP BY 1"),
    ]:
        try:
            print(f"== {label}:", db.execute(sql).fetchall())
        except sqlite3.Error as e:
            print(f"== {label}: {e}")


def nhtsa(models):
    """The model names NHTSA files complaints under, next to the names this tool asks for."""
    q = requests.Session(impersonate="chrome")
    for make in sorted({m.make for m in models}):
        words = {m.model.split()[0].upper() for m in models if m.make == make}
        asked = sorted(NHTSA_NAMES.get(m.key, m.model.upper()) for m in models if m.make == make)
        for issue in ("c", "r"):
            try:
                r = q.get("https://api.nhtsa.gov/products/vehicle/models",
                          params={"modelYear": 2023, "make": make, "issueType": issue}, timeout=30).json()
                names = sorted({x.get("model", "") for x in r.get("results", [])})
                print(f"== nhtsa {make} 2023 {'complaints' if issue == 'c' else 'recalls'}: we ask for {asked}; "
                      f"NHTSA has {[n for n in names if any(w in n.upper() for w in words)]}")
            except Exception as e:
                print(f"== nhtsa {make}: {type(e).__name__}: {str(e)[:100]}")
            time.sleep(1)


def carscom_search(q, s, mdl, slug):
    p = {"makes[]": mdl.make.lower(), "models[]": slug, "maximum_distance": s["radius_miles"],
         "zip": s["origin_zip"], "stock_type": "used", "page_size": 100, "page": 1}
    r = q.get("https://www.cars.com/shopping/results/?" + urlencode(p), timeout=40)
    blobs = [json.loads(html.unescape(b)) for b in re.findall(r'data-vehicle-details="([^"]+)"', r.text)]
    return r, blobs


def carscom(s, models):
    q = requests.Session(impersonate="chrome")
    first, refused = None, []
    for mdl in models:
        for slug in slugs(mdl.carscom):
            try:
                r, blobs = carscom_search(q, s, mdl, slug)
                years = sorted({str(b.get("year")) for b in blobs})
                names = sorted({str(b.get("model")) for b in blobs})[:4]
                print(f"== cars.com search {slug}: HTTP {r.status_code}, {len(blobs)} on page 1, years {years}, models {names}")
                if r.status_code != 200:
                    refused.append((mdl, slug))
                if blobs and not first:
                    first = (blobs[0]["listingId"], r.url)
            except Exception as e:
                print(f"== cars.com search {slug}: {type(e).__name__}: {str(e)[:100]}")
            time.sleep(2)
    for mdl, slug in refused:  # refused once: a one-off, or does the site not know this name?
        r, blobs = carscom_search(q, s, mdl, slug)
        print(f"== cars.com search {slug}, second try: HTTP {r.status_code}, {len(blobs)} on page 1")
        time.sleep(2)
    if not first:
        return
    lid, ref = first
    url = f"https://www.cars.com/vehicledetail/{lid}/"
    try:
        r = requests.Session(impersonate="chrome").get(url, headers={"Referer": ref}, timeout=40)
        t = r.text
        title = re.search(r"<title>(.*?)</title>", t, re.S)
        save("carscom-listing.html", t)
        print(f"== cars.com listing page (plain request): HTTP {r.status_code}, {len(t)} chars, "
              f"title {title.group(1).strip()[:70] if title else None!r}, history box {'stat__label' in t}")
        print("   block markers:", {k: t.lower().count(k.lower()) for k in BLOCK_MARKERS if k.lower() in t.lower()})
        print("   script sources:", sorted(set(re.findall(r'<script[^>]*\bsrc="([^"?]+)', t)))[:6])
        print("   text:", re.sub(r"\s+", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", t, flags=re.S)).strip()[:300])
    except Exception as e:
        print(f"== cars.com listing page (plain request): {type(e).__name__}: {str(e)[:100]}")
    time.sleep(3)
    # The same page in the stealth browser that already handles CarMax and Facebook.
    try:
        with S.Browser() as b:
            pg = b.page()
            pg.goto(ref, timeout=60000)
            pg.wait_for_timeout(4000)
            resp = pg.goto(url, timeout=60000, referer=ref)
            pg.wait_for_timeout(5000)
            t = pg.content()
            save("carscom-listing-browser.html", t)
            stats = re.findall(r'<dt class="stat__label">(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>', t, re.S)
            stats = [(re.sub(r"<[^>]+>", "", k).strip(), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", v)).strip()) for k, v in stats]
            print(f"== cars.com listing page (stealth browser): HTTP {resp.status if resp else None}, {len(t)} chars, "
                  f"title {pg.title()[:70]!r}, history box {'stat__label' in t}, mentions AutoCheck {'AutoCheck' in t}")
            print("   history stats:", stats[:8])
            pg.close()
    except Exception as e:
        print(f"== cars.com listing page (stealth browser): {type(e).__name__}: {str(e)[:200]}")


def autotrader_fetch(q, s, slug, first=0):
    path, _, extra = slug.partition("?")
    params = {"zip": s["origin_zip"], "searchRadius": s["radius_miles"], "startYear": s["min_year"],
              "maxPrice": s["price_max"] + s["near_miss_margin"], "maxMileage": s["max_mileage"],
              "sortBy": "derivedpriceASC", "numRecords": 25, "firstRecord": first, **dict(parse_qsl(extra))}
    r = q.get(f"https://www.autotrader.com/cars-for-sale/used-cars/{path}?{urlencode(params)}", timeout=40)
    return r, S.autotrader_page_data(r.text)


def autotrader(s, models):
    q = requests.Session(impersonate="chrome")
    shown_raw = False
    for mdl in models:
        for slug in list(slugs(mdl.autotrader)) + AUTOTRADER_CANDIDATES.get(mdl.key, []):
            try:
                r, data = autotrader_fetch(q, s, slug)
            except Exception as e:
                print(f"== autotrader {slug}: {type(e).__name__}: {str(e)[:100]}")
                continue
            title = re.search(r"<title>(.*?)</title>", r.text, re.S)
            inv = (data or {}).get("inventory") or {}
            rows = []
            for it in inv.values():
                text = " ".join(str(x) for x in (S._name(it.get("model")), S._name(it.get("trim")), it.get("title")) if x)
                rows.append((mdl.matches(text), it))
            hits = [it for ok, it in rows if ok]
            print(f"== autotrader {slug}: HTTP {r.status_code}, title {title.group(1).strip()[:60] if title else None!r}")
            print(f"   landed on {str(r.url).split('?')[0][-70:]}; page data {'found' if data is not None else 'MISSING'}; "
                  f"{len(inv)} listings, {len(hits)} are a {mdl.model}; owners {len((data or {}).get('owners') or {})}")
            print("   titles:", [str(it.get("title"))[:50] for _, it in rows[:4]])
            if hits:
                print("   years", sorted({str(it.get("year")) for it in hits}), "fuel", sorted({str(it.get("fuelType"))[:40] for it in hits})[:3])
            if inv and not shown_raw:
                shown_raw = True
                save("autotrader.html", r.text)
                it = next(iter(inv.values()))
                print("   RAW LISTING:", json.dumps(short(it), ensure_ascii=False))
                own = ((data or {}).get("owners") or {}).get(str(it.get("ownerId")))
                print("   RAW OWNER:", json.dumps(short(own), ensure_ascii=False))
                text = flight.flight_text(r.text)
                for word in ("fuelTypeGroup", "PIH", "firstRecord", "numRecords", "totalResultCount", "4xe"):
                    found = sorted({m.group(0) for m in re.finditer(r".{0,70}" + re.escape(word) + r".{0,90}", text)})[:2]
                    print(f"   near {word!r}:", found)
                time.sleep(3)
                r2, d2 = autotrader_fetch(q, s, slug, first=25)
                ids2 = set(((d2 or {}).get("inventory") or {}))
                print(f"   second page: {len(ids2)} listings, {len(ids2 & set(inv))} repeated from the first page")
            if data is None:
                save(f"autotrader-{mdl.key}.html", r.text)
                t = r.text
                print("   block markers:", {k: t.lower().count(k.lower()) for k in BLOCK_MARKERS if k.lower() in t.lower()})
            time.sleep(3)


def slugs(v):
    return S.slugs(v)


def main():
    cfg = load_config()
    s, models = cfg["search"], M.enabled(cfg)
    for step in (database, lambda: nhtsa(models), lambda: carscom(s, models), lambda: autotrader(s, models)):
        try:
            step()
        except Exception as e:
            print(f"== step failed: {type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
