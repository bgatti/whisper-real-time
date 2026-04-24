"""
Health check for the noise/ subproject.

Repeatable, zero-dependency. Two dimensions:

1. Code health  — every .py file in noise/ must parse (AST compile).
2. Server health — the Vite dev server at :5174 must answer the API
   endpoints documented in API.md and in vite.config.js with HTTP 200 and
   well-formed JSON for the GET routes.

Exits 0 if everything is OK, 1 otherwise. Designed to be run repeatedly
(e.g. from a loop or CI) — it is read-only and has no side effects.

Usage:
    python noise/health_check.py
    python noise/health_check.py --base http://localhost:5174 --tail N12JA
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
import urllib.error
import urllib.request

NOISE_DIR = os.path.dirname(os.path.abspath(__file__))

# (path, query, expected_keys_in_json_or_None_for_html)
GET_ENDPOINTS = [
    ("/",                         None,                          None),
    ("/api/offenses",             "tail={tail}",                 ("tail", "offenses")),
    ("/api/offenses/segments",    "tail={tail}&hours=24",        ("tail",)),
    ("/api/offenses/active",      "",                            None),   # shape varies
    ("/api/noise-reports",        "",                            None),
    ("/api/notifications",        "",                            None),
    ("/api/complaints",           "",                            None),
]


def check_python_syntax() -> list[str]:
    errors: list[str] = []
    for name in sorted(os.listdir(NOISE_DIR)):
        if not name.endswith(".py"):
            continue
        if name == os.path.basename(__file__):
            continue
        path = os.path.join(NOISE_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                ast.parse(f.read(), filename=name)
        except SyntaxError as e:
            errors.append(f"{name}: {e}")
    return errors


def fetch(url: str, timeout: float = 8.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "noise-health/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def check_endpoints(base: str, tail: str) -> list[str]:
    errors: list[str] = []
    for path, query, expected_keys in GET_ENDPOINTS:
        q = (query or "").format(tail=tail)
        url = f"{base}{path}" + (f"?{q}" if q else "")
        t0 = time.time()
        try:
            status, body = fetch(url)
        except urllib.error.HTTPError as e:
            errors.append(f"{url} -> HTTP {e.code}")
            continue
        except Exception as e:
            errors.append(f"{url} -> {type(e).__name__}: {e}")
            continue
        dt = (time.time() - t0) * 1000
        if status != 200:
            errors.append(f"{url} -> HTTP {status}")
            continue
        if expected_keys is not None:
            try:
                data = json.loads(body)
            except Exception as e:
                errors.append(f"{url} -> invalid JSON: {e}")
                continue
            missing = [k for k in expected_keys if k not in data]
            if missing:
                errors.append(f"{url} -> JSON missing keys {missing}")
                continue
        print(f"  OK   {status}  {dt:6.0f}ms  {path}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:5174")
    ap.add_argument("--tail", default="N12JA",
                    help="tail number used to probe /api/offenses*")
    args = ap.parse_args()

    print("=== code health (AST parse) ===")
    code_errors = check_python_syntax()
    if code_errors:
        for e in code_errors:
            print(f"  FAIL {e}")
    else:
        print("  OK   all .py files parse")

    print(f"\n=== server health ({args.base}) ===")
    server_errors = check_endpoints(args.base, args.tail)

    print("\n=== summary ===")
    if not code_errors and not server_errors:
        print("  ALL OK")
        return 0
    for e in code_errors + server_errors:
        print(f"  FAIL {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
