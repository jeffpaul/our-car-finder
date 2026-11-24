"""Scrapers. Each returns a list of raw listing dicts with these keys (missing -> None):

source, id, url, title, year, trim, price, mileage, vin, seller, private,
city, lat, lon, zip, image_url

Cars.com, Craigslist and Autotrader (with KBB as a fallback) work with plain HTTP
(Chrome/Safari TLS impersonation). CarMax and Facebook need a real browser; Camoufox (stealth Firefox)
gets past their bot checks where headless Chromium does not.
"""
import html as htmlmod
import json
import logging
import re
import time
from urllib.parse import parse_qsl, quote, urlencode

from curl_cffi import requests

from . import flight

log = logging.getLogger("finder")
# A year can run into the make ("2016Honda"), but not into miles ("2000mi", "2015k").
YEAR_RE = re.compile(r"\b(199\d|20[0-3]\d)(?:\b|(?=[A-Za-z])(?![kK]\b|mi))")


def year_from(text):
    m = YEAR_RE.search(text or "")
    return int(m.group(1)) if m else None


def to_int(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^\d]", "", str(v))
    return int(digits) if digits else None


def blank(source, id_, **kw):
    d = dict(source=source, id=str(id_), url=None, title=None, year=None, trim=None, price=None,
             mileage=None, vin=None, seller=None, private=False, city=None, lat=None, lon=None,
             zip=None, image_url=None, kbb=None, deal_indicator=None)
    d.update(kw)
    return d


def slugs(v):
    """A model's site name can be one string or a tuple of several (e.g. RX 350 and RX 450h)."""
    return (v,) if isinstance(v, str) else tuple(v)


def http():
    return requests.Session(impersonate="chrome")


# --- Cars.com ---------------------------------------------------------------

def carscom(cfg, models):
    s, q = cfg["search"], http()
    out, seen = [], set()
    band = (cfg["deals"]["market_price_min"], cfg["deals"]["market_price_max"])
    for mdl in models:
        for slug in slugs(mdl.carscom):
            out += _carscom_model(s, q, mdl, seen, band, slug)
    return out


CARSCOM_MAX_PAGES = 20


def _carscom_model(s, q, mdl, seen, market_band, slug):
    out = []
    # The site serves about 24 cars a page whatever page_size asks for, cheapest first.
    for page in range(1, CARSCOM_MAX_PAGES + 1):
        if page > 1:
            time.sleep(1)
        params = {
            "makes[]": mdl.make.lower(), "models[]": slug,
            "maximum_distance": s["radius_miles"], "zip": s["origin_zip"], "stock_type": "used",
            # Wider than the budget: the extra listings feed the deal-scoring price model.
            "list_price_min": market_band[0], "list_price_max": market_band[1],
            "year_min": s["min_year"] - 2, "mileage_max": s["max_mileage"] + 40000,
            "page_size": 100, "page": page, "sort": "list_price",
        }
        url = "https://www.cars.com/shopping/results/?" + urlencode(params)
        r = q.get(url, timeout=40)
        if r.status_code == 403:  # sometimes refuses one request and serves the next
            time.sleep(5)
            r = q.get(url, timeout=40)
        if r.status_code != 200:
            # One refused or unknown model must not take down the other models' searches. If every
            # model fails, the source returns nothing and is reported as failing.
            log.warning("cars.com: HTTP %s for %s page %d, skipping the rest of this model", r.status_code, slug, page)
            break
        blobs = re.findall(r'data-vehicle-details="([^"]+)"', r.text)
        new = 0
        for b in blobs:
            d = json.loads(htmlmod.unescape(b))
            lid = d.get("listingId")
            if not lid or lid in seen:
                continue
            seen.add(lid)
            new += 1
            seller = d.get("seller") or {}
            out.append(blank(
                "Cars.com", lid, url=f"https://www.cars.com/vehicledetail/{lid}/",
                title=f'{d.get("year")} {d.get("make")} {d.get("model")} {d.get("trim") or ""}'.strip(),
                year=to_int(d.get("year")), trim=d.get("trim"), price=to_int(d.get("price")),
                mileage=to_int(d.get("mileage")), vin=d.get("vin"),
                seller=seller.get("dealerName") or "Private seller",
                private=not seller.get("dealerName"), zip=seller.get("zip"),
                image_url=d.get("primaryThumbnail"), ref=r.url,
            ))
        if new == 0 or len(blobs) < 20:
            break
    return out


