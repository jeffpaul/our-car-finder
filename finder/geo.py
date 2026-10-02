"""Geocoding and driving distance from the search origin, cached in sqlite."""
import math
import time

from curl_cffi import requests

NOMINATIM_UA = {"User-Agent": "minivan-finder/1.0 (personal daily car search)"}
_last_nominatim = 0.0


def haversine_miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class Geo:
    def __init__(self, db, origin_lat, origin_lon):
        self.db = db
        self.olat, self.olon = origin_lat, origin_lon
        self.http = requests.Session(impersonate="chrome")
        db.execute("CREATE TABLE IF NOT EXISTS geocode (q TEXT PRIMARY KEY, lat REAL, lon REAL, label TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS route (k TEXT PRIMARY KEY, miles REAL, minutes REAL)")

    def zip(self, z):
        """Returns (lat, lon, "City, ST") for a US zip code."""
        z = str(z)[:5]
        hit = self._cached("zip:" + z)
        if hit:
            return hit
        try:
            r = self.http.get(f"https://api.zippopotam.us/us/{z}", timeout=15)
            p = r.json()["places"][0]
            res = (float(p["latitude"]), float(p["longitude"]), f'{p["place name"]}, {p["state abbreviation"]}')
        except Exception:
            res = (None, None, None)
        return self._store("zip:" + z, res)

    def place(self, text):
        """Returns (lat, lon, label) for free text like "Georgetown, TX"."""
        q = text.strip().lower()
        hit = self._cached("q:" + q)
        if hit:
            return hit
        global _last_nominatim
        time.sleep(max(0, 1.1 - (time.time() - _last_nominatim)))
        _last_nominatim = time.time()
        try:
            r = self.http.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": text, "format": "json", "limit": 1, "countrycodes": "us"},
                headers=NOMINATIM_UA, timeout=20,
            )
            j = r.json()
            res = (float(j[0]["lat"]), float(j[0]["lon"]), text) if j else (None, None, text)
        except Exception:
            res = (None, None, text)
        return self._store("q:" + q, res)

    def drive(self, lat, lon):
        """Driving (miles, minutes) from the origin. Falls back to straight-line x1.2."""
        if lat is None or lon is None:
            return None, None
        k = f"{lat:.3f},{lon:.3f}"
        row = self.db.execute("SELECT miles, minutes FROM route WHERE k=?", (k,)).fetchone()
        if row:
            return row
        miles = minutes = None
        try:
            r = self.http.get(
                f"https://router.project-osrm.org/route/v1/driving/{self.olon},{self.olat};{lon},{lat}",
                params={"overview": "false"}, timeout=20,
            )
            route = r.json()["routes"][0]
            miles, minutes = route["distance"] / 1609.34, route["duration"] / 60
        except Exception:
            pass
        if miles is None:
            # Not cached, so the next run retries OSRM.
            return round(haversine_miles(self.olat, self.olon, lat, lon) * 1.2, 1), None
        self.db.execute("INSERT OR REPLACE INTO route VALUES (?,?,?)", (k, miles, minutes))
        self.db.commit()
        return miles, minutes

    def _cached(self, q):
        row = self.db.execute("SELECT lat, lon, label FROM geocode WHERE q=?", (q,)).fetchone()
        return tuple(row) if row else None

    def _store(self, q, res):
        if res[0] is not None:
            self.db.execute("INSERT OR REPLACE INTO geocode VALUES (?,?,?,?)", (q, *res))
            self.db.commit()
        return res
