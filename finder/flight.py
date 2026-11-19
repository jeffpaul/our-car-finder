"""Reads listing data out of a Next.js "app router" page.

Autotrader and KBB used to put everything in one <script id="__NEXT_DATA__"> JSON block. They now
stream it as  self.__next_f.push([1, "<escaped text>"])  calls. Joined together, that text is a
list of rows ("<hex id>:<json>"), where a value that appears more than once can be replaced by a
"$<row id>" or "$<row id>:<key>:<key>" reference (the second form points inside a row). A row is
often a page element written as ["$", type, key, props], and a reference reaches its props by name.

Autotrader keeps its search results in a "pageData" object with "inventory" and "owners" maps, and
repeats listings elsewhere as copies whose nested values point back into it. If pageData ever
moves, page_data() falls back to finding every JSON object that looks like a listing: a
17-character VIN plus a price, title or year.
"""
import json
import re

_DEC = json.JSONDecoder()
_PUSH = "self.__next_f.push([1,"
_VIN = re.compile(r'"vin":\s*"[A-HJ-NPR-Z0-9]{17}"')
_ROW = re.compile(r"(?m)^([0-9a-f]{1,6}):")
_REF = re.compile(r"\$([0-9a-f]{1,6})((?::[^:]+)*)")


def flight_text(page):
    """The page's streamed data as one string ("" when the page has none)."""
    out, i = [], page.find(_PUSH)
    while i >= 0:
        try:
            chunk, end = _DEC.raw_decode(page, i + len(_PUSH))
            if isinstance(chunk, str):
                out.append(chunk)
        except ValueError:
            end = i + len(_PUSH)
        i = page.find(_PUSH, end)
    return "".join(out)


def rows(text):
    """{row id: value} for the rows that hold plain JSON, used to follow "$id" references."""
    out = {}
    for m in _ROW.finditer(text):
        try:
            out[m.group(1)], _ = _DEC.raw_decode(text, m.end())
        except ValueError:
            pass
    return out


_ELEMENT = {"type": 1, "key": 2, "props": 3}


def _is_element(v):
    """A page element, written as ["$", type, key, props]."""
    return isinstance(v, list) and len(v) == 4 and v[0] == "$" and isinstance(v[3], dict)


def _follow(ref, table):
    """What a "$id" or "$id:key:key" reference points at; the reference itself if it leads nowhere."""
    m = _REF.fullmatch(ref)
    if not m or m.group(1) not in table:
        return ref
    v = table[m.group(1)]
    for key in m.group(2).split(":")[1:]:
        if _is_element(v) and key in _ELEMENT:
            v = v[_ELEMENT[key]]
        elif isinstance(v, dict) and key in v:
            v = v[key]
        elif isinstance(v, list) and key.isdigit() and int(key) < len(v):
            v = v[int(key)]
        else:
            return ref
    return v


def deref(v, table, depth=4):
    """Replaces references with what they point at, a few levels deep."""
    if depth < 0:
        return v
    if isinstance(v, str):
        if not v.startswith("$"):
            return v
        if v.startswith("$$"):  # a plain string that begins with a dollar sign, e.g. "$$25,000"
            return v[1:]
        if v == "$undefined":
            return None
        target = _follow(v, table)
        return target if target is v else deref(target, table, depth - 1)
    if isinstance(v, dict):
        return {k: deref(x, table, depth - 1) for k, x in v.items()}
    if isinstance(v, list):
        return [deref(x, table, depth - 1) for x in v]
    return v


_LISTING_KEYS = ("pricingDetail", "title", "year", "price", "mileage")


def _own_vin(obj):
    """A listing's VIN: its own "vin", or one held by a directly nested object."""
    if isinstance(obj.get("vin"), str):
        return obj["vin"]
    for v in obj.values():
        if isinstance(v, dict) and isinstance(v.get("vin"), str):
            return v["vin"]
    return None


def _is_listing(obj):
    return isinstance(obj, dict) and any(k in obj for k in _LISTING_KEYS) and _own_vin(obj) is not None


def enclosing_object(text, pos, wanted, tries=400):
    """The innermost JSON object around text[pos] that `wanted` accepts, as (object, end)."""
    start = pos
    for _ in range(tries):
        start = text.rfind("{", 0, start)
        if start < 0:
            break
        try:
            obj, end = _DEC.raw_decode(text, start)
        except ValueError:
            continue
        if end > pos and wanted(obj):
            return obj, end
    return None, pos


def listing_objects(text):
    """Every listing-like object in the data (one that has a VIN plus price, title or year)."""
    out, done = [], -1
    for m in _VIN.finditer(text):
        if m.start() < done:
            continue
        obj, end = enclosing_object(text, m.start(), _is_listing)
        if obj is not None:
            out.append(obj)
            done = end
    return out


