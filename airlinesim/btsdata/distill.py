"""
DISTILL — warehouse -> the small artifact the simulation actually reads.
======================================================================

This is where every INTERPRETIVE decision about the data lives, deliberately
concentrated in one reviewable file rather than smeared through the loader:
which service class counts as passenger demand, how flown passengers become
demand, how the seasonal curve is fitted, how a comparable route is estimated,
and which heuristics stand in for data that does not exist.

Everything here is honest about its own footing. Three tiers of confidence:

  MEASURED    straight from BTS — passenger volumes, distance, monthly shape,
              runway lengths
  DERIVED     a stated transformation of measured data — the seasonal harmonic
              fit, the gravity coefficients, de-censoring
  HEURISTIC   no public dataset exists; a game-balance guess — gate counts,
              fuel supply, runway *requirements*, and (until a Segment export
              lands) the economic seat window

The MANIFEST records which is which, per field, so nothing downstream can
mistake a guess for a measurement.
"""
from __future__ import annotations
import csv
import datetime as _dt
import gzip
import json
import math
import os

# ---- corpus shape (the agreed decisions) -------------------------------
PASSENGER_CLASS = "F"        # scheduled passenger/cargo; L/P are charter, G all-cargo
CORPUS_AIRPORTS = 300        # top N by outbound scheduled passengers
MIN_PAX_PER_DAY = 10.0       # below this a directional pair is noise, not a market

# ---- interpretive constants -------------------------------------------
# T-100 passengers are FLOWN, not demanded: observed = min(demand, capacity).
# With a Segment export we can de-censor by dividing by the observed load
# factor. With MARKET only there are no seats, so there is nothing to divide by
# and demand is left CENSORED — understated on full routes. Recorded in the
# manifest as demand_basis so this is never silently assumed away.
TARGET_LOAD_FACTOR = 0.85

# Runway *requirement* by stage length (metres). The airports' actual runway
# lengths are measured; what a route REQUIRES is not in any dataset, and without
# aircraft types we can't infer it. Industry-shaped bands, game balance only.
RUNWAY_NEED_KM = ((800, 1500.0), (2500, 2000.0), (5000, 2600.0), (1e9, 3200.0))

# No public dataset covers gate counts or fuel throughput. Scaled off passenger
# volume so big airports are congested and small ones aren't. Pure game balance.
PAX_PER_GATE_PER_DAY = 650.0
FUEL_L_PER_PAX = 25.0

MID_MONTH_DOY = (15, 45, 74, 105, 135, 166, 196, 227, 258, 288, 319, 349)


# ============================================================
# small numerics — pure stdlib, no numpy
# ============================================================

