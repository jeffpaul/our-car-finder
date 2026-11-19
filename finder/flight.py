"""Reads listing data out of a Next.js "app router" page.

Autotrader and KBB used to put everything in one <script id="__NEXT_DATA__"> JSON block. They now
stream it as  self.__next_f.push([1, "<escaped text>"])  calls. Joined together, that text is a
list of rows ("<hex id>:<json>"), where a value that appears more than once can be replaced by a
"$<row id>" or "$<row id>:<key>:<key>" reference (the second form points inside a row).

Autotrader keeps its search results under props.pageData in one of those rows. If that ever moves,
page_data() falls back to finding every JSON object that looks like a listing: a 17-character VIN
plus a price, title or year.
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


def _follow(ref, table):
    """What a "$id" or "$id:key:key" reference points at; the reference itself if it leads nowhere."""
    m = _REF.fullmatch(ref)
    if not m or m.group(1) not in table:
        return ref
    v = table[m.group(1)]
    for key in m.group(2).split(":")[1:]:
        if isinstance(v, dict) and key in v:
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


def page_data(page):
    """{"inventory": {listing id: listing}, "owners": {...}} for a search results page, or None
    when the page carries no streamed data at all (a block page, or the old format)."""
    text = flight_text(page)
    if not text:
        return None
    table = rows(text)
    for row in table.values():
        try:
            data = row["props"]["pageData"]
            inv = deref(data["inventory"], table, 1)
        except (KeyError, TypeError):
            continue
        if isinstance(inv, dict):
            own = deref(data.get("owners"), table, 1)
            return {"inventory": {str(k): deref(v, table, 4) for k, v in inv.items() if isinstance(deref(v, table, 0), dict)},
                    "owners": {str(k): deref(v, table, 4) for k, v in own.items()} if isinstance(own, dict) else {}}
    inventory = {}
    for raw in listing_objects(text):
        it = deref(raw, table, 4)
        it.setdefault("vin", _own_vin(raw))
        lid = str(it.get("id") or it.get("listingId") or it["vin"])
        if lid not in inventory or len(it) > len(inventory[lid]):
            inventory[lid] = it
    return {"inventory": inventory, "owners": owners(text, table)}
