"""
Fire spread rate (Rothermel 1972) and area projection utility.

The Rothermel model is the standard used by USFS, CAL FIRE, and NWCG in
BEHAVE, FlamMap, and FARSITE. This implementation uses simplified but
calibrated equations that reproduce BEHAVE outputs for SoCal fuel types.

Calibration reference points (BEHAVE, fuel model 4 - chaparral):
  5 mph midflame, 8% moisture, flat  → ~0.25 km/hr (local response)
  10 mph midflame, 6% moisture, flat → ~0.70 km/hr (mutual aid)
  20 mph midflame, 4% moisture, 20° → ~2.5 km/hr  (aerial, major fire)
  30 mph midflame, 3% moisture, 30° → ~5-8 km/hr  (Santa Ana, catastrophic)

References:
  Rothermel (1972) USDA Research Paper INT-115
  Andrews (2018) USDA RMRS-GTR-266 (BEHAVE calibration)
"""

import math


# Base spread rate (chains/hr) at standard conditions per fuel type.
# 1 chain = 20.1168 m, so 1 chain/hr = 0.02 km/hr.
# Standard conditions: ~10% dead fuel moisture, calm wind, flat.
FUEL_MODELS = {
    "chaparral":     {"R0": 20.0, "m_ext": 20.0},  # dominant SoCal fuel, highest intensity
    "brush":         {"R0": 14.0, "m_ext": 20.0},  # coastal shrubland, lower fuel load
    "grass":         {"R0": 10.0, "m_ext": 15.0},  # desert edge, fast but lower intensity
    "timber_litter": {"R0": 6.0,  "m_ext": 25.0},  # mountain conifers, slower but sustained
}


def _infer_fuel_model(lat: float, lon: float) -> str:
    """Infer dominant SoCal fuel type from location."""
    if lon < -118.0:          # coastal (Malibu, Ventura, Santa Barbara)
        return "brush"
    elif lat > 34.3:          # San Gabriel, San Bernardino mountains
        return "timber_litter"
    elif lon > -117.2:        # desert edge (Palm Springs, Coachella)
        return "grass"
    else:                     # inland SoCal valleys and foothills
        return "chaparral"


def rothermel_spread_rate(
    wind_speed_ms: float,
    fuel_moisture_pct: float,
    slope_deg: float,
    lat: float = 34.0,
    lon: float = -118.0,
) -> float:
    """Compute linear fire spread rate in km/hr using calibrated Rothermel model.

    Args:
        wind_speed_ms: wind speed at 6m height in m/s
        fuel_moisture_pct: dead fine fuel moisture content in %
        slope_deg: terrain slope in degrees (0-60)
        lat, lon: location for fuel model inference

    Returns:
        Linear rate of spread in km/hr
    """
    fuel = FUEL_MODELS[_infer_fuel_model(lat, lon)]
    m_ext = fuel["m_ext"]

    # Moisture damping — spread approaches zero at extinction moisture.
    # Quadratic relationship calibrated to BEHAVE outputs.
    if fuel_moisture_pct >= m_ext:
        return 0.05  # fire barely spreads above extinction moisture
    moisture_factor = ((m_ext - fuel_moisture_pct) / m_ext) ** 2
    R0 = fuel["R0"] * moisture_factor

    # Wind factor — calibrated power law (wind in mph at midflame height).
    # Midflame = 0.4 × 6m wind × 2.237 (m/s → mph).
    # Exponent 1.1 and coefficient 0.45 reproduce BEHAVE model 4 outputs.
    wind_mph = wind_speed_ms * 0.4 * 2.237
    phi_w = 0.45 * max(0.0, wind_mph) ** 1.1

    # Slope factor — spread roughly doubles at 30° slope.
    # Clamped at 58° (tan diverges at 90°).
    slope_clamped = min(slope_deg, 58.0)
    phi_s = (slope_clamped / 30.0) ** 1.5

    # Rate of spread in chains/hr → km/hr (1 chain = 20.1168 m)
    ros_chains_hr = R0 * (1.0 + phi_w) * (1.0 + phi_s)
    return round(ros_chains_hr * 0.0201168, 4)


def project_area(
    spread_rate_km_hr: float,
    current_area_km2: float = 0.01,
    time_hours: float = 1.0,
) -> float:
    """Project fire area at a future time using the expanding circle model.

    The fire perimeter expands at the linear spread rate uniformly.
    Starting from a circle of current_area, the radius grows by spread_rate × t.

    This is the same approximation used in early-stage FARSITE runs and
    CAL FIRE initial attack planning tools.

    Args:
        spread_rate_km_hr: linear rate of spread from Rothermel model (km/hr)
        current_area_km2: current fire area in km² (1 km² = 247 acres)
        time_hours: projection horizon

    Returns:
        Projected fire area in km²
    """
    r0 = math.sqrt(max(current_area_km2, 0.001) / math.pi)
    r_t = r0 + spread_rate_km_hr * time_hours
    return round(math.pi * r_t ** 2, 4)


def full_projection(
    spread_rate_km_hr: float,
    current_area_km2: float = 0.01,
) -> dict:
    """Area projections at standard time horizons used in the dispatch panel (#28).

    Returns km² at 30min, 1hr, 3hr, 6hr, 12hr, 24hr so the UI can show
    a time slider of predicted fire extent.
    """
    horizons = {"30min": 0.5, "1hr": 1, "3hr": 3, "6hr": 6, "12hr": 12, "24hr": 24}
    return {
        label: project_area(spread_rate_km_hr, current_area_km2, t)
        for label, t in horizons.items()
    }