def solve(a: list, b: list) -> list:
    """Gaussian elimination with partial pivoting. Returns x for a·x = b."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return [0.0] * n            # singular; caller treats as no fit
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / m[r][r]
    return x


def ols(rows: list, y: list) -> tuple:
    """
    Ordinary least squares via normal equations. `rows` include their own
    intercept term. Returns (coefficients, r_squared, n).
    """
    if not rows:
        return [], 0.0, 0
    k = len(rows[0])
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(k)] for i in range(k)]
    atb = [sum(rows[i][j] * y[i] for i in range(len(rows))) for j in range(k)]
    coef = solve(ata, atb)
    ybar = sum(y) / len(y)
    sst = sum((v - ybar) ** 2 for v in y)
    sse = sum((y[i] - sum(coef[j] * rows[i][j] for j in range(k))) ** 2
              for i in range(len(rows)))
    r2 = 1.0 - sse / sst if sst > 0 else 0.0
    return coef, r2, len(rows)


def fit_seasonal(monthly_index: list) -> tuple:
    """
    Fit one harmonic to 12 monthly multipliers (mean ~1.0), matching the shape
    route.SegmentDemand already uses:

        season(day) = 1 + amplitude * cos(2π(day - peak_day)/365)

    Returns (amplitude, peak_day). This is DERIVED: a real fit to real monthly
    totals, but a single-harmonic approximation of a curve with more structure
    (spring break and Thanksgiving are not sinusoidal).
    """
    n = len(monthly_index)
    if n != 12:
        return 0.0, 0
    c = s = 0.0
    for i, v in enumerate(monthly_index):
        th = 2 * math.pi * MID_MONTH_DOY[i] / 365.0
        c += (v - 1.0) * math.cos(th)
        s += (v - 1.0) * math.sin(th)
    c *= 2.0 / n
    s *= 2.0 / n
    amp = math.hypot(c, s)
    peak = (math.degrees(math.atan2(s, c)) / 360.0 * 365.0) % 365
    return amp, int(round(peak))


def runway_need_m(distance_km: float) -> float:
    for limit, need in RUNWAY_NEED_KM:
        if distance_km <= limit:
            return need
    return RUNWAY_NEED_KM[-1][1]


def seat_window(demand_per_day: float) -> tuple:
    """Shared with the runtime provider — see routedata.seat_window."""
    from airlinesim.routedata import seat_window as _sw
    return _sw(demand_per_day)


# ============================================================
# aggregation
# ============================================================

def _has(conn, table: str) -> bool:
    row = conn.execute("SELECT COUNT(*) c FROM sqlite_master WHERE type='table' "
                       "AND name=?", (table,)).fetchone()
    if not row or not row["c"]:
        return False
    return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] > 0


def volume_table(conn) -> str:
    """Segment is preferred (it has capacity); Market is the fallback."""
    if _has(conn, "t100_segment"):
        return "t100_segment"
    if _has(conn, "t100_market"):
        return "t100_market"
    raise RuntimeError("no T-100 table in the warehouse — run `airlinesim ingest`")


def span_days(conn, table: str) -> tuple:
    r = conn.execute(f"SELECT MIN(year) lo, MAX(year) hi, "
                     f"COUNT(DISTINCT year || '-' || month) months "
                     f"FROM {table}").fetchone()
    months = r["months"] or 1
    return r["lo"], r["hi"], months * 30.44


def airport_rows(conn, table: str, limit: int) -> list:
    """Top airports by outbound scheduled passengers, joined to runway data."""
    lo, hi, days = span_days(conn, table)
    rows = conn.execute(f"""
        SELECT v.origin AS iata,
               SUM(v.passengers) AS out_pax,
               (SELECT SUM(w.passengers) FROM {table} w
                 WHERE w.dest = v.origin AND w.service_class = ?) AS in_pax,
               a.name AS name, a.longest_runway_m AS runway_m,
               a.lat AS lat, a.lon AS lon
          FROM {table} v LEFT JOIN airport_ref a ON a.iata = v.origin
         WHERE v.service_class = ?
      GROUP BY v.origin
      ORDER BY out_pax DESC
         LIMIT ?""", (PASSENGER_CLASS, PASSENGER_CLASS, limit)).fetchall()

    out = []
    for rank, r in enumerate(rows, start=1):
        opd = (r["out_pax"] or 0) / days
        ipd = (r["in_pax"] or 0) / days
        out.append({
            "iata": r["iata"],
            "name": (r["name"] or "").replace(",", " ")[:60],
            "runway_m": round(r["runway_m"] or 0.0, 1),
            "out_pax_per_day": round(opd, 2),
            "in_pax_per_day": round(ipd, 2),
            "hub_rank": rank,
            # HEURISTIC — see module header.
            "est_gates": max(2, min(300, int(round(opd / PAX_PER_GATE_PER_DAY)))),
            "est_fuel_l_per_day": int(round(opd * FUEL_L_PER_PAX)),
            "lat": round(r["lat"], 4) if r["lat"] is not None else "",
            "lon": round(r["lon"], 4) if r["lon"] is not None else "",
        })
    return out


def route_rows(conn, table: str, keep_iata: set, min_pax_per_day: float) -> list:
    """
    One row per directional pair among the corpus airports, with the 12-month
    shape. Capacity columns are populated only when the volume table is Segment.
    """
    lo, hi, days = span_days(conn, table)
    has_cap = table == "t100_segment"
    cap_sql = (", SUM(seats) AS seats, SUM(departures_performed) AS deps"
               if has_cap else ", 0 AS seats, 0 AS deps")

    pairs = conn.execute(f"""
        SELECT origin, dest, SUM(passengers) AS pax, AVG(distance_mi) AS dist_mi
               {cap_sql}
          FROM {table}
         WHERE service_class = ?
      GROUP BY origin, dest""", (PASSENGER_CLASS,)).fetchall()

    monthly = {}
    for r in conn.execute(f"""
            SELECT origin, dest, month, SUM(passengers) AS pax
              FROM {table} WHERE service_class = ?
          GROUP BY origin, dest, month""", (PASSENGER_CLASS,)):
        monthly.setdefault((r["origin"], r["dest"]), {})[r["month"]] = r["pax"] or 0.0

    out = []
    for r in pairs:
        o, d = r["origin"], r["dest"]
        if o not in keep_iata or d not in keep_iata:
            continue
        ppd = (r["pax"] or 0.0) / days
        if ppd < min_pax_per_day:
            continue

        mv = monthly.get((o, d), {})
        tot = sum(mv.get(m, 0.0) for m in range(1, 13))
        if tot > 0 and len([m for m in range(1, 13) if mv.get(m)]) >= 6:
            mult = [round(mv.get(m, 0.0) * 12.0 / tot, 4) for m in range(1, 13)]
        else:
            mult = [1.0] * 12          # too thin to shape; flat is honest
        amp, peak = fit_seasonal(mult)

        seats_pd = (r["seats"] or 0.0) / days
        lf = (ppd / seats_pd) if seats_pd > 0 else 0.0
        if lf > 0:
            demand = ppd / min(lf, TARGET_LOAD_FACTOR)     # DERIVED de-censoring
            basis = "decensored"
        else:
            demand = ppd                                   # MEASURED but CENSORED
            basis = "censored"

        dist_km = (r["dist_mi"] or 0.0) * 1.609344
        smin, smax = seat_window(demand)
        out.append({
            "origin": o, "dest": d,
            "distance_km": round(dist_km, 1),
            "pax_per_day": round(ppd, 2),
            "demand_per_day": round(demand, 2),
            "demand_basis": basis,
            "seats_per_day": round(seats_pd, 2),
            "deps_per_day": round((r["deps"] or 0.0) / days, 3),
            "load_factor": round(lf, 4),
            "season_amp": round(amp, 4),
            "season_peak_day": peak,
            "min_viable_seats": smin,
            "max_viable_seats": smax,
            "min_runway_m": runway_need_m(dist_km),
            **{f"m{i+1}": mult[i] for i in range(12)},
        })
    return out


def _design(routes: list, airports: list) -> tuple:
    """Build (X, y, actual) using the shared feature function."""
    from airlinesim.routedata import gravity_features
    ap = {a["iata"]: a for a in airports}
    X, y, act = [], [], []
    for r in routes:
        o, d = ap.get(r["origin"]), ap.get(r["dest"])
        if not o or not d:
            continue
        dm = float(r["demand_per_day"])
        f = gravity_features(float(o["out_pax_per_day"]), float(d["in_pax_per_day"]),
                             float(r["distance_km"]), int(o["hub_rank"]),
                             int(d["hub_rank"]))
        if f is None or dm <= 0:
            continue
        X.append(f)
        y.append(math.log(dm))
        act.append(dm)
    return X, y, act


def _median(vals: list) -> float:
    s = sorted(vals)
    return s[len(s) // 2] if s else 1.0


def _fit(routes: list, airports: list) -> tuple:
    """Returns (coefficients, r2, n, calibration)."""
    X, y, act = _design(routes, airports)
    if len(X) < 20:
        return [], 0.0, len(X), 1.0
    coef, r2, n = ols(X, y)
    pred = [math.exp(sum(c * f for c, f in zip(coef, X[i]))) for i in range(len(X))]
    # Calibrate so the MEDIAN predicted/actual ratio is 1.0. exp() of a log-space
    # fit estimates the geometric mean, which understates right-skewed demand;
    # Duan smearing corrects to the arithmetic MEAN and then overshoots (median
    # ratio 1.19 in testing). The median is the quantity a route's demand should
    # be set from, so calibrate on that.
    calib = 1.0 / _median([pred[i] / act[i] for i in range(len(act))])
    return [round(c, 6) for c in coef], round(r2, 4), n, round(calib, 6)


def cross_validate(routes: list, airports: list, folds: int = 7) -> dict:
    """
    Honest accuracy for the Tier-2 fallback: refit on k-1 folds, predict the
    held-out one. Without this there is no basis for claiming a comparable-route
    estimate is worth anything, and the number belongs with the data.
    """
    if len(routes) < folds * 20:
        return {}
    med, w2, w3, tested = [], [], [], 0
    for k in range(folds):
        train = [r for i, r in enumerate(routes) if i % folds != k]
        test = [r for i, r in enumerate(routes) if i % folds == k]
        coef, _, _, calib = _fit(train, airports)
        if not coef:
            continue
        Xt, yt, at = _design(test, airports)
        ratios = [math.exp(sum(c * f for c, f in zip(coef, Xt[i]))) * calib / at[i]
                  for i in range(len(at))]
        if not ratios:
            continue
        tested += len(ratios)
        med.append(_median(ratios))
        w2.append(sum(1 for x in ratios if 0.5 <= x <= 2.0) / len(ratios))
        w3.append(sum(1 for x in ratios if 1 / 3 <= x <= 3.0) / len(ratios))
    if not med:
        return {}
    return {"folds": folds, "held_out_routes": tested,
            "median_ratio": round(sum(med) / len(med), 3),
            "within_2x": round(sum(w2) / len(w2), 4),
            "within_3x": round(sum(w3) / len(w3), 4)}


def _weighted_quantiles(pairs: list, qs=(0.25, 0.5, 0.75)) -> list:
    """
    Passenger-weighted quantiles over [(value, weight), ...]. Weighting matters:
    an unweighted mean over DB1B rows treats a 1-passenger itinerary the same as
    a 9-passenger one.
    """
    if not pairs:
        return [0.0] * len(qs)
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs) or 1.0
    out, acc, i = [], 0.0, 0
    for q in qs:
        target = q * total
        while i < len(pairs) - 1 and acc + pairs[i][1] < target:
            acc += pairs[i][1]
            i += 1
        out.append(pairs[i][0])
    return out


def fare_rows(conn) -> tuple:
    """
    Per-directional-pair fares from DB1B Market, and the vintage they came from.

    NONSTOP MARKETS ONLY (`market_coupons = 1`). A DB1B market fare is the whole
    journey's mile-prorated fare, so a one-stop itinerary's fare belongs to a
    two-leg journey and attributing it to a single leg would overstate that leg.
    Restricting to single-coupon markets gives a fare for a passenger who flew
    exactly this pair nonstop — which is what a route's ticket price means here.

    Still an approximation: DB1B is a 10% sample, so thin pairs are noisy, and
    fares are in the sample's own year's dollars with no deflator applied.
    """
    if not _has(conn, "db1b_market"):
        return {}, None
    rows = conn.execute("""
        SELECT origin, dest, market_fare, passengers
          FROM db1b_market
         WHERE market_coupons = 1 AND market_fare > 0 AND passengers > 0
        """).fetchall()
    by_pair = {}
    for r in rows:
        by_pair.setdefault((r["origin"], r["dest"]), []).append(
            (r["market_fare"], r["passengers"]))

    out = {}
    for key, vals in by_pair.items():
        pax = sum(w for _, w in vals)
        p25, p50, p75 = _weighted_quantiles(vals)
        out[key] = {"mean_fare": round(sum(v * w for v, w in vals) / pax, 2),
                    "fare_p25": round(p25, 2), "fare_median": round(p50, 2),
                    "fare_p75": round(p75, 2), "fare_sample_pax": round(pax, 1)}
    v = conn.execute("SELECT MIN(year) lo, MAX(year) hi, MIN(quarter) q1, "
                     "MAX(quarter) q2 FROM db1b_market").fetchone()
    vintage = {"years": [v["lo"], v["hi"]], "quarters": [v["q1"], v["q2"]]}
    return out, vintage


def connecting_rows(conn) -> dict:
    """
    Per-SEGMENT connecting share from DB1B Coupon.

    A coupon IS a segment, which is why this cannot come from the Market table:
    Market knows only the whole journey and cannot attribute a connection to a
    specific leg. Share = passengers on this segment whose itinerary has more
    than one coupon, over all passengers on the segment.
    """
    if not _has(conn, "db1b_coupon"):
        return {}
    rows = conn.execute("""
        SELECT c.origin AS o, c.dest AS d,
               SUM(CASE WHEN n.cnt > 1 THEN c.passengers ELSE 0 END) AS conn_pax,
               SUM(c.passengers) AS all_pax
          FROM db1b_coupon c
          JOIN (SELECT itin_id, COUNT(*) AS cnt FROM db1b_coupon
                 GROUP BY itin_id) n ON n.itin_id = c.itin_id
      GROUP BY c.origin, c.dest""").fetchall()
    out = {}
    for r in rows:
        if (r["all_pax"] or 0) > 0:
            out[(r["o"], r["d"])] = round((r["conn_pax"] or 0) / r["all_pax"], 4)
    return out


# A 7-parameter fit on a handful of routes interpolates noise: the fixture corpus
# (20 routes) produces R² = -3044. Below this many rows, or with a fit that is
# worse than predicting the mean, the coefficients are WITHHELD so the provider
# falls through to SYNTHETIC rather than serving nonsense as a "comparable route".
MIN_GRAVITY_ROWS = 200


def fit_gravity(routes: list, airports: list) -> dict:
    """
    Tier-2 fallback, fitted on the Tier-1 pairs:

        log(demand) ~ b0 + b1·log(out_pax_o) + b2·log(in_pax_d)
                         + b3·log(dist) + b4·log(dist)² + b5·hub30 + b6·hub10

    "Airport size" is the measured T-100 marginal for each endpoint, so the
    comparable-route estimate is calibrated on real data rather than invented —
    this IS the "size of origin and destination" fallback, fitted. b1 and b2 come
    out at about +0.50 each, which is textbook gravity: near-unit elasticity on
    the product of the two endpoint masses.

    A size-interaction term was tested and REJECTED despite being more accurate;
    see routedata.gravity_features for why monotonicity won.

    DERIVED. Accuracy is reported in the cross_validation block, not asserted.
    """
    coef, r2, n, calib = _fit(routes, airports)
    from airlinesim.routedata import GRAVITY_TERMS
    out = {"terms": list(GRAVITY_TERMS), "coefficients": coef,
           "calibration": calib, "r_squared": r2, "n": n,
           "cross_validation": cross_validate(routes, airports)}
    if n < MIN_GRAVITY_ROWS or r2 <= 0.0:
        out["withheld"] = (
            f"coefficients withheld: n={n} (need >= {MIN_GRAVITY_ROWS}) "
            f"and R²={r2} (need > 0). Tier-2 COMPARABLE estimates are disabled; "
            f"unknown pairs resolve SYNTHETIC instead of being served a fit that "
            f"interpolates noise.")
        out["coefficients_rejected"] = coef
        out["coefficients"] = []
    return out


# ============================================================
# writing the snapshot
# ============================================================

def provider_from_warehouse(conn, corpus_airports: int = CORPUS_AIRPORTS,
                            min_pax_per_day: float = MIN_PAX_PER_DAY):
    """
    The WAREHOUSE backend of Option C: build a live RouteDataProvider straight
    from SQLite, with no snapshot on disk. Same aggregation functions the
    snapshot is written from, so both backends share one code path and one set
    of interpretive rules — that was the whole point of putting them behind one
    interface.

    Note the dependency direction: btsdata imports routedata, never the reverse.
    """
    from airlinesim.routedata import RouteDataProvider
    table = volume_table(conn)
    lo, hi, days = span_days(conn, table)
    airports = airport_rows(conn, table, corpus_airports)
    routes = route_rows(conn, table, {a["iata"] for a in airports}, min_pax_per_day)
    gravity = fit_gravity(routes, airports)
    manifest = {"volume_table": table, "years": [lo, hi],
                "span_days": round(days, 1), "backend": "warehouse",
                "demand_basis": sorted({r["demand_basis"] for r in routes}),
                "gravity": gravity}
    return RouteDataProvider.from_tables(routes, airports, gravity, manifest)


def _write_csv_gz(path: str, rows: list):
    if not rows:
        return 0
    cols = list(rows[0].keys())
    with gzip.open(path, "wt", newline="", compresslevel=9) as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def distill(conn, out_dir: str, corpus_airports: int = CORPUS_AIRPORTS,
            min_pax_per_day: float = MIN_PAX_PER_DAY, verbose=True) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    table = volume_table(conn)
    lo, hi, days = span_days(conn, table)

    airports = airport_rows(conn, table, corpus_airports)
    keep = {a["iata"] for a in airports}
    routes = route_rows(conn, table, keep, min_pax_per_day)

    # Fares and connecting share, when DB1B is loaded. Merged after the volume
    # aggregation so route_rows() stays purely about volumes, and zero-filled
    # otherwise so the snapshot's columns don't depend on which sources happened
    # to be present.
    fares, fare_vintage = fare_rows(conn)
    connecting = connecting_rows(conn)
    fare_hits = conn_hits = 0
    for r in routes:
        key = (r["origin"], r["dest"])
        f = fares.get(key)
        if f:
            r.update(f)
            fare_hits += 1
        else:
            r.update({"mean_fare": 0.0, "fare_p25": 0.0, "fare_median": 0.0,
                      "fare_p75": 0.0, "fare_sample_pax": 0.0})
        if key in connecting:
            r["connecting_share"] = connecting[key]
            conn_hits += 1
        else:
            r["connecting_share"] = -1.0     # -1 = unknown, not "zero connecting"

    gravity = fit_gravity(routes, airports)

    n_r = _write_csv_gz(os.path.join(out_dir, "routes.csv.gz"), routes)
    n_a = _write_csv_gz(os.path.join(out_dir, "airports.csv.gz"), airports)
    with open(os.path.join(out_dir, "gravity.json"), "w") as fh:
        json.dump(gravity, fh, indent=2)

    basis = {r["demand_basis"] for r in routes} or {"none"}
    manifest = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "volume_table": table,
        "years": [lo, hi],
        "span_days": round(days, 1),
        "passenger_class": PASSENGER_CLASS,
        "corpus_airports": len(airports),
        "routes": n_r,
        "airports": n_a,
        "demand_basis": sorted(basis),
        "target_load_factor": TARGET_LOAD_FACTOR,
        "min_pax_per_day": min_pax_per_day,
        # Fares carry their OWN vintage: DB1B collection ended Q2 2025, so the
        # fare window generally lags the volume window and implying one number
        # for both would be wrong.
        "fares": {"source": "db1b_market nonstop markets (market_coupons=1)",
                  "vintage": fare_vintage,
                  "routes_with_fare": fare_hits,
                  "coverage": round(fare_hits / len(routes), 4) if routes else 0.0},
        "connecting": {"source": "db1b_coupon (segment-level)",
                       "routes_with_share": conn_hits,
                       "coverage": round(conn_hits / len(routes), 4) if routes else 0.0,
                       "unknown_sentinel": -1.0},
        "gravity": gravity,
        # Per-field footing, so nothing downstream mistakes a guess for a fact.
        "provenance": {
            "MEASURED": ["pax_per_day", "distance_km", "m1..m12", "runway_m",
                         "out_pax_per_day", "in_pax_per_day"]
                        + (["seats_per_day", "deps_per_day", "load_factor"]
                           if table == "t100_segment" else []),
            "DERIVED": ["season_amp", "season_peak_day", "gravity.coefficients"]
                       + (["demand_per_day (de-censored)"]
                          if "decensored" in basis else []),
            "HEURISTIC": ["min_viable_seats", "max_viable_seats", "min_runway_m",
                          "est_gates", "est_fuel_l_per_day"]
                         + (["demand_per_day (CENSORED — equals flown pax; no "
                             "SEATS in T-100 Market, so it understates demand "
                             "on full routes)"] if "censored" in basis else []),
        },
        "known_gaps": ([] if table == "t100_segment" else [
            "T-100 Market carries no SEATS/departures/aircraft type: no load "
            "factor, no de-censoring, no measured seat window, nothing for "
            "equipment right-sizing."]) + ([] if fare_hits else [
            "No DB1B fares loaded: ticket prices remain engine defaults, and "
            "the traveler-segment mix uses route.py's global default rather "
            "than a per-route connecting split."]) + ([] if not fare_hits else [
            "Fares are nonstop-market DB1B in their own year's dollars with no "
            "deflator; the 10% sample makes thin pairs noisy."]),
    }
    with open(os.path.join(out_dir, "MANIFEST.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    if verbose:
        print(f"  volume table : {table} ({lo}-{hi}, {days:.0f} days)")
        print(f"  airports     : {n_a}")
        print(f"  routes       : {n_r}")
        print(f"  demand basis : {', '.join(sorted(basis))}")
        print(f"  gravity      : R²={gravity['r_squared']} on n={gravity['n']}")
        for f in ("routes.csv.gz", "airports.csv.gz", "gravity.json", "MANIFEST.json"):
            p = os.path.join(out_dir, f)
            if os.path.exists(p):
                print(f"    {f:20s} {os.path.getsize(p):>9,} bytes")
        for gap in manifest["known_gaps"]:
            print(f"  GAP: {gap}")
    return manifest
