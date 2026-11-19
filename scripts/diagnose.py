#!/usr/bin/env python3
"""Shows what each car site returns right now, to work out why a scraper stopped finding listings.

    .venv/bin/python scripts/diagnose.py

Makes spaced-out requests (three to four minutes in total), changes nothing in the database and
sends no notifications. Paste the output to whoever is fixing the scraper. The pages it looked at
are also saved in out/debug/ in case they are asked for.
"""
import html
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from curl_cffi import requests  # noqa: E402

from finder import models as M  # noqa: E402
from finder import sources as S  # noqa: E402
from finder.config import load_config  # noqa: E402
from finder.reliability import NHTSA_NAMES  # noqa: E402

DEBUG = ROOT / "out/debug"
BLOCK_MARKERS = ["captcha-delivery", "datadome", "px-captcha", "perimeterx", "cf-chl", "just a moment", "akamai",
                 "incapsula", "access denied", "unusual activity"]
HISTORY_WORDS = r"accident|owner|title|autocheck|carfax|personal.?use|history|damage"


def save(name, text):
    DEBUG.mkdir(parents=True, exist_ok=True)
    (DEBUG / name).write_text(text)


def squeeze(text, n=None):
    text = re.sub(r"\s+", " ", text).strip()
    return text if n is None else text[:n]


