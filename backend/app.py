"""
HMRL Metro Simulation API
Implements the endpoints from architecture document section 15:
  GET  /api/metro/simulation/vehicles                  - all currently active simulated trains
  GET  /api/metro/simulation/vehicles/{trip_id}         - one trip's simulated position (requires date, see note below)
  GET  /api/metro/simulation/stations/{stop_id}/departures - upcoming scheduled departures
  GET  /api/metro/routes/{route_id}                     - route/corridor info

NOTE ON trip_id AMBIGUITY (flagged during design review):
  A trip_id recurs on every date its service_id is active, so /vehicles/{trip_id} alone is ambiguous
  once replay mode is in use. This API requires an explicit `date` query param on that endpoint for
  that reason -- there is no "guess the date" fallback.

Section 16 (backend strategy): all trip/stop_time/shape/calendar data is loaded into memory ONCE at
startup, not re-queried from Postgres on every request. Postgres remains the source of truth;
this process is the "central simulation service" the architecture document describes.
"""
import os
import math
from datetime import datetime, date as date_cls, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import pymysql
import pymysql.cursors

DB = dict(
    host=os.environ.get("MYSQL_HOST", "localhost"),
    database=os.environ.get("MYSQL_DATABASE", "hmrl_metro"),
    user=os.environ.get("MYSQL_USER", "metro_user"),
    password=os.environ.get("MYSQL_PASSWORD", "Kushagra@8221"),
    port=int(os.environ.get("MYSQL_PORT", 3306)),
    charset="utf8mb4",
)

IST_OFFSET = timedelta(hours=5, minutes=30)

app = FastAPI(title="HMRL Metro Timetable-Based Simulation API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ---------------- In-memory state, loaded once at startup (section 16) ----------------
STATE = {"routes": {}, "calendar": {}, "shapes": {}, "stops": {}, "trips": {}, "stop_times": {}}


def load_state():
    conn = pymysql.connect(**DB)
    cur = conn.cursor()

    cur.execute("SELECT route_id, route_short_name, route_long_name, route_color FROM metro_routes")
    STATE["routes"] = {r[0]: {"short_name": r[1], "long_name": r[2], "color": "#" + r[3]} for r in cur.fetchall()}

    cur.execute("SELECT service_id, monday, tuesday, wednesday, thursday, friday, saturday, sunday FROM metro_calendar")
    STATE["calendar"] = {
        r[0]: {"mon": bool(r[1]), "tue": bool(r[2]), "wed": bool(r[3]), "thu": bool(r[4]),
               "fri": bool(r[5]), "sat": bool(r[6]), "sun": bool(r[7])}
        for r in cur.fetchall()
    }

    cur.execute("""
        SELECT shape_id, shape_pt_sequence, shape_dist_traveled, lon, lat
        FROM metro_shape_points ORDER BY shape_id, shape_pt_sequence
    """)
    shapes = {}
    for shape_id, seq, dist, lon, lat in cur.fetchall():
        shapes.setdefault(shape_id, []).append((float(lon), float(lat), float(dist)))
    STATE["shapes"] = shapes

    cur.execute("SELECT stop_id, stop_name, lon, lat, location_type FROM metro_stops")
    STATE["stops"] = {r[0]: {"name": r[1], "lon": float(r[2]), "lat": float(r[3]), "type": r[4]} for r in cur.fetchall()}

    cur.execute("SELECT trip_id, route_id, service_id, direction_id, shape_id, trip_headsign FROM metro_trips")
    STATE["trips"] = {
        r[0]: {"route": r[1], "service": r[2], "dir": r[3], "shape": r[4], "headsign": r[5]}
        for r in cur.fetchall()
    }

    cur.execute("""
        SELECT trip_id, stop_sequence, stop_id, arrival_time, departure_time, shape_dist_traveled
        FROM metro_stop_times ORDER BY trip_id, stop_sequence
    """)
    stop_times = {}
    for tid, seq, stop_id, arr, dep, dist in cur.fetchall():
        stop_times.setdefault(tid, []).append((seq, stop_id, arr, dep, float(dist) if dist is not None else None))
    STATE["stop_times"] = stop_times

    cur.close()
    conn.close()
    print(f"Loaded: {len(STATE['trips'])} trips, {len(STATE['stops'])} stops, {len(STATE['shapes'])} shapes")




@app.on_event("startup")
def startup():
    load_state()


@app.post("/api/metro/admin/reload")
def reload_state():
    """Re-read the database into memory. Call this after running load_gtfs.py again on a new feed."""
    load_state()
    return {"status": "reloaded", "trips": len(STATE["trips"])}


# ---------------- Core simulation math (same logic validated in the JS prototype) ----------------
def weekday_key(d: date_cls) -> str:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][d.weekday()]


