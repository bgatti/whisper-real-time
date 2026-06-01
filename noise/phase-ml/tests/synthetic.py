"""Synthetic-trajectory generators for unit tests.

Every generator returns a list of `Point`s with realistic-looking lat/lon/alt/ts
so that enrich() can compute well-defined gs/vs/track from them. We work in the
local tangent plane around (lat0, lon0) and translate back to lat/lon at the end.
"""
from __future__ import annotations

import math
from typing import Iterator

from phase_ml.data_loader import Point
from phase_ml.geometry import DEG_TO_RAD, KT_TO_FPS, xy_nm_to_lat_lon


def straight_track(
    *,
    lat0: float = 40.0,
    lon0: float = -105.0,
    alt_ft: float = 7000.0,
    heading_deg: float = 90.0,
    speed_kt: float = 120.0,
    duration_s: float = 60.0,
    sample_s: float = 2.0,
    t0: float = 0.0,
) -> list[Point]:
    """Constant-heading constant-speed cruise."""
    n = int(duration_s / sample_s) + 1
    out: list[Point] = []
    speed_nm_per_s = speed_kt / 3600.0
    theta = heading_deg * DEG_TO_RAD
    sin_t, cos_t = math.sin(theta), math.cos(theta)
    for i in range(n):
        t = i * sample_s
        d = t * speed_nm_per_s
        x = sin_t * d
        y = cos_t * d
        lat, lon = xy_nm_to_lat_lon(x, y, lat0, lon0)
        out.append(Point(lat=lat, lon=lon, alt_msl_ft=alt_ft, ts_unix=t0 + t))
    return out


def steep_turn_track(
    *,
    lat0: float = 40.0,
    lon0: float = -105.0,
    alt_ft: float = 7000.0,
    speed_kt: float = 100.0,
    bank_deg: float = 50.0,
    turn_total_deg: float = 360.0,
    sample_s: float = 2.0,
    t0: float = 0.0,
    direction: int = 1,           # +1 right, -1 left
) -> list[Point]:
    """Coordinated steep turn at constant altitude."""
    # Bank → turn rate: ω = g * tan(bank) / V
    v_fps = speed_kt * KT_TO_FPS
    omega_rad_s = 32.174 * math.tan(bank_deg * DEG_TO_RAD) / v_fps
    turn_rate_dps = omega_rad_s * 180.0 / math.pi * direction
    duration_s = abs(turn_total_deg / turn_rate_dps)
    n = int(duration_s / sample_s) + 1

    # Orbit centre is to the right of the initial heading if turning right.
    radius_nm = (speed_kt / 3600.0) / omega_rad_s   # nm
    # Start heading north; circle clockwise (right).
    out: list[Point] = []
    initial_heading = 0.0   # north
    for i in range(n):
        t = i * sample_s
        angle_swept = turn_rate_dps * t
        heading = initial_heading + angle_swept
        # Position on the circle: centre is offset radius_nm to the right (east) of start.
        cx = radius_nm * direction
        cy = 0.0
        # Aircraft angle from centre, measured CCW from +x axis. Started at -direction*x axis,
        # which is heading 0 at theta_init = 180° (or 0° for left turn).
        theta0 = math.pi if direction > 0 else 0.0
        theta = theta0 + math.radians(angle_swept) * direction
        x = cx + radius_nm * math.cos(theta) * direction
        y = cy + radius_nm * math.sin(theta) * direction
        lat, lon = xy_nm_to_lat_lon(x, y, lat0, lon0)
        out.append(Point(lat=lat, lon=lon, alt_msl_ft=alt_ft, ts_unix=t0 + t))
    return out


