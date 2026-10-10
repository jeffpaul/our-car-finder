"""Builds the generations guide (docs/generations/ on the report site) from hand-written generation
notes plus current matches and NHTSA data in finder.db.

  scripts/generations.py OUT.html      write the page only
  scripts/generations.py --publish     write it into the Pages checkout, push, and send an ntfy push
"""
import datetime as dt
import html
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path("/home/pete/minivan-finder")
sys.path.insert(0, str(ROOT))
from finder.models import BY_KEY, autojunket_url  # noqa: E402
from finder.reliability import Reliability  # noqa: E402

PUBLISH = "--publish" in sys.argv

# Generation notes. Years are model years; "changes" are mid-cycle updates worth knowing about.
GENS = {
    "sienna": [dict(
        name="3rd generation", years=(2011, 2020),
        power="3.5L V6, 266 hp, 6-speed automatic (2011–2016). 2011–2012 base models also offered a 2.7L 4-cylinder (187 hp). 2017+ V6 makes 296 hp with an 8-speed.",
        drive="FWD; AWD available with the V6 (the only AWD minivan of this era).",
        features="7 or 8 seats. Power sliding doors on most trims; SE trim (2011+) has sport-tuned suspension. XLE/Limited add leather, sunroof, and power liftgate; Limited adds a dual-view rear entertainment screen option.",
        changes="2015 refresh: new dashboard and gauges, quieter cabin, more standard features. 2018: Toyota Safety Sense standard.",
        check="Run both power sliding doors through several cycles; listen for grinding and check that they latch. Ask whether the sliding-door recall was done (VIN check at nhtsa.gov).",
    )],
    "odyssey": [dict(
        name="4th generation", years=(2011, 2017),
        power="3.5L V6 with cylinder deactivation (VCM), 248 hp. 5-speed automatic on most 2011–2013 trims (6-speed on Touring); 6-speed on all trims from 2014.",
        drive="FWD only.",
        features="7 or 8 seats. EX and up get power sliding doors; Touring adds navigation, memory seats, and a 6-speed. 2014 Touring Elite has a built-in vacuum (HondaVAC).",
        changes="2014 refresh: direct-injected V6, 6-speed standard, new front end and dash, LaneWatch side camera on EX and up, forward collision warning on upper trims.",
        check="Look for misfire codes and oil use on 2011–2013 (VCM). Ask for timing-belt service records past ~105k miles. Test-drive for shudder at 20–40 mph on 2014+.",
    )],
    "highlander": [dict(
        name="2nd generation", years=(2008, 2013),
        power="3.5L V6, 270 hp, 5-speed automatic. Base models offered a 2.7L 4-cylinder (187 hp, 6-speed). A Hybrid with AWD was also sold.",
        drive="FWD or AWD (V6).",
        features="Up to 7 seats on most trims, with a small 3rd row. SE and Limited add leather, sunroof and power liftgate.",
        changes="2011 refresh: new grille and lights, updated interior trim, standard 3rd row on more trims.",
        check="Few widespread problems. Confirm water-pump and timing-chain area isn't leaking on high-mileage V6s; check the 3rd row folds cleanly.",
    )],
    "pilot": [dict(
        name="2nd generation", years=(2009, 2015),
        power="3.5L V6 with cylinder deactivation (VCM), 250 hp, 5-speed automatic.",
        drive="FWD or 4WD.",
        features="8 seats. Boxy shape gives a usable 3rd row and big cargo opening. EX-L adds leather and sunroof; Touring adds navigation and rear entertainment.",
        changes="2012 refresh: new grille, quieter cabin, updated interior, slightly better fuel economy.",
        check="Look for misfires and oil burning on 2011–2013 (VCM). Timing belt due around 105k miles; ask for proof.",
    ), dict(
        name="3rd generation", years=(2016, 2022),
        power="3.5L direct-injected V6, 280 hp. 6-speed automatic on LX/EX/EX-L; 9-speed on Touring and Elite.",
        drive="FWD or AWD.",
        features="7 or 8 seats. Honda Sensing (adaptive cruise, lane keeping, automatic braking) available; standard from 2019.",
        changes="2019 refresh: Honda Sensing standard, 9-speed on more trims, new infotainment.",
        check="2016–2018 fuel injector failures (Honda extended the warranty), so ask if the injectors were replaced. Test the 9-speed for rough or slow shifts.",
    )],
    "rav4": [dict(
        name="3rd generation", years=(2006, 2012),
        power="2.5L 4-cylinder, 179 hp, 4-speed automatic; or 3.5L V6, 269 hp, 5-speed (fast and rare).",
        drive="FWD or AWD.",
        features="5 seats (a tiny optional 3rd row on some). Side-hinged rear door with the spare tire mounted on it.",
        changes="2011: minor trim and equipment updates.",
        check="The 2.5L is durable; listen for a rattling rear door, check the spare-tire carrier hinge, and confirm the rear seat-belt recall (16V-096) was done.",
    ), dict(
        name="4th generation", years=(2013, 2018),
        power="2.5L 4-cylinder, 176 hp, 6-speed automatic. No V6. Hybrid (AWD) added for 2016.",
        drive="FWD or AWD.",
        features="5 seats. Conventional liftgate (spare moved under the floor). Limited adds a power liftgate and blind-spot monitoring.",
        changes="2016 refresh: new styling, Hybrid model, more safety tech. 2017: Toyota Safety Sense standard.",
        check="Few widespread problems. Feel for a shudder at light throttle around 25–45 mph (torque converter) on early years.",
    )],
    "crv": [dict(
        name="3rd generation", years=(2007, 2011),
        power="2.4L 4-cylinder, 180 hp, 5-speed automatic.",
        drive="FWD or AWD.",
        features="5 seats. Liftgate replaced the older side-hinged door.",
        changes="2010 refresh: more power, updated styling.",
        check="Listen for a brief rattle on cold start (VTC actuator) and check that the A/C blows cold.",
    ), dict(
        name="4th generation", years=(2012, 2016),
        power="2.4L 4-cylinder, 185 hp. 5-speed automatic for 2012–2014; direct injection and a CVT from 2015.",
        drive="FWD or AWD.",
        features="5 seats. Rear seats fold flat with one pull. EX adds sunroof and backup camera; EX-L adds leather; 2015 Touring adds Honda Sensing.",
        changes="2015 refresh: new engine and CVT, quieter cabin, more features, better fuel economy.",
        check="Make sure the A/C blows cold (compressor failures are common). On 2015–2016, feel for vibration at idle in Drive.",
    )],
    "venza": [dict(
        name="1st generation", years=(2009, 2015),
        power="2.7L 4-cylinder, 182 hp, or 3.5L V6, 268 hp; both 6-speed automatic.",
        drive="FWD or AWD.",
        features="5 seats. A tall wagon built on the Camry platform: low step-in height, car-like ride, wide cargo floor.",
        changes="2013 refresh: updated front end, Entune infotainment.",
        check="Few widespread problems. Check the power liftgate and the large wheels and tires (expensive to replace).",
    )],
    "priusv": [dict(
        name="1st generation", years=(2012, 2017),
        power="1.8L hybrid, 134 hp combined, eCVT. About 42 mpg combined.",
        drive="FWD only.",
        features="5 seats. Rear seats slide and recline; much more cargo room than a regular Prius.",
        changes="2015 refresh: new front end, updated interior and infotainment.",
        check="Ask for a hybrid-battery health check. At high mileage, look for coolant loss or overheating (EGR clogging and head gasket).",
    )],
    "prius": [dict(
        name="3rd generation", years=(2010, 2015),
        power="1.8L hybrid, 134 hp combined, eCVT. About 50 mpg combined.",
        drive="FWD only.",
        features="5 seats. Hatchback with a split-folding rear seat. Trims Two through Five; Four and Five add leather and heated seats, and some have a solar-panel roof.",
        changes="2012 refresh: new front end and Entune infotainment.",
        check="Ask for a hybrid-battery health check. At high mileage, look for coolant loss or overheating (EGR clogging and head gasket), which is well known on this generation.",
    ), dict(
        name="4th generation", years=(2016, 2022),
        power="1.8L hybrid, 121 hp combined, eCVT. About 52 mpg combined (56 in the Eco trim).",
        drive="FWD; AWD-e from 2019.",
        features="5 seats. Roomier cargo area than before. Toyota Safety Sense (adaptive cruise, lane departure, automatic braking) standard.",
        changes="2019 refresh: restyled front and rear, AWD-e option.",
        check="Few widespread problems. Ask for a hybrid-battery health check on high-mileage cars.",
    )],
    "rx": [dict(
        name="3rd generation", years=(2010, 2015),
        power="RX 350: 3.5L V6, 275 hp, 6-speed automatic. RX 450h: V6 hybrid, 295 hp combined, eCVT.",
        drive="FWD or AWD.",
        features="5 seats. Quiet, comfortable luxury crossover; shares its V6 with the Highlander. Remote Touch controller for navigation.",
        changes="2013 refresh: spindle grille, F Sport trim added, updated interior.",
        check="Check the dashboard for stickiness or cracks (Lexus ran a replacement program). Same durable V6 as the Highlander.",
    )],
    "mdx": [dict(
        name="2nd generation", years=(2007, 2013),
        power="3.7L V6, 300 hp, 6-speed automatic (2010–2013).",
        drive="SH-AWD (torque-vectoring all-wheel drive) standard.",
        features="7 seats. Technology package adds navigation and premium audio; Advance package adds adaptive cruise and ventilated seats.",
        changes="2010 refresh: bigger 3.7L V6, 6-speed, new grille.",
        check="Timing belt due about every 105k miles; ask for proof. Check SH-AWD fluid service records.",
    ), dict(
        name="3rd generation", years=(2014, 2020),
        power="3.5L direct-injected V6, 290 hp. 6-speed automatic for 2014–2015; 9-speed from 2016. Sport Hybrid from 2017.",
        drive="FWD or SH-AWD.",
        features="7 seats. Lighter and more efficient than before. AcuraWatch safety suite standard from 2017.",
        changes="2016: 9-speed transmission and engine idle stop. 2017 refresh: new grille, AcuraWatch standard.",
        check="Test the 9-speed (2016+) for rough or hesitant shifts. Timing belt due about every 105k miles.",
    )],
    "rdx": [dict(
        name="1st generation", years=(2007, 2012),
        power="2.3L turbocharged 4-cylinder, 240 hp, 5-speed automatic.",
        drive="SH-AWD standard; FWD available from 2010.",
        features="5 seats. Sporty, firm-riding compact luxury crossover.",
        changes="2010 refresh: new grille, FWD option.",
        check="At high mileage, ask about turbo work and oil consumption.",
    ), dict(
        name="2nd generation", years=(2013, 2018),
        power="3.5L V6 with cylinder deactivation, 273 hp (279 hp from 2016), 6-speed automatic.",
        drive="FWD or AWD.",
        features="5 seats. Smoother and roomier than the 1st generation. Technology package adds navigation; 2016+ Advance adds ventilated seats.",
        changes="2016 refresh: more power, new styling, AcuraWatch safety features available.",
        check="Check for misfires and oil burning (cylinder deactivation). Timing belt due about every 105k miles.",
    )],
}

