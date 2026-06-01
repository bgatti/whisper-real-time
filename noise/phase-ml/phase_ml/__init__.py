"""phase_ml — aircraft activity classifier from ADS-B trajectories.

See README.md for design rationale. Layers:
    geometry  — math primitives (haversine, bearing, runway frame, curvature)
    airports  — multi-airport runway / pattern / entry-corridor database
    features  — windowed kinematic + geometric feature extraction
    oracle    — rule-based weak labels (port of classifyAircraft)
    maneuvers — PTS maneuver detectors (steep turn, S-turn, TAP, ED, slow flight, stall, ...)
    intent    — Bayesian multi-airport inbound predictor
    data_loader — read the on-disk tracks_*.json archives
"""

__version__ = "0.1.0"
