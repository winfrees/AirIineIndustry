"""
Magnetic declination, from the World Magnetic Model.

WHY THIS EXISTS
---------------
Aviation works in MAGNETIC directions — runway numbers, headings, VOR radials
— so "north" on a chart a pilot reads is usually magnetic north, not true
north. The network map can orient itself that way, and this is where the angle
comes from.

WHAT IT IS, HONESTLY
--------------------
MEASURED/DERIVED: the Gauss coefficients in ``data/wmm2020.cof`` are the
official World Magnetic Model, produced by NOAA NCEI and the British
Geological Survey for the US and UK defence departments. They are **public
domain** (a US Government work, released with no licence restriction), which
is the only reason they can be committed here — the same test the Natural
Earth base map had to pass.

The evaluation below is the standard spherical-harmonic synthesis from the WMM
technical report: degree 12, with linear secular variation to the epoch you
ask for. It is the real model, not a fit to it.

TWO LIMITS THAT MUST NOT BE PAPERED OVER
----------------------------------------
1. **This is WMM-2020, valid 2020.0-2025.0, and we are past that.** Beyond the
   validity window the linear secular-variation term is an EXTRAPOLATION and
   its error grows. Over the contiguous US declination drifts on the order of
   0.1 deg/year, so a couple of years out is a few tenths of a degree — fine
   for orienting a map, not fine for navigation. `validity_note()` says so and
   the GUI repeats it. Dropping a newer .COF in and bumping `EPOCH` is the
   whole upgrade.

2. **DECLINATION IS NOT A CONSTANT ACROSS A CONTINENT.** It runs from roughly
   +13 deg (east) in Washington State to -16 deg (west) in Maine — about 29
   degrees corner to corner. So NO single rotation makes a map of the lower 48
   magnetic-north-up everywhere; it can only be exact along one reference
   meridian, and every other longitude is off by the difference. A map that
   claimed otherwise would be lying. `declination_range()` returns the actual
   spread so the GUI can state it rather than imply a precision it hasn't got.

Nothing here is used by the simulation — it orients a map. It is deliberately
free of engine imports so it stays that way.
"""
from __future__ import annotations

import math
from pathlib import Path

COF_PATH = Path(__file__).parent / "data" / "wmm2020.cof"

# WGS-84 ellipsoid and the model's reference radius, from the WMM report.
_A = 6378.137            # semi-major axis, km
_F = 1.0 / 298.257223563  # flattening
_B = _A * (1.0 - _F)      # semi-minor axis, km
_RE = 6371.2             # geomagnetic reference radius, km

_MAX_N = 12

_MODEL: dict = {}


def _load():
    """Parse the .COF into g/h coefficient tables and their secular variation."""
    if _MODEL:
        return _MODEL
    g = [[0.0] * (_MAX_N + 1) for _ in range(_MAX_N + 1)]
    h = [[0.0] * (_MAX_N + 1) for _ in range(_MAX_N + 1)]
    gd = [[0.0] * (_MAX_N + 1) for _ in range(_MAX_N + 1)]
    hd = [[0.0] * (_MAX_N + 1) for _ in range(_MAX_N + 1)]
    epoch, name = 2020.0, "WMM"
    with COF_PATH.open() as fh:
        for line in fh:
            parts = line.split()
            if not parts or parts[0].startswith("9999"):
                continue
            if len(parts) >= 2 and "." in parts[0] and not parts[1].isdigit():
                epoch = float(parts[0])
                name = parts[1]
                continue
            if len(parts) < 6:
                continue
            n, m = int(parts[0]), int(parts[1])
            if n > _MAX_N:
                continue
            g[n][m], h[n][m] = float(parts[2]), float(parts[3])
            gd[n][m], hd[n][m] = float(parts[4]), float(parts[5])
    _MODEL.update(g=g, h=h, gd=gd, hd=hd, epoch=epoch, name=name)
    return _MODEL


def _legendre(n_max, ct, st):
    """
    Schmidt semi-normalised associated Legendre functions P[n][m](cos theta)
    and their derivatives with respect to THETA (colatitude), given cos theta
    and sin theta.

    Written as a direct stable recurrence rather than the in-place
    normalise-as-you-go form in the reference C code, because the two are easy
    to mix up: normalising a recurrence that is already Schmidt-normalised
    applies the factor twice, which is silently wrong everywhere except at the
    poles. `scenario_map` checks this against the six published WMM test
    values so a wrong recurrence can't ship looking plausible.
    """
    P = [[0.0] * (n_max + 1) for _ in range(n_max + 1)]
    dP = [[0.0] * (n_max + 1) for _ in range(n_max + 1)]
    P[0][0], dP[0][0] = 1.0, 0.0
    for n in range(1, n_max + 1):
        # sectoral term P[n][n]; the n == 1 factor is 1, not sqrt(1/2)
        c = 1.0 if n == 1 else math.sqrt((2 * n - 1) / (2.0 * n))
        P[n][n] = c * st * P[n - 1][n - 1]
        dP[n][n] = c * (st * dP[n - 1][n - 1] + ct * P[n - 1][n - 1])
        for m in range(0, n):
            denom = math.sqrt(float(n * n - m * m))
            a1 = (2 * n - 1) / denom
            a2 = math.sqrt(((n - 1) ** 2 - m * m) / float(n * n - m * m))
            p2 = P[n - 2][m] if n - 2 >= m else 0.0
            d2 = dP[n - 2][m] if n - 2 >= m else 0.0
            P[n][m] = a1 * ct * P[n - 1][m] - a2 * p2
            dP[n][m] = a1 * (ct * dP[n - 1][m] - st * P[n - 1][m]) - a2 * d2
    return P, dP