def s_turns_track(
    *,
    lat0: float = 40.0,
    lon0: float = -105.0,
    alt_ft: float = 6000.0,
    speed_kt: float = 100.0,
    n_legs: int = 4,
    leg_turn_deg: float = 180.0,
    bank_deg: float = 30.0,
    sample_s: float = 2.0,
    t0: float = 0.0,
) -> list[Point]:
    """S-turn maneuver: n_legs alternating turns of leg_turn_deg each."""
    points: list[Point] = []
    current_t = t0
    current_xy = (0.0, 0.0)
    current_heading = 90.0  # east

    v_fps = speed_kt * KT_TO_FPS
    omega_rad_s = 32.174 * math.tan(bank_deg * DEG_TO_RAD) / v_fps
    radius_nm = (speed_kt / 3600.0) / omega_rad_s

    for leg_i in range(n_legs):
        direction = 1 if leg_i % 2 == 0 else -1
        turn_rate_dps = math.degrees(omega_rad_s) * direction
        duration_s = abs(leg_turn_deg / turn_rate_dps)
        n = int(duration_s / sample_s) + 1
        # Centre: 90° to the right of current heading for a right turn, to the left for a left.
        perp = current_heading + (90.0 * direction)
        perp_rad = math.radians(perp)
        cx = current_xy[0] + radius_nm * math.sin(perp_rad)
        cy = current_xy[1] + radius_nm * math.cos(perp_rad)
        # Aircraft starts at angle (from centre): opposite of perp direction.
        start_angle = math.atan2(current_xy[0] - cx, current_xy[1] - cy)  # north up
        for i in range(n):
            t = i * sample_s
            swept = math.radians(turn_rate_dps * t)
            ang = start_angle + swept
            x = cx + radius_nm * math.sin(ang)
            y = cy + radius_nm * math.cos(ang)
            lat, lon = xy_nm_to_lat_lon(x, y, lat0, lon0)
            points.append(Point(lat=lat, lon=lon, alt_msl_ft=alt_ft, ts_unix=current_t + t))
        # Update state for next leg
        current_xy = (cx + radius_nm * math.sin(start_angle + math.radians(turn_rate_dps * duration_s)),
                      cy + radius_nm * math.cos(start_angle + math.radians(turn_rate_dps * duration_s)))
        current_heading = (current_heading + leg_turn_deg * direction) % 360.0
        current_t += duration_s
    return points


def emergency_descent_track(
    *,
    lat0: float = 40.0,
    lon0: float = -105.0,
    alt_start_ft: float = 12000.0,
    speed_kt: float = 130.0,
    vs_fpm: float = -2500.0,
    duration_s: float = 90.0,
    spiral: bool = True,
    bank_deg: float = 40.0,
    sample_s: float = 2.0,
    t0: float = 0.0,
) -> list[Point]:
    """Steep descent. If `spiral`, in a constant-bank right turn."""
    points: list[Point] = []
    v_fps = speed_kt * KT_TO_FPS
    omega_rad_s = 32.174 * math.tan(bank_deg * DEG_TO_RAD) / v_fps if spiral else 0.0
    radius_nm = ((speed_kt / 3600.0) / omega_rad_s) if omega_rad_s > 0 else 0.0
    cx, cy = radius_nm, 0.0
    n = int(duration_s / sample_s) + 1
    for i in range(n):
        t = i * sample_s
        alt = alt_start_ft + vs_fpm * (t / 60.0)
        if spiral:
            theta = math.pi + omega_rad_s * t
            x = cx + radius_nm * math.cos(theta)
            y = cy + radius_nm * math.sin(theta)
        else:
            # Straight south
            x = 0.0
            y = -(speed_kt / 3600.0) * t
        lat, lon = xy_nm_to_lat_lon(x, y, lat0, lon0)
        points.append(Point(lat=lat, lon=lon, alt_msl_ft=alt, ts_unix=t0 + t))
    return points


def inbound_approach_track(
    *,
    airport_lat: float = 40.0394,
    airport_lon: float = -105.2258,
    field_elev_ft: float = 5288.0,
    runway_heading_deg: float = 260.0,  # land into the west
    start_dist_nm: float = 8.0,
    start_alt_msl_ft: float = 8000.0,
    speed_kt: float = 100.0,
    sample_s: float = 2.0,
    t0: float = 0.0,
) -> list[Point]:
    """Straight-in approach: align with runway, descend at 3°, slow to landing speed.

    Lands on the threshold (alt ≈ field_elev_ft) over `start_dist_nm` nm.
    """
    # Approach heading is opposite of the runway departure heading.
    approach_heading = (runway_heading_deg + 180.0) % 360.0
    # Place start point start_dist_nm out along the approach heading from threshold.
    theta = math.radians(approach_heading)
    start_x = -math.sin(theta) * start_dist_nm   # negative: we'll *fly* in the +theta direction
    start_y = -math.cos(theta) * start_dist_nm
    duration_s = (start_dist_nm / speed_kt) * 3600.0
    n = int(duration_s / sample_s) + 1
    vs_fpm = -(start_alt_msl_ft - field_elev_ft) / (duration_s / 60.0)
    points: list[Point] = []
    for i in range(n):
        t = i * sample_s
        frac = t / duration_s
        x = start_x * (1 - frac)
        y = start_y * (1 - frac)
        alt = start_alt_msl_ft + vs_fpm * (t / 60.0)
        lat, lon = xy_nm_to_lat_lon(x, y, airport_lat, airport_lon)
        points.append(Point(lat=lat, lon=lon, alt_msl_ft=alt, ts_unix=t0 + t))
    return points