def visible(page):
    """A page's text without scripts, styles and tags."""
    return squeeze(html.unescape(re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", page, flags=re.S)))


def markers(page):
    low = page.lower()
    return {k: low.count(k) for k in BLOCK_MARKERS if k in low}


def database():
    path = ROOT / "data/finder.db"
    if not path.exists():
        print("== no data/finder.db yet (no run has finished)")
        return
    db = sqlite3.connect(path)
    for label, sql in [
        ("sources (name, failures in a row, listings last time, last error)",
         "SELECT source, failures, last_count, substr(coalesce(last_error, ''), 1, 100) FROM source_health"),
        ("price observations (model, count)", "SELECT model_key, count(*) FROM market GROUP BY 1"),
        ("NHTSA (model, year, complaints, recalls)",
         "SELECT model_key, year, complaints, recalls FROM reliability ORDER BY 1, 2"),
        ("active listings (site, model, count)",
         "SELECT json_extract(data, '$.source'), json_extract(data, '$.model'), count(*) FROM listings "
         "WHERE gone_on IS NULL GROUP BY 1, 2"),
        ("history filled in (source of history, count)",
         "SELECT json_extract(data, '$.history_source'), count(*) FROM listings WHERE gone_on IS NULL GROUP BY 1"),
    ]:
        try:
            print(f"== {label}:", db.execute(sql).fetchall())
        except sqlite3.Error as e:
            print(f"== {label}: {e}")


def nhtsa(models):
    """Complaint and recall counts for 2023 under each name the tool asks NHTSA for."""
    q = requests.Session(impersonate="chrome")
    for mdl in models:
        names = NHTSA_NAMES.get(mdl.key, mdl.model.upper())
        for name in (names,) if isinstance(names, str) else names:
            try:
                p = {"make": mdl.make, "model": name, "modelYear": 2023}
                c = q.get("https://api.nhtsa.gov/complaints/complaintsByVehicle", params=p, timeout=30).json()
                r = q.get("https://api.nhtsa.gov/recalls/recallsByVehicle", params=p, timeout=30).json()
                print(f"== nhtsa 2023 {mdl.make} {name!r}: {len(c.get('results', []))} complaints, {len(r.get('results', []))} recalls")
            except Exception as e:
                print(f"== nhtsa 2023 {mdl.make} {name!r}: {type(e).__name__}: {str(e)[:100]}")
            time.sleep(1)


def autotrader(s, models):
    """Runs the real Autotrader scraper on the first two pages of each model."""
    client, seen, shown_raw = S._AutotraderClient(), set(), False
    fetch, pages = client.eggs, []
    client.eggs = lambda slug, params: (pages.append(fetch(slug, params)), pages[-1])[1]
    for mdl in models:
        for slug in S.slugs(mdl.autotrader):
            del pages[:]
            try:
                rows = S._autotrader_model(s, mdl, client, seen, slug, pages=2)
            except Exception as e:
                print(f"== autotrader {slug}: {type(e).__name__}: {str(e)[:300]}")
                continue
            raw = {lid: it for p in pages for lid, it in (p.get("inventory") or {}).items() if isinstance(it, dict)}
            kinds = Counter(str(it.get("listingType")) for it in raw.values())
            print(f"== autotrader {slug}: {len(pages)} pages, {len(raw)} listings on them {dict(kinds)}, {len(rows)} kept as a {mdl.model}")
            have = {k: sum(1 for r in rows if r.get(k)) for k in ("price", "mileage", "vin", "kbb", "deal_indicator", "city", "lat", "seller", "image_url")}
            prices = sorted(r["price"] for r in rows if r["price"])
            print(f"   filled in: {have}; years {sorted({r['year'] for r in rows if r['year']})}; "
                  f"prices {prices[0] if prices else None} to {prices[-1] if prices else None}; private sellers {sum(r['private'] for r in rows)}")
            for r in rows[:3]:
                print("   KEPT:", json.dumps({k: r[k] for k in ("title", "year", "trim", "price", "mileage", "kbb", "deal_indicator", "seller", "city", "zip", "vin")}, ensure_ascii=False))
            kept = {r["id"] for r in rows}
            left = [it for lid, it in raw.items() if lid not in kept and it.get("listingType") != "NEW"]
            print("   LEFT OUT (not new cars):", [(str(it.get("title"))[:45], S._name(it.get("model")), str(it.get("fuelType"))[:45]) for it in left[:5]])
            if raw and not shown_raw:
                shown_raw = True
                it = raw.get(rows[0]["id"]) if rows else next(iter(raw.values()))
                print("   RAW KEYS:", sorted(it))
                for k in ("pricingDetail", "mileage", "title", "trim", "model", "year", "fuelType", "listingType", "vin", "price"):
                    print(f"   RAW {k}:", json.dumps(it.get(k), ensure_ascii=False)[:400])
            time.sleep(3)


def carscom_ids(s, mdl):
    """A few listing ids from the first Cars.com results page of a model, and that page's address."""
    q = requests.Session(impersonate="chrome")
    p = {"makes[]": mdl.make.lower(), "models[]": S.slugs(mdl.carscom)[0], "maximum_distance": s["radius_miles"],
         "zip": s["origin_zip"], "stock_type": "used", "page_size": 100, "page": 1}
    url = "https://www.cars.com/shopping/results/?" + urlencode(p)
    r = q.get(url, timeout=40)
    if r.status_code == 403:
        time.sleep(5)
        r = q.get(url, timeout=40)
    blobs = [json.loads(html.unescape(b)) for b in re.findall(r'data-vehicle-details="([^"]+)"', r.text)]
    print(f"== cars.com search: HTTP {r.status_code}, {len(blobs)} cars on the page")
    if blobs:
        print("   search card fields:", json.dumps(blobs[0], ensure_ascii=False)[:900])
        card = re.search(r'<[^>]+data-vehicle-details=.*?(?=<[^>]+data-vehicle-details=|\Z)', r.text, re.S)
        print("   search card text:", visible(card.group(0))[:500] if card else None)
    return [b["listingId"] for b in blobs if b.get("listingId")][:3], str(r.url)


def history_report(page):
    """Prints where a Cars.com listing page keeps the vehicle history, in markup, text and data."""
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S)
    text = visible(page)
    print(f"   'AutoCheck' appears {page.count('AutoCheck')} times in the page, {body.count('AutoCheck')} outside scripts, "
          f"{text.count('AutoCheck')} in the text; old history box {'stat__label' in page}")
    for label, src, before, after in (("MARKUP", body, 600, 2400), ("TEXT", text, 300, 900)):
        hits = [m.start() for m in re.finditer(r"AutoCheck|accidents? reported|No accident|[0-9] owner|1-owner|Clean title", src, re.I)]
        last, shown = -10000, 0
        for i in hits:
            if i - last > after and shown < (1 if label == "MARKUP" else 3):
                print(f"   {label} near {src[i:i + 20]!r}:", squeeze(src[max(0, i - before):i + after]))
                last, shown = i, shown + 1
    facts = re.findall(r'"([A-Za-z_]*(?:' + HISTORY_WORDS + r')[A-Za-z_]*)"\s*:\s*("[^"]{0,60}"|[\w.\-]+)', html.unescape(page), re.I)
    print("   DATA fields:", sorted(set(facts))[:40])
    tags = re.findall(r'<([a-z0-9-]+)[^>]*\b(?:class|id|data-[a-z-]+)="([^"]*(?:' + HISTORY_WORDS + r')[^"]*)"', body, re.I)
    print("   TAGS:", sorted({(t, c[:70]) for t, c in tags})[:40])


def browser_checks(s, models):
    """Cars.com listing pages and CarMax searches, in the stealth browser."""
    try:
        ids, ref = carscom_ids(s, models[0])
    except Exception as e:
        print(f"== cars.com search: {type(e).__name__}: {str(e)[:150]}")
        ids, ref = [], None
    with S.Browser() as b:
        pg = b.page()
        for n, lid in enumerate(ids):
            url = f"https://www.cars.com/vehicledetail/{lid}/"
            try:
                t0 = time.time()
                if n == 0:  # the first one the careful way: search page first, then the listing
                    pg.goto(ref, timeout=60000)
                    pg.wait_for_timeout(4000)
                    resp = pg.goto(url, timeout=60000, referer=ref)
                    pg.wait_for_timeout(5000)
                else:  # the rest the quick way, to see whether that is enough
                    resp = pg.goto(url, timeout=60000, wait_until="domcontentloaded")
                    pg.wait_for_timeout(1500)
                t = pg.content()
                print(f"== cars.com listing {n + 1} ({'via search page' if n == 0 else 'direct'}): HTTP {resp.status if resp else None}, "
                      f"{time.time() - t0:.0f}s, {len(t)} chars, title {pg.title()[:60]!r}, block markers {markers(t)}")
                if n == 0:
                    save("carscom-listing-browser.html", t)
                    history_report(t)
                else:
                    print("   TEXT:", [squeeze(m.group(0)) for m in re.finditer(r".{0,80}(?:AutoCheck|accidents? reported|No accident|[0-9] owner|Clean title).{0,160}", visible(t), re.I)][:3])
            except Exception as e:
                print(f"== cars.com listing {n + 1}: {type(e).__name__}: {str(e)[:200]}")
        pg.close()
        pg = b.page()
        try:
            pg.goto("https://www.carmax.com/cars/toyota", timeout=60000)
            pg.wait_for_timeout(4000)
            for mdl in models:
                for slug in S.slugs(mdl.carmax):
                    api = (f'/cars/api/search/run?uri=/cars/{slug}&skip=0&take=100&zipCode={s["origin_zip"]}'
                           f'&radius={s["radius_miles"]}&shipping=-1&sort=price-asc')
                    status, body = pg.evaluate("async (u) => { const r = await fetch(u); return [r.status, await r.text()] }", api)
                    try:
                        d = json.loads(body)
                    except ValueError:
                        d = {}
                    items = d.get("items") or []
                    names = Counter(f'{c.get("make")} {c.get("model")}' for c in items)
                    ours = sum(1 for c in items if mdl.matches(f'{c.get("make")} {c.get("model")} {c.get("trim") or ""}'))
                    print(f"== carmax {slug}: HTTP {status}, {len(items)} cars (site says {d.get('totalCount')}), "
                          f"{ours} are a {mdl.model}; models {dict(names.most_common(4))}")
                    if items and not ours:
                        print("   first car:", json.dumps({k: items[0].get(k) for k in ("year", "make", "model", "trim", "engineType", "fuelType", "mpgCity")}))
                    pg.wait_for_timeout(800)
        except Exception as e:
            print(f"== carmax: {type(e).__name__}: {str(e)[:200]}")
        pg.close()


def main():
    cfg = load_config()
    s, models = cfg["search"], M.enabled(cfg)
    for step in (database, lambda: nhtsa(models), lambda: autotrader(s, models), lambda: browser_checks(s, models)):
        try:
            step()
        except Exception as e:
            print(f"== step failed: {type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
