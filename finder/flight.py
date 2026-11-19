"""Reads listing data out of a Next.js "app router" page.

Autotrader and KBB used to put everything in one <script id="__NEXT_DATA__"> JSON block. They now
stream it as  self.__next_f.push([1, "<escaped text>"])  calls. Joined together, that text is a
list of rows ("<hex id>:<json>"), where a value that appears more than once can be replaced by a
"$<row id>" reference. Rather than depend on where the listings sit in that tree, this finds every
JSON object that looks like a listing: a 17-character VIN plus a price, title or year.
"""
import json
import re

_DEC = json.JSONDecoder()
_PUSH = "self.__next_f.push([1,"
_VIN = re.compile(r'"vin":\s*"[A-HJ-NPR-Z0-9]{17}"')
_ROW = re.compile(r"(?m)^([0-9a-f]{1,6}):")
_REF = re.compile(r"\$([0-9a-f]{1,6})")


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


def deref(v, table, depth=4):
    """Replaces "$id" references with the rows they point at, a few levels deep."""
    if depth < 0:
        return v
    if isinstance(v, str):
        m = _REF.fullmatch(v)
        return deref(table[m.group(1)], table, depth - 1) if m and m.group(1) in table else v
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


def page_data(page):
    """{"inventory": {listing id: listing}, "owners": {...}} for a search results page, or None
    when the page carries no streamed data at all (a block page, or the old format)."""
    text = flight_text(page)
    if not text:
        return None
    table = rows(text)
    inventory = {}
    for raw in listing_objects(text):
        it = deref(raw, table, 3)
        it.setdefault("vin", _own_vin(raw))
        lid = str(it.get("id") or it.get("listingId") or it["vin"])
        if lid not in inventory or len(it) > len(inventory[lid]):
            inventory[lid] = it
    return {"inventory": inventory, "owners": owners(text, table)}
