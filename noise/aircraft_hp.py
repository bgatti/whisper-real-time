"""ICAO type designator -> engine horsepower (total, all engines).

Values are rough published-spec guesses. For turboprops/jets, HP is set so the
10*log10(HP/100) scaling in the noise model lands near published flyover dBA.
Gliders and balloons get 0 (silent). Unknown types fall back to DEFAULT_HP.
"""

DEFAULT_HP = 180

HP_BY_ICAO = {
    # --- piston singles ---
    "C140": 90,  "C150": 100, "C152": 110, "C162": 100,
    "C172": 180, "C72R": 180, "C177": 180,
    "C182": 230, "C82R": 230, "C185": 300,
    "C206": 300, "C210": 300, "C240": 230,
    "DA40": 180, "DV20": 100,
    "SR20": 215, "SR22": 310, "S22T": 315, "SP20": 215,
    "COL3": 310, "COL4": 310,  # Columbia 300/350/400
    "M20P": 200, "M20T": 280,
    "P28A": 160, "P28B": 180, "P28R": 200, "P28T": 300, "P32R": 300,
    "PA11": 90,  "PA12": 115, "PA16": 108, "PA18": 150,
    "BE35": 285, "BE36": 300,
    "AA5": 150,
    "CH7A": 118, "CH7B": 150, "CRUZ": 100,
    "T34P": 285, "T6": 600, "HUSK": 200,
    "RV4": 160,  "RV6": 180, "RV7": 180, "RV8": 180, "RV12": 100,
    "LNC2": 200, "LGEZ": 180, "ERCO": 75,
    "BDOG": 200, "EAGL": 180, "FOX": 180, "ULAC": 100, "VL3": 100, "EXPR": 210,
    "AR65": 420,  # AT-6 / Arrow variant placeholder
    # --- piston twins (total HP) ---
    "BE55": 520, "BE76": 360, "BE40": 800,
    "C310": 520, "C421": 750, "C425": 900,
    "PA34": 440, "PA44": 360, "P337": 420, "P06T": 700,
    "SW3": 1400,
    # --- turboprops (approximate SHP equivalents) ---
    "BE30": 1250, "B350": 2100,
    "PC12": 1200, "M600": 600,
    "TBM7": 700, "TBM8": 850, "TBM9": 850,
    "PA46": 350, "P46T": 500, "PAY1": 1000,
    "AC90": 1400, "C208": 675,
    # --- jets (nominal equivalent HP so model gives a plausible +dB offset) ---
    "B38M": 8000, "B752": 10000,
    "C25C": 2500, "C25M": 2500, "C510": 1800, "C525": 2200,
    "C55B": 3000, "C560": 3500, "C56X": 3500, "C650": 4500,
    "C680": 4500, "C68A": 4500, "C700": 5000, "C750": 5500,
    "CL30": 4500, "CL35": 4500, "CL60": 5000,
    "E135": 5000, "E145": 5000, "E55P": 2500,
    "F2TH": 5000, "F900": 6000,
    "G200": 5000, "GLEX": 7000, "GLF4": 6500, "GLF5": 7000,
    "H25B": 5000, "HDJT": 1500,
    "LJ31": 3000, "LJ45": 3500, "LJ60": 4500, "LJ70": 4500,
    # --- helicopters ---
    "R44": 245, "B06": 420, "B407": 700, "H500": 420, "AS50": 590, "UH1": 1400,
    # --- silent ---
    "GLID": 0, "BALL": 0, "XNOS": 0, "COY2": 0,
}


def hp_for_type(icao_type: str) -> int:
    if not icao_type:
        return DEFAULT_HP
    return HP_BY_ICAO.get(icao_type.upper(), DEFAULT_HP)
