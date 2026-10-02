"""Vehicles to search for, with per-site slugs and cargo volume by generation.

Cargo figures are manufacturer specs in cubic feet (approximate; trims vary slightly):
  max     = behind the front seats, rear seats folded or removed
  row2    = behind the 2nd row
  row3    = behind the 3rd row (None for two-row vehicles)
"""
import re
from dataclasses import dataclass, field


@dataclass
class Model:
    key: str
    make: str
    model: str
    body: str
    seats: str
    carscom: str
    autotrader: str
    carmax: str
    pattern: str
    # (first_year, last_year, max, row2, row3)
    cargo: list = field(default_factory=list)

    def matches(self, text):
        return re.search(self.pattern, text or "", re.I) is not None

    def cargo_for(self, year):
        for lo, hi, mx, r2, r3 in self.cargo:
            if year and lo <= year <= hi:
                return {"max": mx, "row2": r2, "row3": r3, "gen": f"{lo}–{hi}"}
        if self.cargo:
            # Unknown year: use the oldest generation in range as an estimate.
            lo, hi, mx, r2, r3 = self.cargo[0]
            return {"max": mx, "row2": r2, "row3": r3, "gen": f"{lo}–{hi}", "estimate": True}
        return None


MODELS = [
    Model("sienna", "Toyota", "Sienna", "Minivan", "7–8", "toyota-sienna", "toyota/sienna", "toyota/sienna",
          r"\bsienna\b", [(2011, 2020, 150.0, 87.1, 39.1), (2021, 2030, 101.0, 75.2, 33.5)]),
    Model("odyssey", "Honda", "Odyssey", "Minivan", "7–8", "honda-odyssey", "honda/odyssey", "honda/odyssey",
          r"\bodyssey\b", [(2011, 2017, 148.5, 93.1, 38.4), (2018, 2030, 155.8, 86.6, 32.8)]),
    Model("highlander", "Toyota", "Highlander", "3-row SUV", "7–8", "toyota-highlander", "toyota/highlander",
          "toyota/highlander", r"\bhighlander\b",
          [(2008, 2013, 95.4, 42.3, 10.3), (2014, 2019, 83.7, 42.3, 13.8), (2020, 2030, 84.3, 40.6, 16.0)]),
    Model("pilot", "Honda", "Pilot", "3-row SUV", "8", "honda-pilot", "honda/pilot", "honda/pilot",
          r"\bpilot\b", [(2009, 2015, 87.0, 47.7, 20.8), (2016, 2022, 83.9, 46.8, 16.5)]),
    Model("4runner", "Toyota", "4Runner", "SUV", "5 (7 opt.)", "toyota-4runner", "toyota/4runner",
          "toyota/4runner", r"\b4\s?-?runner\b", [(2010, 2024, 88.8, 47.2, 9.0)]),
    Model("rav4", "Toyota", "RAV4", "Compact SUV", "5", "toyota-rav4", "toyota/rav4", "toyota/rav4",
          r"\brav\s?-?4\b",
          [(2006, 2012, 73.0, 36.4, None), (2013, 2018, 73.4, 38.4, None), (2019, 2030, 69.8, 37.6, None)]),
    Model("crv", "Honda", "CR-V", "Compact SUV", "5", "honda-cr_v", "honda/cr-v", "honda/cr-v",
          r"\bcr\s?-?v\b",
          [(2007, 2011, 72.9, 35.7, None), (2012, 2016, 70.9, 37.2, None), (2017, 2022, 75.8, 39.2, None)]),
    Model("venza", "Toyota", "Venza", "Crossover wagon", "5", "toyota-venza", "toyota/venza", "toyota/venza",
          r"\bvenza\b", [(2009, 2015, 70.1, 30.7, None)]),
    Model("priusv", "Toyota", "Prius v", "Wagon", "5", "toyota-prius_v", "toyota/prius-v", "toyota/prius-v",
          r"\bprius\s?-?v\b", [(2012, 2017, 67.3, 34.3, None)]),
    Model("element", "Honda", "Element", "Boxy SUV", "4", "honda-element", "honda/element", "honda/element",
          r"\belement\b", [(2003, 2011, 74.6, 25.1, None)]),
]

BY_KEY = {m.key: m for m in MODELS}


def enabled(cfg):
    keys = cfg["search"].get("models") or list(BY_KEY)
    return [BY_KEY[k] for k in keys if k in BY_KEY]


def identify(text, models):
    """Returns the Model a listing title belongs to, or None."""
    # Check Prius v before anything that might loosely match.
    for m in sorted(models, key=lambda m: -len(m.pattern)):
        if m.matches(text):
            return m
    return None
