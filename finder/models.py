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
    Model("prius", "Toyota", "Prius", "Hatchback", "5", "toyota-prius", "toyota/prius", "toyota/prius",
          r"\bprius\b(?!\s?-?(?:v|c|prime|plug-?in)\b)",
          [(2010, 2015, 39.6, 21.6, None), (2016, 2022, 65.5, 24.6, None)]),
    Model("element", "Honda", "Element", "Boxy SUV", "4", "honda-element", "honda/element", "honda/element",
          r"\belement\b", [(2003, 2011, 74.6, 25.1, None)]),
    Model("rx", "Lexus", "RX", "Luxury midsize SUV", "5", ("lexus-rx_350", "lexus-rx_450h"),
          ("lexus/rx-350", "lexus/rx-450h"), ("lexus/rx-350", "lexus/rx-450h"),
          r"\brx\s?-?(?:350|450h?)\b|\blexus\s+rx\b",
          [(2010, 2015, 80.3, 40.0, None), (2016, 2022, 56.3, 18.4, None)]),
    Model("gx", "Lexus", "GX 460", "Luxury 3-row SUV", "7–8", "lexus-gx_460", "lexus/gx-460", "lexus/gx-460",
          r"\bgx\s?-?460\b|\blexus\s+gx\b", [(2010, 2023, 64.7, 46.7, 11.6)]),
    Model("mdx", "Acura", "MDX", "Luxury 3-row SUV", "7", "acura-mdx", "acura/mdx", "acura/mdx",
          r"\bmdx\b", [(2007, 2013, 83.5, 42.9, 15.0), (2014, 2020, 90.9, 43.4, 15.8)]),
    Model("rdx", "Acura", "RDX", "Luxury compact SUV", "5", "acura-rdx", "acura/rdx", "acura/rdx",
          r"\brdx\b", [(2007, 2012, 60.6, 27.8, None), (2013, 2018, 61.3, 26.1, None)]),

    # Plug-in hybrid SUVs. Each site lists these as their own model, separate from the gas version,
    # so the slugs and patterns below must never match a plain Wrangler, Outlander, Tucson or RAV4.
    # Slugs follow each site's URL scheme but have not been checked against the live sites yet:
    # run each source once with -v (GETTING_STARTED.md section 5, step 6) and fix any that return 0.
    # Cargo figures are approximate.
    Model("wrangler4xe", "Jeep", "Wrangler 4xe", "Plug-in hybrid 4x4", "5",
          ("jeep-wrangler_4xe", "jeep-wrangler_unlimited_4xe"), "jeep/wrangler-4xe", "jeep/wrangler-4xe",
          r"\bwrangler\b.*\b4xe\b", [(2021, 2030, 67.4, 27.7, None)]),
    Model("grandcherokee4xe", "Jeep", "Grand Cherokee 4xe", "Plug-in hybrid SUV", "5",
          "jeep-grand_cherokee_4xe", "jeep/grand-cherokee-4xe", "jeep/grand-cherokee-4xe",
          r"\bgrand\s+cherokee\b.*\b4xe\b", [(2022, 2030, 70.8, 37.7, None)]),
    Model("outlanderphev", "Mitsubishi", "Outlander PHEV", "Plug-in hybrid 3-row SUV", "7",
          "mitsubishi-outlander_phev", "mitsubishi/outlander-phev", "mitsubishi/outlander-phev",
          r"\boutlander\b.*\b(?:phev|plug[\s-]?in)\b", [(2023, 2030, 64.7, 30.8, 12.8)]),
    Model("tucsonphev", "Hyundai", "Tucson Plug-in Hybrid", "Plug-in hybrid compact SUV", "5",
          "hyundai-tucson_plug_in_hybrid", "hyundai/tucson-plug-in-hybrid", "hyundai/tucson-plug-in-hybrid",
          r"\btucson\b.*\b(?:phev|plug[\s-]?in)\b", [(2022, 2030, 66.3, 31.9, None)]),
    # Renamed "RAV4 Plug-in Hybrid" for 2025.
    Model("rav4prime", "Toyota", "RAV4 Prime", "Plug-in hybrid compact SUV", "5",
          ("toyota-rav4_prime", "toyota-rav4_plug_in_hybrid"), ("toyota/rav4-prime", "toyota/rav4-plug-in-hybrid"),
          ("toyota/rav4-prime", "toyota/rav4-plug-in-hybrid"),
          r"\brav\s?-?4\b.*\b(?:prime|phev|plug[\s-]?in)\b", [(2021, 2030, 69.8, 33.5, None)]),
]

BY_KEY = {m.key: m for m in MODELS}

# Autojunket reliability report slugs (autojunket.com/vehicles/<year>-<slug>). Autojunket has no
# Prius v page and only some years of a few models, so those years are listed explicitly.
AUTOJUNKET = {
    "sienna": "toyota-sienna", "odyssey": "honda-odyssey", "highlander": "toyota-highlander",
    "pilot": "honda-pilot", "4runner": "toyota-4runner", "rav4": "toyota-rav4", "crv": "honda-cr-v",
    "venza": "toyota-venza", "prius": "toyota-prius", "element": "honda-element", "rx": "lexus-rx", "gx": "lexus-gx",
    "mdx": "acura-mdx", "rdx": "acura-rdx",
}
AUTOJUNKET_YEARS = {"venza": {2012}, "element": {2010}}


def autojunket_url(key, year):
    slug = AUTOJUNKET.get(key)
    if not slug or not year or (key in AUTOJUNKET_YEARS and year not in AUTOJUNKET_YEARS[key]):
        return None
    return f"https://autojunket.com/vehicles/{year}-{slug}"


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