_HISTORY_LABELS = ("Title", "Accidents", "Owners", "Prior use", "Recalls")


def carscom_history_parse(page):
    """Vehicle history (AutoCheck) from a Cars.com listing page, as
    {"owners", "accidents", "title_status", "prior_use"} with whatever the page gives; {} when the
    listing has no history report.

    The page states it twice: as true/false fields in its tracking data, and as a "History" box
    (Title: Clean title, Accidents: No accidents or damage reported, Owners: 1 owner, Prior use: …).
    The box says more, so it is read first and the fields fill in what it leaves out."""
    data = htmlmod.unescape(page)

    def field(name):
        m = re.search(r'"%s"\s*:\s*(true|false)\b' % name, data)
        return None if not m else m.group(1) == "true"

    text = re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", page, flags=re.S)))
    box = re.search(r"Reported by (?:Experian )?AutoCheck (.{0,500})", text)
    box = re.split(r"Get the AutoCheck Report|Seller's notes", box.group(1))[0] if box else ""
    parts = re.split(r"\b(%s) " % "|".join(_HISTORY_LABELS), box)
    stats = {parts[i].lower(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if not stats and not re.search(r'"vhr_source"\s*:\s*"[a-z]', data):
        return {}
    d = {}
    owners = re.search(r"(\d+)\+? owner", stats.get("owners", ""), re.I)
    if owners:
        d["owners"] = "1" if owners.group(1) == "1" else "2+"
    elif field("single_owner") is not None:
        d["owners"] = "1" if field("single_owner") else "2+"
    acc = stats.get("accidents", "")
    count = re.search(r"(\d+) accident", acc, re.I)
    if re.match(r"no accident", acc, re.I) or (not acc and field("no_accidents")):
        d["accidents"] = 0
    elif count:
        d["accidents"] = int(count.group(1))
    elif acc or field("no_accidents") is False:
        d["accidents"] = "reported"  # stated without a count
    title = stats.get("title", "")
    if re.search(r"\bclean\b", title, re.I) or (not title and field("clean_title")):
        d["title_status"] = "Clean"
    elif title or field("clean_title") is False:
        brand = re.search(r"salvage|rebuilt|flood|lemon|junk|lien", title, re.I)
        d["title_status"] = brand.group(0).capitalize() if brand else "Branded"
    use = stats.get("prior use", "")
    if re.search(r"non-?personal|rental|fleet|commercial", use, re.I) or (not use and field("personal_use_only") is False):
        d["prior_use"] = "non-personal use"
    elif re.search(r"personal", use, re.I) or (not use and field("personal_use_only")):
        d["prior_use"] = "personal use"
    return d


def _carscom_blocked(status, text):
    return status != 200 or "Just a moment" in text[:4000] or "cf-chl" in text[:20000]


def _carscom_open(pg, url, ref):
    """Opens a listing page the way a shopper would and returns its HTML, or None if refused."""
    for attempt in range(2):
        if attempt:  # refused: look at the search results again, then retry once
            pg.goto(ref, timeout=60000, wait_until="domcontentloaded")
            pg.wait_for_timeout(4000)
        resp = pg.goto(url, timeout=60000, referer=ref, wait_until="domcontentloaded")
        if resp is not None and resp.status in (404, 410):
            return ""  # the listing is gone
        for _ in range(15):  # a "Just a moment..." check can clear by itself within a few seconds
            pg.wait_for_timeout(1000)
            text = pg.content()
            if "Just a moment" not in pg.title() and "cf-chl" not in text[:20000]:
                if resp is None or resp.status in (200, 403):  # 403 is the check page that then cleared
                    return text
    return None


def carscom_history(browser, todo, limit=60, delay=1.0):
    """Reads the vehicle history from Cars.com listing pages.

    todo is [(listing id, search page URL)]. Listing pages refuse plain requests (a Cloudflare
    check), so they are read in the stealth browser: first by asking for the page from inside a
    search results page, which is quick; if that is refused, by opening each listing. Stops after
    two refusals in a row so the search scraper isn't put at risk. Returns {id: data}; data is {}
    for listings without a history report."""
    out, refused, quick = {}, 0, True
    if not todo:
        return out
    pg = browser.page()
    try:
        pg.goto(todo[0][1], timeout=60000, wait_until="domcontentloaded")
        pg.wait_for_timeout(4000)
        for n, (lid, ref) in enumerate(todo[:limit]):
            url, text = f"https://www.cars.com/vehicledetail/{lid}/", None
            try:
                if quick:
                    status, body = pg.evaluate(
                        "async ([u, ref]) => { const r = await fetch(u, {referrer: ref, credentials: 'include'});"
                        " return [r.status, await r.text()] }", [url, ref])
                    if status in (404, 410):
                        text = ""  # the listing is gone
                    elif _carscom_blocked(status, body):
                        log.info("cars.com history: quick request refused (HTTP %s), opening pages instead", status)
                        quick = False
                    else:
                        text = body
                if text is None:
                    text = _carscom_open(pg, url, ref)
            except Exception as e:
                log.warning("cars.com history: %s failed: %s", lid, str(e)[:200])
            if text is None:
                refused += 1
                if refused >= 2:
                    log.warning("cars.com history: refused twice in a row after %d pages, stopping", len(out))
                    break
                continue
            refused = 0
            out[lid] = carscom_history_parse(text)
            pg.wait_for_timeout(int(delay * 1000))
    finally:
        pg.close()
    log.info("cars.com history: read %d listing pages (%s), %d with a history report",
             len(out), "quick requests" if quick else "opened one by one", sum(1 for d in out.values() if d))
    return out


# --- Craigslist ---------------------------------------------------------------

def craigslist(cfg, models):
    out, seen = [], set()
    for mdl in models:
        for x in _craigslist_model(cfg["search"], mdl, cfg["sources"].get("craigslist_site", "austin")):
            if x["id"] not in seen:
                seen.add(x["id"])
                out.append(x)
    return out


def _craigslist_model(s, mdl, site):
    params = {
        "batch": "15-0-360-0-0", "cc": "US", "lang": "en", "searchPath": "cta",
        "query": f"{mdl.make} {mdl.model}".lower(), "postal": s["origin_zip"], "search_distance": s["radius_miles"],
        "min_price": max(0, s["price_min"] - s["near_miss_margin"]),
        "max_price": s["price_max"] + s["near_miss_margin"],
    }
    r = http().get("https://sapi.craigslist.org/web/v8/postings/search/full?" + urlencode(params),
                   headers={"Referer": f"https://{site}.craigslist.org/"}, timeout=40)
    r.raise_for_status()
    data = r.json()["data"]
    dec = data["decode"]
    out = []
    for it in data["items"]:
        pid = dec["minPostingId"] + it[0]
        cat, price = it[2], it[3]
        loc = str(it[4])  # "locIdx:descIdx~lat~lon"
        parts = loc.split("~")
        lat = float(parts[1]) if len(parts) > 2 else None
        lon = float(parts[2]) if len(parts) > 2 else None
        idx = parts[0].split(":")
        desc_i = int(idx[1]) if len(idx) > 1 and idx[1].isdigit() else 0
        city = dec["locationDescriptions"][desc_i] if 0 < desc_i < len(dec["locationDescriptions"]) else None
        tagged = {x[0]: x[1:] for x in it if isinstance(x, list) and x and isinstance(x[0], int)}
        title = it[-1] if isinstance(it[-1], str) else ""
        token = tagged.get(13, [None])[0]
        slug = tagged.get(6, [""])[0]
        imgs = tagged.get(4, [])
        img = imgs[0].split(":", 1)[-1] if imgs else None
        out.append(blank(
            "Craigslist", pid,
            url=f"https://www.craigslist.org/view/d/{slug}/{token}" if token else None,
            title=title, year=year_from(title) or year_from(slug.replace("-", " ")), price=price,
            mileage=to_int(tagged.get(9, [None])[0]),
            seller="Dealer (Craigslist)" if cat == 146 else "Private seller", private=cat != 146,
            city=city.title() if city and not city.isdigit() else None,
            zip=city if city and city.isdigit() and len(city) == 5 else None,
            lat=lat, lon=lon,
            image_url=f"https://images.craigslist.org/{img}_600x450.jpg" if img else None,
        ))
    out = [x for x in out if mdl.matches(x["title"])]
    # Some titles leave out the year ("Honda CR-V for sale"); the listing page has it as a structured field.
    for x in [x for x in out if not x["year"] and x["url"]][:15]:
        try:
            page = http().get(x["url"], timeout=30)
            m = re.search(r"<title>(.*?)</title>", page.text, re.S)
            x["year"] = year_from(m.group(1)) if m else None
        except Exception as e:
            log.warning("craigslist year lookup failed for %s: %s", x["url"], e)
        time.sleep(1)
    return out


# --- Browser-based sources ------------------------------------------------------

class Browser:
    """One Camoufox instance shared by the browser-based scrapers."""

    def __enter__(self):
        from camoufox.sync_api import Camoufox
        self._cm = Camoufox(headless=True, humanize=True, os="windows")
        self.b = self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        self._cm.__exit__(*exc)

    def page(self):
        return self.b.new_page()


CARMAX_MAX_PAGES = 8


def carmax_search(pg, s, mdl, slug):
    """Every car of this model in a CarMax search, 100 at a time. pg is a page already on carmax.com.

    A slug can be a model ("jeep/wrangler-4xe") or just a make ("hyundai"), for models CarMax has no
    address for; a make search returns every model, so only cars matching the model are kept."""
    out = []
    for skip in range(0, 100 * CARMAX_MAX_PAGES, 100):
        api = (f'/cars/api/search/run?uri=/cars/{slug}'
               f'&skip={skip}&take=100&zipCode={s["origin_zip"]}&radius={s["radius_miles"]}&shipping=-1&sort=price-asc')
        status, body = pg.evaluate("async (u) => { const r = await fetch(u); return [r.status, await r.text()] }", api)
        if status != 200:
            raise RuntimeError(f"CarMax API returned HTTP {status} for {slug}")
        data = json.loads(body)
        page = data.get("items") or []
        out += [c for c in page if mdl.matches(f'{c.get("make")} {c.get("model")} {c.get("trim") or ""}')]
        pg.wait_for_timeout(800)
        if len(page) < 100 or skip + len(page) >= (data.get("totalCount") or 0):
            break
    return out


def carmax(cfg, models, browser):
    s = cfg["search"]
    items = []
    pg = browser.page()
    try:
        pg.goto("https://www.carmax.com/cars/toyota", timeout=60000)
        pg.wait_for_timeout(4000)
        for mdl in models:
            for slug in slugs(mdl.carmax):
                items += carmax_search(pg, s, mdl, slug)
    finally:
        pg.close()
    out, seen = [], set()
    for c in items:
        stock = c.get("stockNumber")
        if stock in seen:
            continue
        seen.add(stock)
        out.append(blank(
            "CarMax", stock, url=f"https://www.carmax.com/car/{stock}",
            title=f'{c.get("year")} {c.get("make")} {c.get("model")} {c.get("trim") or ""}'.strip(),
            year=c.get("year"), trim=c.get("trim"), price=to_int(c.get("basePrice")),
            mileage=to_int(c.get("mileage")), vin=c.get("vin"),
            seller=f'CarMax {c.get("storeName") or ""}'.strip(),
            city=f'{c.get("storeCity")}, {c.get("stateAbbreviation")}' if c.get("storeCity") else None,
            image_url=f"https://img2.carmax.com/assets/{stock}/hero.jpg?width=640&height=480",
            owners="1" if "singleOwner" in (c.get("highlights") or []) else None,
        ))
    return out


# Autotrader's bot check blocks some clients some days (it blocked the stealth browser for most of
# Oct 4–8 while plain requests got through). Kelley Blue Book is the same company and serves the
# same listings in the same page format, so each page is tried on these in order, starting with
# whichever worked last.
AUTOTRADER_HOSTS = [
    ("autotrader", "chrome", "https://www.autotrader.com/cars-for-sale/used-cars/{slug}?{q}"),
    ("autotrader", "safari", "https://www.autotrader.com/cars-for-sale/used-cars/{slug}?{q}"),
    ("kbb", "chrome", "https://www.kbb.com/cars-for-sale/used/{slug}?{q}"),
    ("kbb", "safari", "https://www.kbb.com/cars-for-sale/used/{slug}?{q}"),
]


def autotrader_page_data(text):
    """Listing data from an Autotrader or KBB results page: {"inventory": {...}, "owners": {...}}.

    Handles the streamed format the sites use now and the single JSON block they used before.
    Returns None for a page with neither (a bot-check or error page)."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if m:
        try:
            return json.loads(m.group(1))["props"]["pageProps"]["__eggsState"]
        except (ValueError, KeyError):
            pass
    return flight.page_data(text)


class _AutotraderClient:
    def __init__(self):
        self.order = list(range(len(AUTOTRADER_HOSTS)))
        self.sessions = {}
        self.unknown = set()

    def eggs(self, slug, params):
        """Returns the page's listing data from the first host that serves it.

        A slug can carry its own filters after a "?", e.g. "jeep/wrangler?fuelTypeGroup=PIH"."""
        path, _, extra = slug.partition("?")
        params = {**params, **dict(parse_qsl(extra))}
        errors = []
        for i in list(self.order):
            site, imp, tmpl = AUTOTRADER_HOSTS[i]
            q = self.sessions.setdefault(imp, requests.Session(impersonate=imp))
            try:
                r = q.get(tmpl.format(slug=path, q=urlencode(params)), timeout=40)
                t = re.search(r"<title>(.*?)</title>", r.text, re.S)
                title = t.group(1).strip() if t else ""
                eggs = autotrader_page_data(r.text) if r.status_code == 200 and "unavailable" not in title.lower() else None
                if eggs is not None:
                    if self.order[0] != i:
                        log.info("autotrader: switching to %s (%s)", site, imp)
                        self.order.remove(i)
                        self.order.insert(0, i)
                    # An unknown model is redirected to the make's page (every Jeep, say), which
                    # would look like a working search that finds almost nothing.
                    if f"/{path.split('/')[-1]}" not in str(r.url):
                        if slug not in self.unknown:
                            self.unknown.add(slug)
                            log.warning("autotrader: %s doesn't know the model %r (sent to %s)", site, path, str(r.url)[:100])
                        return {"inventory": {}, "owners": {}}
                    return eggs
                errors.append(f"{site}/{imp}: HTTP {r.status_code} {title[:40]}")
            except Exception as e:
                errors.append(f"{site}/{imp}: {e}"[:80])
            time.sleep(2)
        raise RuntimeError("Autotrader and KBB both blocked: " + "; ".join(errors))


def _name(v):
    """Autotrader gives trim and model as {"name": ...}; tolerate a plain string too."""
    return v.get("name") if isinstance(v, dict) else (v if isinstance(v, str) else None)


def _at_title(it):
    """A listing's title; some sponsored listings only fill in the long one."""
    return next((it[k] for k in ("title", "titleLong") if isinstance(it.get(k), str) and it[k].strip()), "")


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _autotrader_is(mdl, slug, it):
    """Whether an Autotrader listing is this model.

    Autotrader has no separate model for most plug-in hybrids: a RAV4 Prime is a "RAV4" whose fuel
    type is plug-in hybrid, and its title may just say "Toyota RAV4 SE". So a slug that filters by
    fuel (…?fuelTypeGroup=PIH) accepts any plug-in hybrid of the slug's model. Each listing is still
    checked, because the page mixes in sponsored cars that ignore the filters."""
    title = _at_title(it)
    if mdl.matches(" ".join(filter(None, [_name(it.get("model")), _name(it.get("trim")), title]))):
        return True
    path, _, extra = slug.partition("?")
    if "fuelTypeGroup=PIH" not in extra:
        return False
    fuel = it.get("fuelType")
    fuel = " ".join(str(x) for x in fuel.values()) if isinstance(fuel, dict) else str(fuel or "")
    base = _squash(path.split("/")[-1])
    return "plug" in fuel.lower() and (_squash(_name(it.get("model"))).startswith(base) or base in _squash(title))


def autotrader(cfg, models):
    s = cfg["search"]
    out, seen, client = [], set(), _AutotraderClient()
    for mdl in models:
        for slug in slugs(mdl.autotrader):
            out += _autotrader_model(s, mdl, client, seen, slug)
            time.sleep(1)
    return out


AUTOTRADER_MAX_PAGES = 8


def _autotrader_model(s, mdl, client, seen, slug, pages=AUTOTRADER_MAX_PAGES):
    out = []
    for first in range(0, 25 * pages, 25):
        params = {
            "zip": s["origin_zip"], "searchRadius": s["radius_miles"], "startYear": s["min_year"],
            "maxPrice": s["price_max"] + s["near_miss_margin"], "maxMileage": s["max_mileage"],
            "sortBy": "derivedpriceASC", "numRecords": 25, "firstRecord": first,
        }
        if first:
            time.sleep(2)
        eggs = client.eggs(slug, params)
        owners = eggs.get("owners") or {}
        new = 0
        for lid, it in (eggs.get("inventory") or {}).items():
            if lid in seen or not isinstance(it, dict) or it.get("listingType") == "NEW" or not _autotrader_is(mdl, slug, it):
                continue
            seen.add(lid)
            new += 1
            trim = _name(it.get("trim"))
            title = re.sub(r"^(?:Used|Certified|New)\s+", "", _at_title(it))
            if not mdl.matches(title):  # later steps identify the model from the title alone
                title = " ".join(str(p) for p in (it.get("year"), mdl.make, mdl.model, trim) if p)
            own = owners.get(str(it.get("ownerId"))) or (it.get("owner") if isinstance(it.get("owner"), dict) else {})
            addr = ((own.get("location") or {}).get("address") or {})
            images = it.get("images")
            srcs = (images.get("sources") if isinstance(images, dict) else images) or []
            pd = it.get("pricingDetail") if isinstance(it.get("pricingDetail"), dict) else {}
            miles = it.get("mileage")
            out.append(blank(
                "Autotrader", lid, url=f"https://www.autotrader.com/cars-for-sale/vehicle/{lid}",
                title=title, year=to_int(it.get("year")), trim=trim,
                price=to_int(pd.get("salePrice") or pd.get("displayPrice") or pd.get("incentive") or it.get("price")),
                mileage=to_int(miles.get("value") if isinstance(miles, dict) else miles), vin=it.get("vin"),
                seller=own.get("name") or it.get("ownerName"),
                private=bool(own.get("privateSeller")),
                city=f'{addr["city"]}, {addr.get("state", "")}' if addr.get("city") else None,
                zip=addr.get("zip"), lat=addr.get("latitude"), lon=addr.get("longitude"),
                image_url=srcs[0].get("src") if srcs and isinstance(srcs[0], dict) else None,
                kbb=to_int(pd.get("kbbFppAmount")) or None, deal_indicator=pd.get("dealIndicator"),  # 0 means KBB has no figure
            ))
        if new == 0:
            break
    return out


FB_MILES_RE = re.compile(r"([\d.,]+)\s*([Kk])?\s*miles", re.I)


def facebook(cfg, models, browser):
    s = cfg["search"]
    out, seen = [], set()
    pg = browser.page()
    try:
        for city, mdl in ((c, m) for c in cfg["sources"]["facebook_cities"] for m in models):
            url = (f"https://www.facebook.com/marketplace/{city}/search?"
                   f'minPrice={max(0, s["price_min"] - s["near_miss_margin"])}&maxPrice={s["price_max"] + s["near_miss_margin"]}'
                   f'&query={quote(mdl.make + " " + mdl.model)}&exact=false')
            pg.goto(url, timeout=60000)
            pg.wait_for_timeout(5000)
            for _ in range(2):
                pg.mouse.wheel(0, 2500)
                pg.wait_for_timeout(1500)
            cards = pg.eval_on_selector_all(
                "a[href*='/marketplace/item/']",
                "els => els.map(e => [e.href, e.innerText, (e.querySelector('img')||{}).src || null])",
            )
            for href, text, img in cards:
                m = re.search(r"/marketplace/item/(\d+)", href)
                if not m or m.group(1) in seen:
                    continue
                lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
                prices = [ln for ln in lines if ln.startswith("$") or ln.lower() == "free"]
                rest = [ln for ln in lines if ln not in prices]
                title = rest[0] if rest else ""
                if not mdl.matches(title):
                    continue
                seen.add(m.group(1))
                miles = None
                for ln in rest[1:]:
                    mm = FB_MILES_RE.search(ln)
                    if mm:
                        n = float(mm.group(1).replace(",", ""))
                        miles = int(n * 1000) if mm.group(2) else int(n)
                place = next((ln for ln in rest[1:] if "," in ln and not FB_MILES_RE.search(ln)), None)
                out.append(blank(
                    "Facebook", m.group(1), url=f"https://www.facebook.com/marketplace/item/{m.group(1)}/",
                    title=title, year=year_from(title), price=to_int(prices[0]) if prices else None,
                    mileage=miles, seller="Private seller", private=True, city=place, image_url=img,
                ))
    finally:
        pg.close()
    return out


HTTP_SOURCES = {"carscom": carscom, "craigslist": craigslist, "autotrader": autotrader}
BROWSER_SOURCES = {"carmax": carmax, "facebook": facebook}


FB_DRIVEN_RE = re.compile(r"Driven ([\d,]+) miles")
FB_DESC_MILES_RE = re.compile(r"\b(\d{2,3}(?:,\d{3})|\d{2,3}\s?[kK])\s*(?:miles|mi\b|original miles)", re.I)
FB_VIN_RE = re.compile(r"VIN:\s*([A-HJ-NPR-Z0-9]{17})")


FB_END_MARKERS = ("Location is approximate", "Today's picks", "See more on Facebook", "Seller information")


def fb_listing_text(text):
    """Keeps only the listing's own details and description from a Facebook page's text.

    The rest of the page is Facebook's feed of unrelated items, which must not be matched."""
    start = text.find("About this vehicle")
    if start < 0:
        start = text.find("Listed ")
    body = text[max(start, 0):]
    ends = [i for i in (body.find(m) for m in FB_END_MARKERS) if i > 0]
    return body[:min(ends)] if ends else body[:2500]


def facebook_details(browser, ids, limit=300):
    """Opens Facebook listing pages to get mileage, VIN and title status.

    Returns {id: {"mileage", "vin", "title_status", "text"}}; ids that fail are left out."""
    out = {}
    pg = browser.page()
    try:
        for fid in list(ids)[:limit]:
            try:
                pg.goto(f"https://www.facebook.com/marketplace/item/{fid}/", timeout=45000)
                pg.wait_for_timeout(2500)
                try:  # expand a truncated seller description
                    pg.get_by_text("See more", exact=True).first.click(timeout=1500)
                    pg.wait_for_timeout(500)
                except Exception:
                    pass
                text = pg.inner_text("body")
            except Exception:
                continue
            listed = text.find("Listed ")
            body = (text[listed:text.find("About this vehicle")] if listed >= 0 else "") + fb_listing_text(text)
            miles = None
            m = FB_DRIVEN_RE.search(body)
            if m:
                miles = int(m.group(1).replace(",", ""))
            else:
                m = FB_DESC_MILES_RE.search(body)
                if m:
                    v = m.group(1).replace(",", "").replace(" ", "")
                    miles = int(v[:-1]) * 1000 if v[-1] in "kK" else int(v)
            ts = re.search(r"\b(Clean|Rebuilt|Salvage|Lien) title\b", body, re.I)
            vin = FB_VIN_RE.search(body)
            out[fid] = {"mileage": miles, "vin": vin.group(1) if vin else None,
                        "title_status": ts.group(1).lower() if ts else None, "text": body[:3000]}
    finally:
        pg.close()
    return out
