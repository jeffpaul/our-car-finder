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
    "rav4": 330_000, "crv": 330_000, "venza": 40_000, "priusv": 25_000, "prius": 140_000, "element": 15_000,
    "rx": 100_000, "gx": 25_000, "mdx": 55_000, "rdx": 45_000,
    # Plug-in hybrids: rough average US sales per year since launch. These are small numbers, so one
    # recall campaign can swing the complaint rate; read the stars as a hint only.
    "wrangler4xe": 170_000, "grandcherokee4xe": 30_000, "outlanderphev": 8_000, "tucsonphev": 10_000,
    "rav4prime": 25_000,
}
# NHTSA files complaints under one name per variant; the RX uses the RX 350, its main version.
# A tuple means NHTSA uses different names in different years; they are all asked for and merged.
NHTSA_NAMES = {"crv": "CR-V", "priusv": "PRIUS V", "4runner": "4RUNNER", "rx": "RX350", "gx": "GX460",
               # NHTSA has no separate Wrangler 4xe: its complaints are filed with every other Wrangler,
               # so that model's stars describe the Wrangler as a whole (and SALES above counts them all).
               "wrangler4xe": "WRANGLER",
               "grandcherokee4xe": ("GRAND CHEROKEE PHEV", "GRAND CHEROKEE 4XE"),
               "outlanderphev": "OUTLANDER PHEV", "tucsonphev": "TUCSON PLUG-IN HYBRID", "rav4prime": "RAV4 PRIME"}