def owners(text, table):
    """The {owner id: seller details} map the listings point into, when the page has one."""
    out = {}
    for m in re.finditer(r'"owners":\s*', text):
        try:
            val, _ = _DEC.raw_decode(text, m.end())
        except ValueError:
            continue
        val = deref(val, table, 3)
        if isinstance(val, dict):
            out.update({str(k): v for k, v in val.items() if isinstance(v, dict)})
    return out


_LOOSE = re.compile(r"\$(?:undefined|[0-9a-f]{1,6}(?::[^:]+)+)")


def loose(v):
    """How many references are left in a value (ones that led nowhere)."""
    if isinstance(v, str):
        return 1 if _LOOSE.fullmatch(v) else 0
    if isinstance(v, dict):
        return sum(loose(x) for x in v.values())
    if isinstance(v, list):
        return sum(loose(x) for x in v)
    return 0


def scrub(v):
    """Replaces references that led nowhere with None, so they are never mistaken for values."""
    if isinstance(v, str):
        return None if _LOOSE.fullmatch(v) else v
    if isinstance(v, dict):
        return {k: scrub(x) for k, x in v.items()}
    if isinstance(v, list):
        return [scrub(x) for x in v]
    return v


def page_data_objects(v, table, depth=8):
    """Every object a few levels into a row that holds an "inventory" map of listings."""
    if depth < 0:
        return
    if isinstance(v, dict):
        inv = deref(v.get("inventory"), table, 0)
        if isinstance(inv, dict) and any(isinstance(deref(x, table, 0), dict) for x in inv.values()):
            yield v
            return
        v = list(v.values())
    if isinstance(v, list):
        for x in v:
            if isinstance(x, (dict, list)):
                yield from page_data_objects(x, table, depth - 1)


_BY_ID = re.compile(r"\$[0-9a-f]{1,6}(?::[^:]+)*?:(inventory|owners):([^:]+)((?::[^:]+)*)")


def repair(v, pools, depth=6):
    """Second way to settle a reference like "$b:…:inventory:<id>:mileage" that led nowhere: look
    the listing (or owner) up by its id among the full copies found anywhere on the page."""
    if isinstance(v, str):
        m = _BY_ID.fullmatch(v)
        target = pools.get(m.group(1), {}).get(m.group(2)) if m else None
        for key in m.group(3).split(":")[1:] if m else ():
            if isinstance(target, dict) and key in target:
                target = target[key]
            elif isinstance(target, list) and key.isdigit() and int(key) < len(target):
                target = target[int(key)]
            else:
                target = None
        if target is None or (isinstance(target, str) and _LOOSE.fullmatch(target)):
            return v
        return repair(target, pools, depth - 1) if depth > 0 else target
    if isinstance(v, dict):
        return {k: repair(x, pools, depth) for k, x in v.items()}
    if isinstance(v, list):
        return [repair(x, pools, depth) for x in v]
    return v


def _scanned(text, table):
    """{listing id: listing} from every listing-like object on the page, fullest copy of each."""
    inventory, rank = {}, {}
    for raw in listing_objects(text):
        it = deref(raw, table, 5)
        it.setdefault("vin", _own_vin(raw))
        lid = str(it.get("id") or it.get("listingId") or it["vin"])
        r = (loose(it), -len(it))
        if lid not in inventory or r < rank[lid]:
            inventory[lid], rank[lid] = it, r
    return inventory


def page_data(page):
    """{"inventory": {listing id: listing}, "owners": {...}} for a search results page, or None
    when the page carries no streamed data at all (a block page, or the old format)."""
    text = flight_text(page)
    if not text:
        return None
    table = rows(text)
    best = None
    for data in (d for row in table.values() for d in page_data_objects(row, table)):
        inv = deref(deref(data["inventory"], table, 0), table, 5)
        inv = {str(k): v for k, v in inv.items() if isinstance(v, dict)}
        # Prefer the fullest map with the fewest references that lead nowhere.
        r = (loose(inv) / max(len(inv), 1), "owners" not in data, -len(inv))
        if best is None or r < best[0]:
            best = (r, inv, data)
    if best:
        own = deref(deref(best[2].get("owners"), table, 0), table, 5)
        inv, own = best[1], {str(k): v for k, v in own.items()} if isinstance(own, dict) else {}
        own = own or owners(text, table)
    else:
        inv, own = _scanned(text, table), owners(text, table)
    if loose(inv) or loose(own):
        pools = {"inventory": {**_scanned(text, table), **{k: v for k, v in inv.items() if not loose(v)}} if best else inv,
                 "owners": {**owners(text, table), **{k: v for k, v in own.items() if not loose(v)}}}
        inv, own = repair(inv, pools), repair(own, pools)
    return {"inventory": scrub(inv), "owners": scrub(own)}