BOTTOM = {
    "sienna": "The roomiest van here and the only one with AWD. 2013–2014 drew far fewer complaints than 2011.",
    "odyssey": "The most listings in the search. It drives better than the Sienna, but budget for VCM issues and the timing belt.",
    "highlander": "A dependable 3-row SUV, though the 3rd row is small. Few match because they hold their value.",
    "pilot": "The roomiest 3-row SUV in this price range; watch the VCM years.",
    "rav4": "Simple and durable. 2013+ is roomier, but 2011–2012 can be had with the fast V6.",
    "crv": "Lots of listings and cheap to run. Check the A/C.",
    "venza": "A comfortable wagon-like 5-seater with a low ride height; uncommon and few known problems.",
    "priusv": "The cheapest to fuel by far; the battery's age is the big unknown.",
    "prius": "The cheapest to run here, but much less cargo room than the Prius v.",
    "rx": "A luxury Highlander for 5; rare in this budget.",
    "mdx": "Most luxury for the money, with 3 rows and SH-AWD; budget for the timing belt.",
    "rdx": "Lots of listings. The 2013+ V6 is the safer bet over the 1st-gen turbo.",
}

db = sqlite3.connect(ROOT / "data/finder.db")
rel = Reliability(db)
rows = [json.loads(r[0]) for r in db.execute("SELECT data FROM listings WHERE gone_on IS NULL")]
matches = [x for x in rows if x["tier"] == "match" and x.get("year")
           and (x.get("deal") or {}).get("label") != "suspect"]