def field(lat_deg: float, lon_deg: float, year: float, alt_km: float = 0.0):
    """
    Geomagnetic field at a geodetic point, as (X north, Y east, Z down) in nT.

    `year` is a decimal year (2026.5 = mid-2026). Secular variation is applied
    linearly from the model epoch — see the validity note in the module
    docstring before trusting a date outside the model's window.
    """
    m = _load()
    dt = year - m["epoch"]
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)

    # geodetic -> geocentric
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    a2, b2 = _A * _A, _B * _B
    rho = math.hypot(_A * cos_lat, _B * sin_lat)
    r = math.sqrt(alt_km * alt_km + 2.0 * alt_km * rho
                  + (a2 * a2 * cos_lat ** 2 + b2 * b2 * sin_lat ** 2) / (rho * rho))
    cd = (alt_km + rho) / r
    sd = (a2 - b2) / rho * sin_lat * cos_lat / r
    sin_gc = sin_lat * cd - cos_lat * sd
    cos_gc = cos_lat * cd + sin_lat * sd

    # theta is COLATITUDE, so cos(theta) = sin(geocentric latitude)
    P, dP = _legendre(_MAX_N, sin_gc, cos_gc)
    ratio = _RE / r
    Xp = Yp = Zp = 0.0
    for n in range(1, _MAX_N + 1):
        rn = ratio ** (n + 2)
        for k in range(0, n + 1):
            gnm = m["g"][n][k] + dt * m["gd"][n][k]
            hnm = m["h"][n][k] + dt * m["hd"][n][k]
            cos_k, sin_k = math.cos(k * lon), math.sin(k * lon)
            # +dP/dtheta: with theta measured from the NORTH pole, the
            # northward component carries no leading minus (the sign check is
            # the dipole at the equator, which must point north)
            Xp += rn * (gnm * cos_k + hnm * sin_k) * dP[n][k]
            Yp += rn * k * (gnm * sin_k - hnm * cos_k) * P[n][k] / max(cos_gc, 1e-10)
            Zp -= rn * (n + 1) * (gnm * cos_k + hnm * sin_k) * P[n][k]

    # geocentric -> geodetic components
    X = Xp * cd + Zp * sd
    Z = Zp * cd - Xp * sd
    return X, Yp, Z


def declination(lat_deg: float, lon_deg: float, year: float) -> float:
    """
    Magnetic declination (variation) in DEGREES, east positive.

    This is the angle from true north to magnetic north: at +10 the compass
    points 10 degrees east of true north, so a magnetic-north-up map is the
    true-north-up map rotated 10 degrees.
    """
    X, Y, _Z = field(lat_deg, lon_deg, year)
    return math.degrees(math.atan2(Y, X))


def declination_range(bbox, year: float, steps: int = 6):
    """
    (min, max) declination over a lon/lat window, in degrees east.

    The reason this exists: a continental map CANNOT be magnetic-north-up
    everywhere, and the honest way to say so is to show the spread rather than
    quietly orient to one point and imply it holds throughout.
    """
    w, s, e, n = bbox
    vals = []
    for i in range(steps + 1):
        for j in range(steps + 1):
            lon = w + (e - w) * i / steps
            lat = s + (n - s) * j / steps
            vals.append(declination(lat, lon, year))
    return min(vals), max(vals)


def model_name() -> str:
    return _load()["name"]


def epoch() -> float:
    return _load()["epoch"]


def validity_note(year: float) -> str:
    """Plain-language statement of whether the answer is inside the model."""
    ep = epoch()
    if ep <= year <= ep + 5.0:
        return (f"{model_name()} (epoch {ep:.1f}), valid {ep:.1f}-{ep + 5:.1f}; "
                f"evaluated at {year:.1f}")
    return (f"{model_name()} (epoch {ep:.1f}) is valid {ep:.1f}-{ep + 5:.1f} and "
            f"this is evaluated at {year:.1f} — the secular-variation term is "
            f"EXTRAPOLATED, worth a few tenths of a degree over the US. Fine "
            f"for orienting a map, not for navigation. Drop a newer .COF into "
            f"airlinesim/data/ to refresh it.")
