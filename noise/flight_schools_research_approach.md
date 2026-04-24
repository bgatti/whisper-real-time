# Flight School Fleet Research — Approach & What's Working

**Goal:** Build a complete tail-number → flight-school mapping for trainers operating within 40 nm of KBDU, so noise events can be attributed to specific operators.

**Status:** 46 confirmed tails across 22 schools (up from 26). KBJC coverage substantially improved.

---

## What's working

### 1. FAA Registry name lookup (highest yield)
The FAA aircraft registry supports **registered-owner name search**:
https://registry.faa.gov/aircraftinquiry/Search/NameInquiry

Trainer fleets are typically held by a single LLC ("G&M Aircraft Inc", "Spartan Education LLC", "Aero-Sphere Inc"), so one query returns the entire fleet at once. This is by far the most efficient technique.

**Wins:**
- `G&M Aircraft Inc` → 6 tails for Rocky Mountain Flight School (KBJC)
- `Spartan Education LLC` → 6 tails for Spartan College
- `Aero-Sphere Inc` → confirmed N737JR for Aero-Sphere
- `Journeys Aviation Inc` → N52993

**Key insight:** Don't search the school name — search the *holding LLC*. Grab the LLC from one known tail's registry entry, then re-query by name.

### 2. Direct fleet-page scraping (works for ~30% of schools)
Schools that publish full fleets on plain HTML pages:
- Leading Edge Flight Training (KFNL) — 8 tails
- Vector Air (KEIK) — 5 tails
- Blue Sky Flyers (KGXY) — 4 tails

### 3. Per-aircraft URLs
Wix/Squarespace fleet pages don't render to fetch, but individual aircraft detail pages often do (e.g., Aspen Flying Club, Centennial Flyers).

---

## What's NOT working

| Technique | Problem |
|---|---|
| Wix-hosted fleet pages | JS-rendered, fetch returns no tails (Centennial Flyers, Summit, Songbird) |
| ATP Flight School | National 658-aircraft pool with no per-base listing |
| American Flight Schools aggregated page | Repeatedly times out; per-aircraft LLCs (e.g., "TREDE 2020 C172 LLC") obscure operator |
| Per-aircraft LLC naming | Each tail under its own shell LLC defeats name-based registry lookup |
| SSL-expired sites | Specialty Flight Training, Elite Aviation |
| Sites that simply don't publish fleets | McAir Aviation, Independence Aviation |

---

## Next techniques to try

1. **FAA registry by aircraft type + state** — narrow to CO-registered C172S, then cross-reference owner names
2. **ADS-B Exchange / FlightAware operator history** — observe which tails consistently fly out of KBJC and tie to school via pattern
3. **Wayback Machine snapshots** of fleet pages (especially McAir, Specialty) — sites change over time
4. **Instagram / Facebook scraping** — schools post tail numbers in photo captions
5. **Google image search** for `"school name" N-number`
6. **State business filings** — find the holding LLC behind a known school, then registry-search the LLC

---

## File outputs

- [flight_schools_fleets.json](flight_schools_fleets.json) — current best mapping (46 tails, 22 schools)
- [flight_schools_research_approach.md](flight_schools_research_approach.md) — this document

## Coverage summary by airport

| Airport | Schools | Tails |
|---|---|---|
| KBDU | 3 | 2 |
| KBJC | 5 + unattributed | 11 |
| KAPA | 4 | 3 |
| KFNL | 1 | 8 |
| KLMO | 2 | 2 |
| KEIK | 1 | 5 |
| KCFO | 3 | 7 |
| KGXY | 5 | 4 |

Biggest gaps: KAPA (Independence Aviation Cirrus fleet, Aspen Flying Club's ~40 aircraft) and KBJC (McAir's ~15 aircraft, Specialty Flight Training).