def active_services(d: date_cls):
    wk = weekday_key(d)
    return {sid for sid, cal in STATE["calendar"].items() if cal[wk]}


def interp_along_shape(shape_pts, target_dist):
    if target_dist <= shape_pts[0][2]:
        return shape_pts[0][0], shape_pts[0][1], None
    last = shape_pts[-1]
    if target_dist >= last[2]:
        return last[0], last[1], None
    lo, hi = 0, len(shape_pts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if shape_pts[mid][2] <= target_dist:
            lo = mid
        else:
            hi = mid
    a, b = shape_pts[lo], shape_pts[hi]
    span = b[2] - a[2]
    f = (target_dist - a[2]) / span if span > 0 else 0
    lon = a[0] + (b[0] - a[0]) * f
    lat = a[1] + (b[1] - a[1]) * f
    bearing = math.degrees(math.atan2(b[0] - a[0], b[1] - a[1]))
    return lon, lat, bearing


def compute_train_state(trip_id: str, t: int):
    st = STATE["stop_times"].get(trip_id)
    if not st or len(st) < 2:
        return None
    first, last = st[0], st[-1]
    if t < first[2]:
        return {"state": "NOT_STARTED"}
    if t > last[3]:
        return {"state": "COMPLETED"}

    for i in range(len(st) - 1):
        _, stop_cur, arr_cur, dep_cur, dist_cur = st[i]
        _, stop_next, arr_next, dep_next, dist_next = st[i + 1]

        if arr_cur <= t <= dep_cur:
            s = STATE["stops"][stop_cur]
            return {
                "state": "AT_ORIGIN" if i == 0 else "DWELLING",
                "lon": s["lon"], "lat": s["lat"], "bearing": None,
                "current_stop": stop_cur, "current_stop_name": s["name"],
                "next_stop": stop_next, "next_stop_name": STATE["stops"][stop_next]["name"],
                "scheduled_eta_next": arr_next,
            }
        if dep_cur < t < arr_next:
            trip = STATE["trips"][trip_id]
            shape_pts = STATE["shapes"][trip["shape"]]
            frac = (t - dep_cur) / (arr_next - dep_cur)
            target_dist = dist_cur + frac * (dist_next - dist_cur)
            lon, lat, bearing = interp_along_shape(shape_pts, target_dist)
            return {
                "state": "IN_TRANSIT",
                "lon": lon, "lat": lat, "bearing": bearing,
                "current_stop": stop_cur, "current_stop_name": STATE["stops"][stop_cur]["name"],
                "next_stop": stop_next, "next_stop_name": STATE["stops"][stop_next]["name"],
                "scheduled_eta_next": arr_next,
            }
    s = STATE["stops"][last[1]]
    return {
        "state": "AT_TERMINAL", "lon": s["lon"], "lat": s["lat"], "bearing": None,
        "current_stop": last[1], "current_stop_name": s["name"],
        "next_stop": None, "next_stop_name": None, "scheduled_eta_next": None,
    }


def parse_date_param(date_str: Optional[str]) -> date_cls:
    if date_str:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    return (datetime.utcnow() + IST_OFFSET).date()


def parse_time_param(time_str: Optional[str], for_date: date_cls) -> int:
    if time_str:
        h, m, s = map(int, time_str.split(":"))
        return h * 3600 + m * 60 + s
    now_ist = datetime.utcnow() + IST_OFFSET
    return now_ist.hour * 3600 + now_ist.minute * 60 + now_ist.second


# ---------------- Endpoints ----------------

@app.get("/api/metro/simulation/vehicles")
def get_all_vehicles(
    date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today (IST)"),
    time: Optional[str] = Query(None, description="HH:MM:SS, defaults to now (IST)"),
    route_id: Optional[str] = Query(None, description="Filter to RED/BLUE/GREEN"),
):
    d = parse_date_param(date)
    t = parse_time_param(time, d)
    svcs = active_services(d)

    vehicles = []
    for trip_id, trip in STATE["trips"].items():
        if trip["service"] not in svcs:
            continue
        if route_id and trip["route"] != route_id:
            continue
        st = compute_train_state(trip_id, t)
        if not st or st["state"] in ("NOT_STARTED", "COMPLETED"):
            continue
        vehicles.append({
            "trip_id": trip_id, "route_id": trip["route"], "direction_id": trip["dir"],
            "headsign": trip["headsign"], "simulation_time": t, "source": "GTFS_SCHEDULE_SIMULATION",
            "mode": "simulated", **st,
        })

    return {
        "simulation_date": d.isoformat(), "simulation_time_seconds": t,
        "active_service_ids": sorted(svcs), "vehicle_count": len(vehicles),
        "disclaimer": "SIMULATED positions calculated from GTFS schedule. Not live GPS or operational tracking.",
        "vehicles": vehicles,
    }


@app.get("/api/metro/simulation/vehicles/{trip_id}")
def get_vehicle(
    trip_id: str,
    date: str = Query(..., description="YYYY-MM-DD -- REQUIRED, since trip_id recurs across dates"),
    time: Optional[str] = Query(None, description="HH:MM:SS, defaults to now (IST)"),
):
    if trip_id not in STATE["trips"]:
        raise HTTPException(404, f"trip_id '{trip_id}' not found")
    d = parse_date_param(date)
    trip = STATE["trips"][trip_id]
    if trip["service"] not in active_services(d):
        raise HTTPException(404, f"trip_id '{trip_id}' does not run on {date} (service_id={trip['service']})")
    t = parse_time_param(time, d)
    st = compute_train_state(trip_id, t)
    if not st:
        raise HTTPException(404, "No stop_times found for this trip")
    return {
        "trip_id": trip_id, "route_id": trip["route"], "direction_id": trip["dir"],
        "headsign": trip["headsign"], "simulation_date": d.isoformat(), "simulation_time_seconds": t,
        "source": "GTFS_SCHEDULE_SIMULATION", "mode": "simulated", **st,
    }


@app.get("/api/metro/simulation/stations/{stop_id}/departures")
def get_departures(
    stop_id: str,
    date: Optional[str] = Query(None),
    time: Optional[str] = Query(None),
    limit: int = Query(10, le=50),
):
    if stop_id not in STATE["stops"]:
        raise HTTPException(404, f"stop_id '{stop_id}' not found")
    d = parse_date_param(date)
    t = parse_time_param(time, d)
    svcs = active_services(d)

    results = []
    for trip_id, rows in STATE["stop_times"].items():
        trip = STATE["trips"][trip_id]
        if trip["service"] not in svcs:
            continue
        for seq, sid, arr, dep, dist in rows:
            if sid == stop_id and dep >= t:
                results.append({
                    "trip_id": trip_id, "route_id": trip["route"], "headsign": trip["headsign"],
                    "scheduled_departure_seconds": dep,
                })
    results.sort(key=lambda r: r["scheduled_departure_seconds"])
    return {
        "stop_id": stop_id, "stop_name": STATE["stops"][stop_id]["name"],
        "simulation_date": d.isoformat(), "as_of_seconds": t,
        "departures": results[:limit],
    }


@app.get("/api/metro/routes/{route_id}")
def get_route(route_id: str):
    if route_id not in STATE["routes"]:
        raise HTTPException(404, f"route_id '{route_id}' not found")
    trip_count = sum(1 for tr in STATE["trips"].values() if tr["route"] == route_id)
    return {"route_id": route_id, **STATE["routes"][route_id], "scheduled_trips_total": trip_count}


@app.get("/api/metro/routes")
def list_routes():
    return {"routes": [{"route_id": rid, **r} for rid, r in STATE["routes"].items()]}


@app.get("/health")
def health():
    return {"status": "ok", "trips_loaded": len(STATE["trips"])}