REPORT = "https://petenelson.github.io/our-car-finder-report/"


def e(s):
    return html.escape(str(s), quote=True)


def money(n):
    return f"${n:,.0f}"


def stars(n):
    return f'<span class="st{" low" if n <= 2 else ""}" aria-label="{n} of 5 stars">{"★" * n}{"☆" * (5 - n)}</span>'


sections, overview = [], []
for key, gens in GENS.items():
    m = BY_KEY[key]
    mine = [x for x in matches if x["model_key"] == key]
    if not mine:
        continue
    gen_html = []
    for g in gens:
        lo, hi = g["years"]
        cars = [x for x in mine if lo <= x["year"] <= hi]
        if not cars:
            continue
        years = sorted({x["year"] for x in cars})
        prices = [x["price"] for x in cars]
        miles = [x["mileage"] for x in cars if x.get("mileage")]
        deals = [x for x in cars if (x.get("deal") or {}).get("label") in ("great", "good")]
        best = min(cars, key=lambda x: (-(x.get("deal") or {}).get("pct", 0) if x.get("deal") else 0, x["price"]))
        cargo = m.cargo_for(lo) if lo >= 2011 else m.cargo_for(years[0])
        # Per-year reliability across the matched years of this generation.
        yr_rows, star_vals = [], []
        for y in range(max(lo, 2011), min(hi, dt.date.today().year) + 1):
            r = rel.rating(key, y)
            if not r or not r.get("stars"):
                continue
            n_y = sum(1 for x in cars if x["year"] == y)
            if y in years:
                star_vals.append(r["stars"])
            aj = autojunket_url(key, y)
            yr_rows.append(
                f'<tr class="{"" if n_y else "dim"}"><td>{y}</td><td>{stars(r["stars"])}</td>'
                f'<td>{r["per10k"]}</td><td>{r["recalls"]}</td><td>{e(", ".join(r["components"]).lower())}</td>'
                f'<td class="n">{n_y or "–"}</td><td>{f"<a href={chr(34)}{e(aj)}{chr(34)} target=_blank rel=noopener>↗</a>" if aj else ""}</td></tr>')
        issues = next((t for a, b, t in __import__("finder.reliability", fromlist=["KNOWN_ISSUES"]).KNOWN_ISSUES.get(key, [])
                       if a <= years[0] <= b), "")
        med_miles = statistics.median(miles) if miles else None
        avg_st = round(sum(star_vals) / len(star_vals), 1) if star_vals else None
        overview.append(dict(model=f"{m.make} {m.model}", key=key, gen=g["name"], years=f"{lo}–{hi}",
                             n=len(cars), lo=min(prices), hi=max(prices), med=statistics.median(prices),
                             miles=med_miles, cargo=cargo, stars=avg_st, deals=len(deals)))
        cargo_txt = (f'{cargo["max"]} cu ft max · {cargo["row2"]} behind 2nd row'
                     + (f' · {cargo["row3"]} behind 3rd row' if cargo.get("row3") else "")) if cargo else "—"
        best_link = next(iter(best["links"].values()), None)
        gen_html.append(f"""
      <div class="gen">
        <div class="gen-head">
          <h3>{e(g["name"])} <span>{lo}–{hi}</span></h3>
          <div class="pill">{len(cars)} match{"es" if len(cars) != 1 else ""} · {money(min(prices))}–{money(max(prices))}{f" · median {med_miles/1000:.0f}k mi" if med_miles else ""}{f" · {len(deals)} good/great deal{'s' if len(deals) != 1 else ''}" if deals else ""}</div>
        </div>
        <dl>
          <dt>Powertrain</dt><dd>{e(g["power"])}</dd>
          <dt>Drive</dt><dd>{e(g["drive"])}</dd>
          <dt>Features</dt><dd>{e(g["features"])}</dd>
          <dt>Updates</dt><dd>{e(g["changes"])}</dd>
          <dt>Cargo</dt><dd>{e(cargo_txt)} · seats {e(m.seats)}</dd>
          <dt>Known issues</dt><dd>{e(issues)}</dd>
          <dt>Before you buy</dt><dd>{e(g["check"])}</dd>
        </dl>
        <div class="tbl"><table>
          <thead><tr><th>Year</th><th>Reliability</th><th>Complaints / 10k sold</th><th>Recalls</th><th>Most complaints</th><th class="n">Listed now</th><th>Autojunket</th></tr></thead>
          <tbody>{"".join(yr_rows)}</tbody>
        </table></div>
        {f'<p class="best">Best deal listed now: <a href="{e(best_link)}" target="_blank" rel="noopener">{e(best["year"])} {e(m.model)} {e(best.get("trim") or "")} · {money(best["price"])} · {f"{best[chr(109)+chr(105)+chr(108)+chr(101)+chr(97)+chr(103)+chr(101)]/1000:.0f}k mi · " if best.get("mileage") else ""}{e(best.get("city") or "")}</a></p>' if best_link else ""}
      </div>""")
    sections.append(f"""
    <section class="model" id="{key}">
      <div class="model-head">
        <h2>{e(m.make)} {e(m.model)}</h2>
        <span class="body">{e(m.body)} · {len(mine)} current match{"es" if len(mine) != 1 else ""}</span>
      </div>
      <p class="bottom">{e(BOTTOM[key])}</p>
      {"".join(gen_html)}
    </section>""")

