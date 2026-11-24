#!/usr/bin/env python3
"""Shows what each car site returns right now, to work out why a scraper stopped finding listings.

    .venv/bin/python scripts/diagnose.py

Makes spaced-out requests (about three minutes in total), changes nothing in the database and
sends no notifications. Paste the output to whoever is fixing the scraper. The pages it looked at
are also saved in out/debug/ in case they are asked for.
"""
import html
import json
import logging
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

DEBUG = ROOT / "out/debug"
BLOCK_MARKERS = ["captcha-delivery", "datadome", "px-captcha", "perimeterx", "cf-chl", "just a moment", "akamai",
                 "incapsula", "access denied", "unusual activity"]


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
         "SELECT j.key, json_extract(l.data, '$.model'), count(*) FROM listings l, json_each(l.data, '$.links') j "
         "WHERE l.gone_on IS NULL GROUP BY 1, 2"),
        ("active listings (deal label, count)",
         "SELECT json_extract(data, '$.deal.label'), count(*) FROM listings WHERE gone_on IS NULL GROUP BY 1"),
        ("history filled in (source of history, owners, accidents, prior use, count)",
         "SELECT json_extract(data, '$.history_source'), json_extract(data, '$.owners'), json_extract(data, '$.accidents'), "
         "json_extract(data, '$.prior_use'), count(*) FROM listings WHERE gone_on IS NULL GROUP BY 1, 2, 3, 4"),
        ("listing pages checked for history (with a report, without)",
         "SELECT sum(data != '{}'), sum(data = '{}') FROM carscom_history"),
    ]:
        try:
            print(f"== {label}:", db.execute(sql).fetchall())
        except sqlite3.Error as e:
            print(f"== {label}: {e}")


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
            fuel = lambda it: (it.get("fuelType") or {}).get("group") if isinstance(it.get("fuelType"), dict) else it.get("fuelType")  # noqa: E731
            print(f"   LEFT OUT ({len(left)} that are not new cars):", [(S._at_title(it)[:40], S._name(it.get("model")), str(fuel(it))[:22]) for it in left[:8]])
            if rows and not shown_raw:
                shown_raw = True
                print("   RAW pricingDetail:", json.dumps(raw[rows[0]["id"]].get("pricingDetail"), ensure_ascii=False)[:700])
            time.sleep(3)


def carscom_ids(s, mdl):
    """A few listing ids from the first Cars.com results page of a model, each with that page's address."""
    q = requests.Session(impersonate="chrome")
    p = {"makes[]": mdl.make.lower(), "models[]": S.slugs(mdl.carscom)[0], "maximum_distance": s["radius_miles"],
         "zip": s["origin_zip"], "stock_type": "used", "page_size": 100, "page": 1}
    url = "https://www.cars.com/shopping/results/?" + urlencode(p)
    r = q.get(url, timeout=40)
    if r.status_code == 403:
        time.sleep(5)
        r = q.get(url, timeout=40)
    ids = [json.loads(html.unescape(b)).get("listingId") for b in re.findall(r'data-vehicle-details="([^"]+)"', r.text)]
    print(f"== cars.com search for a {mdl.model}: HTTP {r.status_code}, {len(ids)} cars on the page")
    return [(i, str(r.url)) for i in ids if i][:3]


def browser_checks(s, models):
    """Cars.com vehicle history and the CarMax searches, in the stealth browser, using the real scraper code."""
    todo = []
    for mdl in models[:2]:
        try:
            todo += carscom_ids(s, mdl)
        except Exception as e:
            print(f"== cars.com search: {type(e).__name__}: {str(e)[:150]}")
        time.sleep(2)
    with S.Browser() as b:
        pages, parse = [], S.carscom_history_parse
        S.carscom_history_parse = lambda t: (pages.append(t), parse(t))[1]
        try:
            t0 = time.time()
            got = S.carscom_history(b, todo)
            print(f"== cars.com vehicle history: asked for {len(todo)} listing pages, read {len(got)} in {time.time() - t0:.0f}s")
            for (lid, data), t in zip(got.items(), pages):
                box = re.search(r".{0,40}Reported by.{0,260}", visible(t))
                print(f"   {lid[:8]}: {data} | {len(t)} chars, block markers {markers(t[:20000])} | {box.group(0) if box else visible(t)[:200]!r}")
            if pages:
                save("carscom-listing-browser.html", pages[0])
        except Exception as e:
            print(f"== cars.com vehicle history: {type(e).__name__}: {str(e)[:300]}")
        finally:
            S.carscom_history_parse = parse
        pg = b.page()
        try:
            pg.goto("https://www.carmax.com/cars/toyota", timeout=60000)
            pg.wait_for_timeout(4000)
            for mdl in models:
                for slug in S.slugs(mdl.carmax):
                    try:
                        items = S.carmax_search(pg, s, mdl, slug)
                        names = Counter(f'{c.get("year")} {c.get("make")} {c.get("model")}' for c in items)
                        prices = sorted(c.get("basePrice") or 0 for c in items)
                        print(f"== carmax {slug}: {len(items)} are a {mdl.model}; prices {prices[0] if prices else None} to "
                              f"{prices[-1] if prices else None}; {dict(names.most_common(6))}")
                    except Exception as e:
                        print(f"== carmax {slug}: {type(e).__name__}: {str(e)[:200]}")
        except Exception as e:
            print(f"== carmax: {type(e).__name__}: {str(e)[:200]}")
        pg.close()


def main():
    cfg = load_config()
    s, models = cfg["search"], M.enabled(cfg)
    logging.basicConfig(level=logging.INFO, format="   log: %(message)s", stream=sys.stdout)
    for step in (database, lambda: autotrader(s, models), lambda: browser_checks(s, models)):
        try:
            step()
        except Exception as e:
            print(f"== step failed: {type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
