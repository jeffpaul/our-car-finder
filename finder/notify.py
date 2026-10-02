"""Push notifications through ntfy (https://ntfy.sh)."""
import logging

from curl_cffi import requests

log = logging.getLogger("finder")
MAX_INDIVIDUAL = 8


def fmt_money(n):
    return f"${n:,.0f}" if n else "?"


def describe(x):
    bits = [fmt_money(x["price"])]
    bits.append(f'{x["mileage"] / 1000:.0f}k mi' if x.get("mileage") else "miles unknown")
    where = x.get("city") or "location unknown"
    if x.get("drive_mi"):
        where += f' ({x["drive_mi"]} mi away)'
    bits.append(where)
    if x.get("cargo"):
        bits.append(f'cargo {x["cargo"]["max"]:.0f} cu ft')
    line = " · ".join(bits)
    d = x.get("deal")
    if d and d.get("expected") and d["label"] in ("great", "good"):
        line += f'\n{d["label"].title()} deal: {fmt_money(d["delta"])} under {d["basis"]} ({fmt_money(d["expected"])})'

    extra = f'{x.get("seller") or ""} via {", ".join(x["links"])}'
    if x.get("flags"):
        extra += f' — mentions: {", ".join(x["flags"])}'
    return f"{line}\n{extra}"


def headline(x):
    return " ".join(str(p) for p in (x.get("year") or "", x["make"], x["model"], x.get("trim") or "") if p).strip()


class Notifier:
    def __init__(self, cfg, enabled=True):
        n = cfg["ntfy"]
        self.url = f'{n["server"].rstrip("/")}/{n["topic"]}'
        self.report_url = cfg.get("publish", {}).get("page_url") or None
        self.enabled = enabled and bool(n.get("topic"))
        self.http = requests.Session()

    def send(self, title, body, click=None, tags=None, attach=None, priority=None, report_button=True):
        if not self.enabled:
            log.info("[notify disabled] %s | %s", title, body.replace("\n", " | "))
            return
        headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore")}
        if click:
            headers["Click"] = click
        if tags:
            headers["Tags"] = tags
        if attach:
            headers["Attach"] = attach
        if priority:
            headers["Priority"] = str(priority)
        if report_button and self.report_url:
            headers["Actions"] = f"view, Open report, {self.report_url}"
        try:
            r = self.http.post(self.url, data=body.encode("utf-8"), headers=headers, timeout=20)
            r.raise_for_status()
        except Exception as e:
            log.error("ntfy send failed: %s", e)

    def new_listings(self, items):
        rank = {"great": 0, "good": 1}
        items = sorted(items, key=lambda x: (rank.get((x.get("deal") or {}).get("label"), 2),
                                             x["tier"] != "match", x.get("drive_mi") or 999))
        for x in items[:MAX_INDIVIDUAL]:
            great = (x.get("deal") or {}).get("label") == "great"
            if great:
                label, tag, prio = "Great deal", "fire", 5
            elif x["tier"] == "match":
                label, tag, prio = "New match", "red_car", 4
            else:
                label, tag, prio = "New near-miss", "small_orange_diamond", 3
            self.send(f"{label}: {headline(x)}", describe(x), click=next(iter(x["links"].values()), None),
                      tags=tag, attach=x.get("image_url"), priority=prio)
        rest = items[MAX_INDIVIDUAL:]
        if rest:
            lines = [f'{headline(x)} · {fmt_money(x["price"])} · {x.get("city") or "?"}' for x in rest[:20]]
            self.send(f"{len(rest)} more new listings", "\n".join(lines), tags="red_car")

    def price_drops(self, drops):
        for x, old_price in drops[:MAX_INDIVIDUAL]:
            self.send(f"Price drop: {headline(x)}",
                      f'{fmt_money(old_price)} → {describe(x)}',
                      click=next(iter(x["links"].values()), None), tags="chart_with_downwards_trend",
                      attach=x.get("image_url"))

    def first_run(self, items):
        items = list(items)
        matches = sum(1 for x in items if x["tier"] == "match")
        self.send("Minivan finder is running",
                  f"First scan found {matches} matches and {len(items) - matches} near misses. "
                  "From now on you'll get a notification for each new listing and price drop.",
                  tags="white_check_mark")

    def source_failures(self, rows):
        body = "\n".join(f"{src}: failed {n} runs in a row ({err})" for src, n, err in rows)
        self.send("Some car sites are failing", body, tags="warning", report_button=False)
