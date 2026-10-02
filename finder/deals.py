"""Deal scoring: how a listing's price compares with similar cars on the market.

Every run records year/mileage/price for each model from all sites, including a wider
Cars.com price band and CarMax's full inventory, so the sample isn't limited to the
budget. Per model we fit  log(price) = a + b*year + c*mileage + d*private  on the last 45 days of
observations (with one outlier-trimming pass) and compare each listing to its
predicted price. The private-seller term matters: private asking prices run well below
dealer retail, so without it every Facebook listing looks like a bargain. Autotrader's KBB Fair Purchase Price is used when it's available.
"""
import datetime as dt
import json
import math

import numpy as np

WINDOW_DAYS = 45
MIN_OBS = 15

SUSPECT_PCT = 0.40
# Every car in a fixed budget is in the cheap tail of its model's market, so labels are also
# relative: "great" needs the top 10% of current candidates, "good" the top 25%, plus these floors.
GREAT_FLOOR, GOOD_FLOOR = 0.15, 0.07
GREAT_PCTL, GOOD_PCTL = 90, 75


def thresholds(db):
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    row = db.execute("SELECT v FROM meta WHERE k='deal_thresholds'").fetchone()
    return tuple(json.loads(row[0])) if row else (GREAT_FLOOR, GOOD_FLOOR)


def update_thresholds(db, pcts):
    """Sets the great/good cutoffs from the current candidates' % under expected."""
    if len(pcts) >= 20:
        great = max(GREAT_FLOOR, float(np.percentile(pcts, GREAT_PCTL)))
        good = max(GOOD_FLOOR, float(np.percentile(pcts, GOOD_PCTL)))
    else:
        great, good = GREAT_FLOOR, GOOD_FLOOR
    db.execute("INSERT OR REPLACE INTO meta VALUES ('deal_thresholds', ?)", (json.dumps([great, good]),))
    db.commit()
    return great, good


def record_market(db, rows, today):
    db.execute("""CREATE TABLE IF NOT EXISTS market (
        k TEXT PRIMARY KEY, model_key TEXT, year INTEGER, mileage INTEGER, price INTEGER, seen TEXT,
        private INTEGER DEFAULT 0)""")
    cols = [r[1] for r in db.execute("PRAGMA table_info(market)")]
    if "private" not in cols:
        db.execute("ALTER TABLE market ADD COLUMN private INTEGER DEFAULT 0")
        db.execute("UPDATE market SET private=1 WHERE k LIKE 'Facebook:%'")
    db.executemany("INSERT OR REPLACE INTO market (k, model_key, year, mileage, price, seen, private) "
                   "VALUES (?,?,?,?,?,?,?)", rows)
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    db.execute("DELETE FROM market WHERE seen < ?", (cutoff,))
    db.commit()


class Market:
    def __init__(self, db):
        self.fits = {}
        rows = db.execute("SELECT model_key, year, mileage, price, private FROM market").fetchall()
        by_model = {}
        for mk, y, m, p, priv in rows:
            by_model.setdefault(mk, []).append((y, m, p, priv or 0))
        for mk, obs in by_model.items():
            if len(obs) >= MIN_OBS:
                self.fits[mk] = self._fit(np.array(obs, dtype=float))

    @staticmethod
    def _fit(a):
        y0 = 2014.0
        X = np.column_stack([np.ones(len(a)), a[:, 0] - y0, a[:, 1] / 1e5, a[:, 3]])
        z = np.log(a[:, 2])
        coef, *_ = np.linalg.lstsq(X, z, rcond=None)
        resid = z - X @ coef
        sd = resid.std() or 0.1
        keep = np.abs(resid) < 2.5 * sd
        if keep.sum() >= MIN_OBS:
            coef, *_ = np.linalg.lstsq(X[keep], z[keep], rcond=None)
            sd = (z[keep] - X[keep] @ coef).std() or sd
        if a[:, 3].sum() < 5:  # too few private sales to estimate the discount; assume a typical 12%
            coef[3] = math.log(0.88)
        return {"coef": coef, "y0": y0, "n": int(keep.sum()), "sd": float(sd),
                "years": (int(a[:, 0].min()), int(a[:, 0].max()))}

    def expected(self, model_key, year, mileage, private=False):
        f = self.fits.get(model_key)
        if not f or not year or not mileage:
            return None
        lo, hi = f["years"]
        if not lo - 1 <= year <= hi + 1:
            return None
        c = f["coef"]
        return float(math.exp(c[0] + c[1] * (year - f["y0"]) + c[2] * mileage / 1e5 + c[3] * bool(private)))

    def score(self, x, cut=(GREAT_FLOOR, GOOD_FLOOR)):
        """Returns a deal dict for a normalized listing, or None when there isn't enough to go on."""
        exp = self.expected(x["model_key"], x.get("year"), x.get("mileage"), x.get("private"))
        kbb = x.get("kbb")
        ref = kbb or exp
        if not ref:
            if x.get("deal_indicator") in ("Great", "Good"):
                return {"label": x["deal_indicator"].lower(), "basis": "Autotrader rating"}
            return None
        delta = ref - x["price"]
        pct = delta / ref
        if pct >= SUSPECT_PCT:
            # Far below market is almost always a down-payment ad, a scam, or a branded title.
            return {"label": "suspect", "expected": round(ref, -1), "delta": round(delta, -1),
                    "pct": round(pct * 100), "raw_pct": pct, "basis": "similar listings"}
        great, good = cut
        if pct >= great:
            label = "great"
        elif pct >= good:
            label = "good"
        elif pct <= -0.08:
            label = "high"
        else:
            label = "fair"
        # Autotrader's own rating can only promote a listing, never demote it.
        ind = (x.get("deal_indicator") or "").lower()
        if ind == "great" and label != "great":
            label = "great"
        elif ind == "good" and label in ("fair", "high"):
            label = "good"
        return {"label": label, "expected": round(ref, -1), "delta": round(delta, -1),
                "pct": round(pct * 100), "raw_pct": pct,
                "basis": "KBB fair price" if kbb else ("similar private sales" if x.get("private") else "similar dealer listings"),
                "model_estimate": round(exp, -1) if exp else None}
