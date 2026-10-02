"""Reliability ratings from NHTSA owner complaints and recalls, plus known issues per generation.

Stars come from complaints per 10,000 vehicles sold for each model year, ranked against every
model year in the search (2011+ of all enabled models). Airbag complaints are left out because
most of them follow the Takata recall campaign rather than how a car holds up. Sales figures are
approximate average US sales per year, so treat the stars as a rough guide, not a precise score.
"""
import datetime as dt
import json
import logging

import numpy as np
from curl_cffi import requests

log = logging.getLogger("finder")
REFRESH_DAYS = 30
SKIP_COMPONENTS = {"AIR BAGS", "UNKNOWN OR OTHER"}

# Approximate average US sales per year over 2011–2020, used to normalize complaint counts.
SALES = {
    "sienna": 120_000, "odyssey": 115_000, "highlander": 170_000, "pilot": 140_000, "4runner": 100_000,
    "rav4": 330_000, "crv": 330_000, "venza": 40_000, "priusv": 25_000, "element": 15_000,
}
NHTSA_NAMES = {"crv": "CR-V", "priusv": "PRIUS V", "4runner": "4RUNNER"}

# Common problems reported for each generation (first year, last year, note).
KNOWN_ISSUES = {
    "sienna": [(2011, 2020, "Power sliding door cables and motors are the usual repair. 2011, the first year of this generation, drew the most complaints.")],
    "odyssey": [(2011, 2013, "The V6's cylinder deactivation (VCM) is linked to oil burning and fouled spark plugs. Check for misfires and oil use."),
                (2014, 2017, "Owners report transmission shudder (Honda issued a software update) and sliding-door problems.")],
    "highlander": [(2008, 2019, "Few widespread problems reported for this generation.")],
    "pilot": [(2011, 2015, "2011–2013 V6 cylinder deactivation (VCM) is linked to oil burning and misfires."),
              (2016, 2022, "2016–2018 fuel injector failures (Honda extended the warranty) and rough 9-speed shifting on upper trims.")],
    "4runner": [(2010, 2024, "Very few widespread problems; known for lasting a long time.")],
    "rav4": [(2013, 2018, "Few widespread problems reported for this generation."),
             (2019, 2030, "Some 2019 fuel tanks won't fill completely; early 8-speed shift complaints.")],
    "crv": [(2012, 2016, "A/C compressor and condenser failures are common; 2015–2016 owners report vibration at idle."),
            (2017, 2022, "The 1.5L turbo can dilute oil with fuel (mostly in cold climates); A/C condenser leaks.")],
    "venza": [(2009, 2015, "Few widespread problems reported.")],
    "priusv": [(2012, 2017, "An aging hybrid battery is the main cost risk; the 1.8L hybrid engine can clog its EGR system and fail a head gasket at high mileage.")],
    "element": [(2003, 2011, "Few widespread problems reported.")],
}

LABELS = {5: "Excellent", 4: "Above average", 3: "Average", 2: "Below average", 1: "Poor"}


class Reliability:
    def __init__(self, db):
        self.db = db
        db.execute("""CREATE TABLE IF NOT EXISTS reliability (
            model_key TEXT, year INTEGER, complaints INTEGER, counted INTEGER, recalls INTEGER,
            components TEXT, fetched TEXT, PRIMARY KEY (model_key, year))""")
        self._cuts = None

    def refresh(self, models, first_year, last_year):
        """Fetches NHTSA data for model years not fetched in the last REFRESH_DAYS days."""
        http = requests.Session(impersonate="chrome")
        stale = (dt.date.today() - dt.timedelta(days=REFRESH_DAYS)).isoformat()
        fresh = {(m, y) for m, y in self.db.execute("SELECT model_key, year FROM reliability WHERE fetched >= ?", (stale,))}
        n = 0
        for mdl in models:
            name = NHTSA_NAMES.get(mdl.key, mdl.model.upper())
            for year in range(first_year, last_year + 1):
                if (mdl.key, year) in fresh or not any(lo <= year <= hi for lo, hi, *_ in mdl.cargo):
                    continue
                try:
                    params = {"make": mdl.make, "model": name, "modelYear": year}
                    comp = http.get("https://api.nhtsa.gov/complaints/complaintsByVehicle", params=params, timeout=30).json()
                    rec = http.get("https://api.nhtsa.gov/recalls/recallsByVehicle", params=params, timeout=30).json()
                except Exception as e:
                    log.warning("NHTSA lookup failed for %s %s: %s", year, mdl.model, e)
                    continue
                counts, counted = {}, 0
                for c in comp.get("results", []):
                    parts = {p.split(":")[0].strip() for p in (c.get("components") or "").split(",") if p.strip()}
                    if parts and parts <= SKIP_COMPONENTS:
                        continue
                    counted += 1
                    for p in parts - SKIP_COMPONENTS:
                        counts[p] = counts.get(p, 0) + 1
                top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
                self.db.execute("INSERT OR REPLACE INTO reliability VALUES (?,?,?,?,?,?,?)",
                                (mdl.key, year, comp.get("count", 0), counted, rec.get("Count", 0),
                                 json.dumps(top), dt.date.today().isoformat()))
                n += 1
        self.db.commit()
        self._cuts = None
        if n:
            log.info("reliability: fetched NHTSA data for %d model years", n)

    def _rate(self, model_key, counted):
        return counted / (SALES.get(model_key, 100_000) / 10_000)

    def cuts(self):
        """Complaint-rate quintile boundaries across all stored model years."""
        if self._cuts is None:
            rates = [self._rate(m, c) for m, c in self.db.execute("SELECT model_key, counted FROM reliability")]
            self._cuts = list(np.percentile(rates, [20, 40, 60, 80])) if len(rates) >= 10 else None
        return self._cuts

    def rating(self, model_key, year):
        issues = next((t for lo, hi, t in KNOWN_ISSUES.get(model_key, []) if year and lo <= year <= hi), None)
        row = self.db.execute("SELECT counted, recalls, components FROM reliability WHERE model_key=? AND year=?",
                              (model_key, year)).fetchone() if year else None
        if not row or not self.cuts():
            return {"stars": None, "issues": issues} if issues else None
        counted, recalls, comps = row
        rate = self._rate(model_key, counted)
        stars = 5 - int(sum(rate > c for c in self.cuts()))
        return {"stars": stars, "label": LABELS[stars], "complaints": counted, "per10k": round(float(rate), 1),
                "recalls": recalls, "components": [c.title() for c, _ in json.loads(comps)[:3]], "issues": issues}
