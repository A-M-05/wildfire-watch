"""
Fire spread projection using the Rothermel (1972) rate of spread model.

Given a predicted linear spread rate (km/hr), projects fire area at
multiple time horizons using the expanding circle approximation.

The Rothermel model is the standard used by USFS, CAL FIRE, and NWCG
for operational fire behavior prediction (BEHAVE, FARSITE, FlamMap).

References:
  Rothermel, R.C. (1972) USDA Forest Service Research Paper INT-115
  Andrews, P.L. (2018) USDA Forest Service RMRS-GTR-266
"""

import math


# Southern California fuel model parameters (Rothermel fuel models)
# Chaparral (model 4) dominates SoCal wildland-urban interface.
# Brush (model 5) covers shorter coastal shrubland.
FUEL_MODELS = {
    "chaparral":    {"R0_ref": 12.0, "wind_b": 0.0185, "wind_c": 0.85, "m_ext": 20.0},
    "brush":        {"R0_ref": 9.0,  "wind_b": 0.0100, "wind_c": 0.85, "m_ext": 20.0},
    "grass":        {"R0_ref": 6.0,  "wind_b": 0.0082, "wind_c": 1.00, "m_ext": 15.0},
    "timber_litter":{"R0_ref": 4.0,  "wind_b": 0.0050, "wind_c": 0.80, "m_ext": 25.0},
}

# SoCal lat/lon zones → dominant fuel model
# Rough approximation: coastal = brush, inland valleys = chaparral,
# mountains = timber, desert edge = grass
def _infer_fuel_model(lat: float, lon: float) -> str:
    if lon < -118.0:                  # coastal (Malibu, Ventura)
        return "brush"
    elif lat > 34.3:                  # mountains (San Gabriel, San Bernardino)
        return "timber_litter"
    elif lon > -117.2:                # desert edge (Palm Springs area)
        return "grass"
    else:                             # inland SoCal (most fires)
        return "chaparral"


def rothermel_spread_rate(
    wind_speed_ms: float,
    fuel_moisture_pct: float,
    slope_deg: float,
    lat: float = 34.0,
    lon: float = -118.0,
) -> float:
    """Compute linear fire spread rate in km/hr using simplified Rothermel model.

    Args:
        wind_speed_ms: wind speed at 6m height in m/s (converted to midflame mph internally)
        fuel_moisture_pct: dead fine fuel moisture content in %
        slope_deg: terrain slope in degrees
        lat, lon: used to infer SoCal fuel model

    Returns:
        Linear rate of spread in km/hr
    """
    fuel = FUEL_MODELS[_infer_fuel_model(lat, lon)]

    # Convert wind: m/s at 6m → midflame mph (midflame ≈ 0.4 * 6m wind)
    wind_mph = wind_speed_ms * 0.4 * 2.237

    # Moisture damping — spread drops sharply above extinction moisture
    m_ext = fuel["m_ext"]
    moisture_clamped = max(1.0, min(fuel_moisture_pct, m_ext - 1))
    f_moist = (m_ext - moisture_clamped) / (m_ext - 3.0)
    R0 = fuel["R0_ref"] * math.exp(-0.0144 * max(0, f_moist))

    # Wind factor (Rothermel φ_w)
    phi_w = fuel["wind_b"] * max(0, wind_mph) ** fuel["wind_c"]

    # Slope factor (Rothermel φ_s) — capped at 60° (tan goes to infinity)
    slope_tan = math.tan(math.radians(min(slope_deg, 58.0)))
    phi_s = 5.275 * (0.05 ** 0.3) * slope_tan ** 2   # β=0.05 for chaparral

    # Rate of spread in cm/s → km/hr
    ros_cms = R0 * (1.0 + phi_w) * (1.0 + phi_s)
    return round(ros_cms * 0.036, 4)   # 1 cm/s = 0.036 km/hr


def project_area(
    spread_rate_km_hr: float,
    current_area_km2: float = 0.01,
    time_hours: float = 1.0,
) -> float:
    """Project fire area at a future time using expanding circle model.

    The fire perimeter expands at the linear spread rate in all directions.
    Starting from a circle of current_area, the radius grows by spread_rate * t.

    Args:
        spread_rate_km_hr: linear rate of spread from Rothermel model (km/hr)
        current_area_km2: current fire area in km² (default 0.01 = 1 hectare)
        time_hours: projection horizon in hours

    Returns:
        Projected fire area in km²
    """
    # Current equivalent circular radius
    r0 = math.sqrt(max(current_area_km2, 0.001) / math.pi)
    # Radius after time t
    r_t = r0 + spread_rate_km_hr * time_hours
    return round(math.pi * r_t ** 2, 4)


def full_projection(
    spread_rate_km_hr: float,
    current_area_km2: float = 0.01,
) -> dict:
    """Return area projections at standard time horizons.

    Used by the enrichment Lambda and the dispatch panel (#28) to show
    the dispatcher and residents how the fire is expected to grow.
    """
    horizons = [0.5, 1, 3, 6, 12, 24]
    return {
        f"{h}hr": project_area(spread_rate_km_hr, current_area_km2, h)
        for h in horizons
    }