overview.sort(key=lambda o: -o["n"])
ov_rows = "".join(
    f'<tr><td><a href="#{o["key"]}">{e(o["model"])}</a><small>{e(o["gen"])} · {o["years"]}</small></td>'
    f'<td class="n">{o["n"]}</td><td class="n">{money(o["med"])}</td>'
    f'<td class="n">{f"{o[chr(109)+chr(105)+chr(108)+chr(101)+chr(115)]/1000:.0f}k" if o["miles"] else "—"}</td>'
    f'<td class="n">{o["cargo"]["max"] if o["cargo"] else "—"}</td>'
    f'<td>{stars(round(o["stars"])) if o["stars"] else "—"} <small>{o["stars"] or ""}</small></td>'
    f'<td class="n">{o["deals"] or "–"}</td></tr>'
    for o in overview)

today = dt.date.today().strftime("%b %-d, %Y")
page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Car Generations Guide</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Barlow:wght@400;500;600&display=swap">
<style>
:root {{ --bg:#f3f4f1; --paper:#fff; --ink:#15201b; --muted:#5d6a63; --line:#dde1db; --accent:#1b6a46; --soft:#e3efe8; --warn:#b5541f; --star:#b07d12;
  --head:"Barlow Condensed","Arial Narrow",system-ui,sans-serif; --body:"Barlow",system-ui,-apple-system,"Segoe UI",sans-serif; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111814; --paper:#18221d; --ink:#e6eee9; --muted:#9db0a5; --line:#2a3a31; --accent:#6fcf9b; --soft:#1f3329; --warn:#f08a5d; --star:#e0b146; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 var(--body); -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1040px; margin:0 auto; padding:28px 16px 60px; }}
a {{ color:var(--accent); text-underline-offset:3px; }}
.kicker {{ font:600 13px/1 var(--body); letter-spacing:.12em; text-transform:uppercase; color:var(--muted); margin:0 0 8px; }}
h1 {{ font:700 clamp(38px,7vw,56px)/1 var(--head); margin:0 0 10px; }}
.intro {{ color:var(--muted); max-width:760px; margin:0 0 22px; }}
nav.jump {{ display:flex; gap:8px; overflow-x:auto; padding-bottom:6px; margin-bottom:22px; }}
nav.jump a {{ flex:none; padding:6px 12px; border:1px solid var(--line); border-radius:999px; background:var(--paper); text-decoration:none; color:var(--ink); font-size:14px; }}
.tbl {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
table {{ border-collapse:collapse; width:100%; min-width:680px; font-size:14.5px; }}
th {{ text-align:left; font:600 11.5px/1.2 var(--body); letter-spacing:.08em; text-transform:uppercase; color:var(--muted); padding:8px 10px; border-bottom:2px solid var(--ink); white-space:nowrap; }}
td {{ padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
td small {{ display:block; color:var(--muted); font-size:12.5px; }}
.n {{ text-align:right; font-variant-numeric:tabular-nums; }}
tr.dim td {{ color:var(--muted); opacity:.7; }}
.st {{ color:var(--star); letter-spacing:1px; white-space:nowrap; }}
.st.low {{ color:var(--warn); }}
.overview {{ background:var(--paper); border:1px solid var(--line); border-radius:12px; padding:14px 14px 6px; margin-bottom:34px; }}
.overview h2 {{ font:700 26px/1 var(--head); margin:2px 0 10px; }}
.model {{ margin-bottom:40px; scroll-margin-top:12px; }}
.model-head {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 14px; border-bottom:3px solid var(--ink); padding-bottom:6px; }}
.model-head h2 {{ font:700 36px/1 var(--head); margin:0; }}
.model-head .body {{ color:var(--muted); font-size:15px; }}
.bottom {{ font-size:17px; margin:10px 0 14px; }}
.gen {{ background:var(--paper); border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin-bottom:14px; }}
.gen-head {{ display:flex; flex-wrap:wrap; justify-content:space-between; align-items:baseline; gap:6px 12px; margin-bottom:8px; }}
.gen-head h3 {{ font:700 24px/1.1 var(--head); margin:0; }}
.gen-head h3 span {{ color:var(--muted); font-weight:600; }}
.pill {{ background:var(--soft); color:var(--accent); font-weight:600; font-size:13.5px; padding:4px 10px; border-radius:999px; }}
dl {{ display:grid; grid-template-columns:130px 1fr; gap:6px 14px; margin:0 0 12px; font-size:15px; }}
dt {{ font:600 12px/1.6 var(--body); letter-spacing:.07em; text-transform:uppercase; color:var(--muted); }}
dd {{ margin:0; }}
.best {{ margin:10px 0 0; font-size:14.5px; }}
.notes {{ color:var(--muted); font-size:14px; border-top:1px solid var(--line); padding-top:14px; }}
@media (max-width:600px) {{ dl {{ grid-template-columns:1fr; gap:0 }} dd {{ margin-bottom:8px }} .gen {{ padding:14px }} }}
</style>
</head>
<body>
<div class="wrap">
  <p class="kicker">Our Car Finder · snapshot from {today}</p>
  <h1>Generations guide</h1>
  <p class="intro">Every model and generation that has listings matching our search right now ($6–10k, 2011+, under 150k miles, within 250 miles of Austin): what changed between years, what to check, and how each model year rates for reliability. This is a one-time snapshot; the <a href="{REPORT}">live report</a> keeps updating.</p>
  <nav class="jump">{"".join(f'<a href="#{k}">{e(BY_KEY[k].model)}</a>' for k in GENS if any(x["model_key"] == k for x in matches))}</nav>

  <div class="overview">
    <h2>At a glance</h2>
    <div class="tbl"><table>
      <thead><tr><th>Model / generation</th><th class="n">Matches</th><th class="n">Median price</th><th class="n">Median miles</th><th class="n">Max cargo (cu ft)</th><th>Avg reliability</th><th class="n">Good/great deals</th></tr></thead>
      <tbody>{ov_rows}</tbody>
    </table></div>
  </div>

  {"".join(sections)}

  <div class="notes">
    <p><b>Reliability:</b> stars come from NHTSA owner complaints per 10,000 vehicles sold for each model year, ranked against every model year in the search (airbag complaints excluded). Greyed rows are years with no current listings. Sales figures are approximate, so treat stars as a guide. The ↗ links open Autojunket's report for that year; its scores are its own estimates.</p>
    <p><b>Not included:</b> 4Runner (only two "matches", a 2023 and a 2026 at under $10k, which are almost certainly bait listings), Element, and GX 460 (no current matches).</p>
    <p>Specs are for the common U.S. versions and vary slightly by trim. Verify any specific car with a pre-purchase inspection and a VIN recall check at nhtsa.gov/recalls.</p>
  </div>
</div>
</body>
</html>"""
if not PUBLISH:
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print("wrote", out, len(page), "bytes;", len(sections), "models")
    sys.exit(0)

import subprocess  # noqa: E402

import run  # noqa: E402
from finder.notify import Notifier  # noqa: E402

cfg = run.load_config()
repo = ROOT / cfg["publish"].get("checkout", "pages")


def git(*args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


git("fetch", "origin")
git("reset", "--hard", f"origin/{cfg['publish'].get('branch', 'main')}")
out = repo / cfg["publish"].get("path", "docs") / "generations" / "index.html"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(page)
git("add", "-A")
if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]).returncode:
    git("commit", "-m", f"Rebuild the generations guide {dt.date.today().isoformat()}")
    git("push", "origin", cfg["publish"].get("branch", "main"))
url = cfg["publish"]["page_url"].rstrip("/") + "/generations/"
n_prius = sum(1 for x in matches if x["model_key"] == "prius")
Notifier(cfg).send("Generations guide updated",
                   f"Rebuilt with today's matches: {len(sections)} models, including {n_prius} regular Prius listings.\n{url}",
                   tags="blue_book", report_button=False, ops=True)
print("published", url)