# Common problems reported for each generation (first year, last year, note).
KNOWN_ISSUES = {
    "sienna": [(2011, 2020, "Power sliding door cables and motors are the usual repair. 2011, the first year of this generation, drew the most complaints.")],
    "odyssey": [(2011, 2013, "The V6's cylinder deactivation (VCM) is linked to oil burning and fouled spark plugs. Check for misfires and oil use."),
                (2014, 2017, "Owners report transmission shudder (Honda issued a software update) and sliding-door problems.")],
    "highlander": [(2008, 2019, "Few widespread problems reported for this generation.")],
    "pilot": [(2011, 2015, "2011–2013 V6 cylinder deactivation (VCM) is linked to oil burning and misfires."),
              (2016, 2022, "2016–2018 fuel injector failures (Honda extended the warranty) and rough 9-speed shifting on upper trims.")],
    "4runner": [(2010, 2024, "Very few widespread problems; known for lasting a long time.")],
    "rav4": [(2006, 2012, "The 2.5L 4-cylinder is durable. A clunk in the steering shaft is a common complaint; make sure the rear seat-belt recall (16V-096) was done."),
             (2013, 2018, "Few widespread problems reported for this generation."),
             (2019, 2030, "Some 2019 fuel tanks won't fill completely; early 8-speed shift complaints.")],
    "crv": [(2007, 2011, "A/C compressor failures are common; a brief rattle on cold starts points to a worn VTC actuator."),
            (2012, 2016, "A/C compressor and condenser failures are common; 2015–2016 owners report vibration at idle."),
            (2017, 2022, "The 1.5L turbo can dilute oil with fuel (mostly in cold climates); A/C condenser leaks.")],
    "venza": [(2009, 2015, "Few widespread problems reported.")],
    "priusv": [(2012, 2017, "An aging hybrid battery is the main cost risk; the 1.8L hybrid engine can clog its EGR system and fail a head gasket at high mileage.")],
    "prius": [(2010, 2015, "An aging hybrid battery is the main cost risk; the 1.8L engine can clog its EGR system and fail a head gasket at high mileage."),
              (2016, 2022, "Few widespread problems reported for this generation.")],
    "element": [(2003, 2011, "Few widespread problems reported.")],
    "rx": [(2010, 2015, "Dashboards can turn sticky or crack in Texas heat; otherwise few widespread problems. Same V6 as the Highlander."),
           (2016, 2022, "Few widespread problems reported.")],
    "gx": [(2010, 2023, "Very few widespread problems; built on the 4Runner's frame and V8.")],
    "mdx": [(2007, 2013, "Generally solid. The V6 timing belt is due about every 105k miles, so ask for proof it was done."),
            (2014, 2020, "2016+ 9-speed transmission draws rough-shifting complaints. Timing belt is due about every 105k miles.")],
    "rdx": [(2007, 2012, "The turbo 4-cylinder can need turbo work and burn oil at high mileage."),
            (2013, 2018, "V6 cylinder deactivation is linked to oil burning and misfires; timing belt is due about every 105k miles.")],
    "wrangler4xe": [(2021, 2030, "The high-voltage battery has been recalled for fire risk; check the VIN at nhtsa.gov/recalls before buying. In cold weather the engine can run on its own to refresh fuel and oil, which cuts electric driving in winter.")],
    "grandcherokee4xe": [(2022, 2030, "High-voltage battery and loss-of-power recalls; check the VIN at nhtsa.gov/recalls. Owners also report software and infotainment glitches. Many were rental cars.")],
    "outlanderphev": [(2023, 2030, "Few widespread problems reported so far for this generation.")],
    "tucsonphev": [(2022, 2030, "Few widespread problems reported so far; check the VIN for open recalls.")],
    "rav4prime": [(2021, 2030, "Few widespread problems. 2021 models had a recall for the hybrid system shutting down in very cold weather; confirm it was done.")],
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
        # Counts fetched under other model names are wrong, so start over when the names change.
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        names = json.dumps(sorted((m.key, NHTSA_NAMES.get(m.key, m.model.upper())) for m in models))
        if self.db.execute("SELECT v FROM meta WHERE k='nhtsa_names'").fetchone() != (names,):
            self.db.execute("DELETE FROM reliability")
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('nhtsa_names', ?)", (names,))
        stale = (dt.date.today() - dt.timedelta(days=REFRESH_DAYS)).isoformat()
        fresh = {(m, y) for m, y in self.db.execute("SELECT model_key, year FROM reliability WHERE fetched >= ?", (stale,))}
        n = 0
        for mdl in models:
            names = NHTSA_NAMES.get(mdl.key, mdl.model.upper())
            names = (names,) if isinstance(names, str) else names
            for year in range(first_year, last_year + 1):
                if (mdl.key, year) in fresh or not any(lo <= year <= hi for lo, hi, *_ in mdl.cargo):
                    continue
                complaints, recalls = {}, {}
                try:
                    for name in names:
                        params = {"make": mdl.make, "model": name, "modelYear": year}
                        comp = http.get("https://api.nhtsa.gov/complaints/complaintsByVehicle", params=params, timeout=30).json()
                        rec = http.get("https://api.nhtsa.gov/recalls/recallsByVehicle", params=params, timeout=30).json()
                        # Keyed by NHTSA's own ids, so a record returned under two names counts once.
                        for i, c in enumerate(comp.get("results", [])):
                            complaints[c.get("odiNumber") or (name, i)] = c
                        for i, r in enumerate(rec.get("results", [])):
                            recalls[r.get("NHTSACampaignNumber") or (name, i)] = r
                except Exception as e:
                    log.warning("NHTSA lookup failed for %s %s: %s", year, mdl.model, e)
                    continue
                counts, counted = {}, 0
                for c in complaints.values():
                    parts = {p.split(":")[0].strip() for p in (c.get("components") or "").split(",") if p.strip()}
                    if parts and parts <= SKIP_COMPONENTS:
                        continue
                    counted += 1
                    for p in parts - SKIP_COMPONENTS:
                        counts[p] = counts.get(p, 0) + 1
                top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
                self.db.execute("INSERT OR REPLACE INTO reliability VALUES (?,?,?,?,?,?,?)",
                                (mdl.key, year, len(complaints), counted, len(recalls),
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
