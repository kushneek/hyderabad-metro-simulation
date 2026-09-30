"""
HMRL Metro Simulation API
Implements the endpoints from architecture document section 15:
  GET  /api/metro/simulation/vehicles
  GET  /api/metro/simulation/vehicles/{trip_id}
  GET  /api/metro/simulation/stations/{stop_id}/departures
  GET  /api/metro/routes/{route_id}
"""
import os
import csv
import math
import statistics
from datetime import datetime, date as date_cls, timedelta
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from zoneinfo import ZoneInfo
from fastapi.middleware.cors import CORSMiddleware
import pymysql
import pymysql.cursors

DB = dict(
    host=os.environ.get("MYSQL_HOST", "localhost"),
    database=os.environ.get("MYSQL_DATABASE", "hmrl_metro"),
    user=os.environ.get("MYSQL_USER", "root"),
    password=os.environ.get("MYSQL_PASSWORD", ""),
    port=int(os.environ.get("MYSQL_PORT", 3306)),
    charset="utf8mb4",
)

# ONE clock for every visitor. The timetable is in the metro's local time, so
# the "current time" must be that zone's time no matter where the visitor or
# the server is. Change with METRO_TIMEZONE if needed.
METRO_TZ_NAME = os.environ.get("METRO_TIMEZONE", "Asia/Kolkata")
METRO_TZ = ZoneInfo(METRO_TZ_NAME)


def now_local() -> datetime:
    return datetime.now(METRO_TZ)


BASE_DIR = "C:/Users/ASUS/Downloads/hmrl-metro-project (1)/hmrl-metro-project/"

app = FastAPI(title="HMRL Metro Timetable-Based Simulation API")
# The page is served by this same app, so CORS is only needed if you host the
# HTML somewhere else. Set ALLOWED_ORIGINS="https://your-site.com,https://x.com"
_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware, allow_origins=_origins, allow_methods=["GET", "POST"], allow_headers=["*"]
)

STATE = {
    "routes": {},
    "calendar": {},
    "shapes": {},
    "stops": {},
    "trips": {},
    "stop_times": {},
    "stations": {},
    "route_shapes": {},
}


# ---------------- Stop time repair (learned from YOUR dataset) ----------------
# Most rows in stop_times.txt have a real stoppage (15/20/30 s ...), but some
# trips have arrival == departure at every stop, so those trains could never
# stop. For those rows only, the stoppage is copied from the SAME platform
# (stop_id) in the other trips of the dataset (median of the rows that DO have
# a stoppage). No number is invented. Set LEARN_DWELL=0 to switch this off.
LEARN_DWELL = os.environ.get("LEARN_DWELL", "1") == "1"
MIN_RUN_SECONDS = 10   # a segment is never squeezed below this


def _kind(i, n):
    return "first" if i == 0 else "last" if i == n - 1 else "mid"


def learn_dwell(stop_times):
    samples = {}
    for rows in stop_times.values():
        n = len(rows)
        for i, (seq, sid, arr, dep, dist) in enumerate(rows):
            if dep - arr > 0:
                samples.setdefault((sid, _kind(i, n)), []).append(dep - arr)
    return {k: statistics.median_low(v) for k, v in samples.items()}


def apply_learned_dwell(rows, learned):
    """Returns (new_rows, still_zero, patched). Next-stop arrival is never moved."""
    n, out, patched, still_zero = len(rows), [], 0, 0
    for i, (seq, sid, arr, dep, dist) in enumerate(rows):
        if dep - arr <= 0:
            k = _kind(i, n)
            dw = learned.get((sid, k)) or learned.get((sid, "mid"))
            if dw:
                old = (arr, dep)
                if i == 0:
                    arr = max(0, dep - dw)              # waits on platform, then leaves
                elif i == n - 1:
                    dep = arr + dw                      # stays after arriving
                else:
                    dep = max(arr, min(arr + dw, rows[i + 1][2] - MIN_RUN_SECONDS))
                if (arr, dep) != old:
                    patched += 1
            if dep - arr <= 0:
                still_zero += 1
        out.append((seq, sid, arr, dep, dist))
    return out, still_zero, patched


# ---------------- Data source: GTFS files (no database) OR MySQL ----------------
# If the folder ./gtfs (or $GTFS_DIR) contains routes.txt, calendar.txt,
# shapes.txt, stops.txt, trips.txt and stop_times.txt, they are loaded
# directly and MySQL is NOT needed (ideal for Render). Otherwise MySQL is used.
GTFS_DIR = os.environ.get("GTFS_DIR", os.path.join(BASE_DIR, "gtfs_data"))
GTFS_FILES = ("routes.txt", "calendar.txt", "shapes.txt", "stops.txt", "trips.txt", "stop_times.txt")


def use_gtfs_files() -> bool:
    return all(os.path.isfile(os.path.join(GTFS_DIR, f)) for f in GTFS_FILES)


def _read_csv(name):
    with open(os.path.join(GTFS_DIR, name), encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in rd]


def _hms(v: str) -> int:
    h, m, sec = v.split(":")          # GTFS allows hours >= 24 (after midnight)
    return int(h) * 3600 + int(m) * 60 + int(sec)


def _iso_date(v: str) -> str:
    return f"{v[:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 and v.isdigit() else v


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _gtfs_table(table):
    """Returns rows shaped EXACTLY like the SQL queries in load_state()."""
    if table == "metro_routes":
        return [
            (r["route_id"], r.get("route_short_name") or r["route_id"],
             r.get("route_long_name", ""), (r.get("route_color") or "555555").lstrip("#"))
            for r in _read_csv("routes.txt")
        ]

    if table == "metro_calendar":
        return [
            (r["service_id"], int(r["monday"]), int(r["tuesday"]), int(r["wednesday"]),
             int(r["thursday"]), int(r["friday"]), int(r["saturday"]), int(r["sunday"]),
             _iso_date(r["start_date"]), _iso_date(r["end_date"]))
            for r in _read_csv("calendar.txt")
        ]

    if table == "metro_shape_points":
        by_shape = {}
        for r in _read_csv("shapes.txt"):
            by_shape.setdefault(r["shape_id"], []).append(r)
        out = []
        for sid in sorted(by_shape):
            pts = sorted(by_shape[sid], key=lambda r: int(r["shape_pt_sequence"]))
            total, prev = 0.0, None
            for r in pts:
                lat, lon = float(r["shape_pt_lat"]), float(r["shape_pt_lon"])
                if r.get("shape_dist_traveled"):
                    total = float(r["shape_dist_traveled"])
                elif prev:
                    total += _haversine_m(prev[0], prev[1], lat, lon)   # metres
                prev = (lat, lon)
                out.append((sid, int(r["shape_pt_sequence"]), total, lon, lat))
        return out

    if table == "metro_stops":
        return [
            (r["stop_id"], r.get("stop_name", ""), float(r["stop_lon"]), float(r["stop_lat"]),
             int(r["location_type"] or 0))
            for r in _read_csv("stops.txt")
        ]

    if table == "metro_trips":
        return [
            (r["trip_id"], r["route_id"], r["service_id"], int(r.get("direction_id") or 0),
             r.get("shape_id", ""), r.get("trip_headsign", ""))
            for r in _read_csv("trips.txt")
        ]

    if table == "metro_stop_times":
        rows = []
        for r in _read_csv("stop_times.txt"):
            dep = r["departure_time"] or r["arrival_time"]
            arr = r["arrival_time"] or dep
            if not r.get("shape_dist_traveled"):
                raise ValueError(
                    f"stop_times.txt: shape_dist_traveled is empty (trip {r['trip_id']}, "
                    f"stop {r['stop_id']}). It is required to place trains on the track."
                )
            rows.append((r["trip_id"], int(r["stop_sequence"]), r["stop_id"],
                         _hms(arr), _hms(dep), float(r["shape_dist_traveled"])))
        rows.sort(key=lambda x: (x[0], x[1]))
        return rows

    raise ValueError(f"unknown table {table}")


class _GtfsCursor:
    _TABLES = ("metro_routes", "metro_calendar", "metro_shape_points",
               "metro_stops", "metro_trips", "metro_stop_times")

    def execute(self, sql):
        for t in self._TABLES:
            if f"FROM {t}" in " ".join(sql.split()):
                self._rows = _gtfs_table(t)
                return
        raise ValueError("unsupported query for GTFS source")

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _GtfsConn:
    def cursor(self):
        return _GtfsCursor()

    def close(self):
        pass


def open_source():
    if use_gtfs_files():
        print("Loading timetable from GTFS files in", GTFS_DIR)
        return _GtfsConn()
    print("GTFS files not found -> loading timetable from MySQL")
    try:
        return pymysql.connect(**DB)
    except Exception as e:
        missing = [f for f in GTFS_FILES if not os.path.isfile(os.path.join(GTFS_DIR, f))]
        raise RuntimeError(
            f"No data source available. Missing GTFS files in '{GTFS_DIR}': {missing}. "
            f"MySQL connection also failed: {e}"
        ) from e


def load_state():
    conn = open_source()
    cur = conn.cursor()

    cur.execute("SELECT route_id, route_short_name, route_long_name, route_color FROM metro_routes")
    STATE["routes"] = {
        r[0]: {
            "short_name": r[1],
            "long_name": r[2],
            "color": "#" + r[3],
        }
        for r in cur.fetchall()
    }

    cur.execute("""
        SELECT service_id, monday, tuesday, wednesday, thursday, friday,
               saturday, sunday, start_date, end_date
        FROM metro_calendar
    """)
    STATE["calendar"] = {
        r[0]: {
            "mon": bool(r[1]),
            "tue": bool(r[2]),
            "wed": bool(r[3]),
            "thu": bool(r[4]),
            "fri": bool(r[5]),
            "sat": bool(r[6]),
            "sun": bool(r[7]),
            "start_date": str(r[8]),
            "end_date": str(r[9]),
        }
        for r in cur.fetchall()
    }

    cur.execute("""
        SELECT shape_id, shape_pt_sequence, shape_dist_traveled, lon, lat
        FROM metro_shape_points
        ORDER BY shape_id, shape_pt_sequence
    """)
    shapes = {}
    for shape_id, seq, dist, lon, lat in cur.fetchall():
        shapes.setdefault(shape_id, []).append(
            (float(lon), float(lat), float(dist))
        )
    STATE["shapes"] = shapes

    cur.execute("""
        SELECT stop_id, stop_name, lon, lat, location_type
        FROM metro_stops
    """)
    STATE["stops"] = {
        r[0]: {
            "name": r[1],
            "lon": float(r[2]),
            "lat": float(r[3]),
            "type": r[4],
        }
        for r in cur.fetchall()
    }

    cur.execute("""
        SELECT trip_id, route_id, service_id, direction_id, shape_id, trip_headsign
        FROM metro_trips
    """)
    STATE["trips"] = {
        r[0]: {
            "route": r[1],
            "service": r[2],
            "dir": r[3],
            "shape": r[4],
            "headsign": r[5],
        }
        for r in cur.fetchall()
    }

    cur.execute("""
        SELECT trip_id, stop_sequence, stop_id, arrival_time,
               departure_time, shape_dist_traveled
        FROM metro_stop_times
        ORDER BY trip_id, stop_sequence
    """)
    stop_times = {}
    for tid, seq, stop_id, arr, dep, dist in cur.fetchall():
        stop_times.setdefault(tid, []).append(
            (seq, stop_id, arr, dep, float(dist) if dist is not None else None)
        )

    # Report of what the DATABASE really contains (before any repair).
    per_route = {}
    zero_trips = 0
    for tid, rows in stop_times.items():
        r = STATE["trips"][tid]["route"]
        d = per_route.setdefault(r, {"rows": 0, "zero_dwell_rows": 0})
        z = sum(1 for x in rows if x[3] - x[2] <= 0)
        d["rows"] += len(rows)
        d["zero_dwell_rows"] += z
        if z >= max(1, len(rows) - 2):
            zero_trips += 1

    patched_total = still_zero_total = 0
    if LEARN_DWELL:
        learned = learn_dwell(stop_times)
        for tid in stop_times:
            stop_times[tid], sz, pt = apply_learned_dwell(stop_times[tid], learned)
            patched_total += pt
            still_zero_total += sz

    STATE["dwell_report"] = {
        "per_route_in_database": per_route,
        "trips_with_no_stoppage_at_all": zero_trips,
        "learn_dwell_enabled": LEARN_DWELL,
        "rows_repaired_from_same_platform_median": patched_total,
        "rows_still_zero_after_repair": still_zero_total,
    }
    print("Dwell report:", STATE["dwell_report"])

    STATE["stop_times"] = stop_times

    # ---------------- Station-level data ----------------
    station_groups = {}

    for trip_id, rows in STATE["stop_times"].items():
        route_id = STATE["trips"][trip_id]["route"]

        for _, stop_id, _, _, _ in rows:
            stop = STATE["stops"].get(stop_id)
            if not stop:
                continue

            name = (stop["name"] or "").strip()
            if not name:
                continue

            key = name.casefold()

            group = station_groups.setdefault(
                key,
                {
                    "station_name": name,
                    "stop_ids": set(),
                    "routes": set(),
                    "lons": [],
                    "lats": [],
                },
            )

            group["stop_ids"].add(stop_id)
            group["routes"].add(route_id)
            group["lons"].append(stop["lon"])
            group["lats"].append(stop["lat"])

    STATE["stations"] = {
        key: {
            "station_id": key,
            "name": group["station_name"],
            "stop_ids": sorted(group["stop_ids"]),
            "routes": sorted(group["routes"]),
            "lon": sum(group["lons"]) / len(group["lons"]),
            "lat": sum(group["lats"]) / len(group["lats"]),
            "intersection": len(group["routes"]) > 1,
        }
        for key, group in station_groups.items()
    }

    # ---------------- Route -> shapes ----------------
    route_shapes = {}

    for trip in STATE["trips"].values():
        route_shapes.setdefault(trip["route"], set()).add(trip["shape"])

    STATE["route_shapes"] = {
        route_id: sorted(shape_ids)
        for route_id, shape_ids in route_shapes.items()
    }

    cur.close()
    conn.close()

    print(
        f"Loaded: {len(STATE['trips'])} trips, "
        f"{len(STATE['stops'])} stops, "
        f"{len(STATE['shapes'])} shapes, "
        f"{len(STATE['stations'])} stations"
    )


@app.on_event("startup")
def startup():
    load_state()


@app.post("/api/metro/admin/reload")
def reload_state(x_admin_token: Optional[str] = Header(None)):
    # Disabled unless ADMIN_TOKEN is set; anyone on the internet can call this.
    token = os.environ.get("ADMIN_TOKEN")
    if not token or x_admin_token != token:
        raise HTTPException(403, "Forbidden")
    load_state()
    return {"status": "reloaded", "trips": len(STATE["trips"])}


# ---------------- Core simulation math ----------------
def weekday_key(d: date_cls) -> str:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][d.weekday()]


def _to_date(v):
    return date_cls.fromisoformat(v) if isinstance(v, str) else v


def dataset_window():
    """(earliest start_date, latest end_date) across all calendar rows."""
    cals = list(STATE["calendar"].values())
    if not cals:
        return None
    return (
        min(_to_date(c["start_date"]) for c in cals),
        max(_to_date(c["end_date"]) for c in cals),
    )


def map_to_dataset_date(d: date_cls) -> date_cls:
    """Reuse the timetable for dates OUTSIDE the dataset's validity window.

    The dataset covers e.g. September 2026. For 13 Oct 2026 (a Tuesday) we
    move back whole weeks until we land inside the window, so the SAME
    weekday's timetable is used. Weekday is preserved, so a Sunday stays a
    Sunday. Dates already inside the window are returned unchanged.
    """
    win = dataset_window()
    if not win:
        return d
    lo, hi = win
    if lo <= d <= hi:
        return d
    if d > hi:
        d2 = d - timedelta(days=7 * math.ceil((d - hi).days / 7))
    else:
        d2 = d + timedelta(days=7 * math.ceil((lo - d).days / 7))
    return d2 if lo <= d2 <= hi else d


def active_services(d: date_cls):
    d = map_to_dataset_date(d)
    wk = weekday_key(d)
    active = set()

    for sid, cal in STATE["calendar"].items():
        if _to_date(cal["start_date"]) <= d <= _to_date(cal["end_date"]) and cal[wk]:
            active.add(sid)

    # Window shorter than a week (some weekday missing): fall back to the
    # weekday pattern alone so the simulation never goes blank.
    if not active:
        active = {sid for sid, cal in STATE["calendar"].items() if cal[wk]}

    return active


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

    # Before the train starts
    if t < first[2]:
        return {"state": "NOT_STARTED"}

    # Check every stop except the final terminal
    for i in range(len(st) - 1):

        _, stop_cur, arr_cur, dep_cur, dist_cur = st[i]
        _, stop_next, arr_next, dep_next, dist_next = st[i + 1]

        current_station = STATE["stops"].get(stop_cur)
        next_station = STATE["stops"].get(stop_next)

        if not current_station or not next_station:
            continue

        # ---------------------------------------------------------
        # 1. TRAIN IS STOPPED AT CURRENT STATION
        #
        # [arrival_time, departure_time)
        #
        # Example:
        # arrival  = 04:40:44
        # departure = 04:40:59
        #
        # 04:40:44 -> 04:40:58 = exactly at station
        # 04:40:59 -> starts moving
        # ---------------------------------------------------------
        if arr_cur <= t < dep_cur:
            return {
                "state": "AT_ORIGIN" if i == 0 else "DWELLING",
                "lon": current_station["lon"],
                "lat": current_station["lat"],
                "bearing": None,
                "current_stop": stop_cur,
                "current_stop_name": current_station["name"],
                "next_stop": stop_next,
                "next_stop_name": next_station["name"],
                "scheduled_eta_next": arr_next,
                "scheduled_arrival_current": arr_cur,
                "scheduled_departure_current": dep_cur,
            }

        # ---------------------------------------------------------
        # 2. TRAIN IS TRAVELLING TO NEXT STATION
        #
        # [departure_time, next_arrival_time)
        #
        # The train leaves the current station exactly at
        # departure_time and reaches the next station exactly
        # at next_arrival_time.
        # ---------------------------------------------------------
        if dep_cur <= t < arr_next:

            trip = STATE["trips"][trip_id]
            shape_pts = STATE["shapes"][trip["shape"]]

            travel_duration = arr_next - dep_cur

            if travel_duration <= 0:
                continue

            # Progress through the journey between the two stations.
            frac = (t - dep_cur) / travel_duration

            # Keep progress safely inside [0, 1].
            frac = max(0.0, min(1.0, frac))

            # Move along the GTFS shape using shape_dist_traveled.
            target_dist = (
                dist_cur
                + frac * (dist_next - dist_cur)
            )

            lon, lat, bearing = interp_along_shape(
                shape_pts,
                target_dist
            )

            return {
                "state": "IN_TRANSIT",
                "lon": lon,
                "lat": lat,
                "bearing": bearing,
                "current_stop": stop_cur,
                "current_stop_name": current_station["name"],
                "next_stop": stop_next,
                "next_stop_name": next_station["name"],
                "scheduled_eta_next": arr_next,
                "scheduled_arrival_current": arr_cur,
                "scheduled_departure_current": dep_cur,
            }

    # -------------------------------------------------------------
    # Final terminal station
    # -------------------------------------------------------------
    _, last_stop, last_arr, last_dep, _ = last
    s = STATE["stops"][last_stop]

    # Keep the train exactly at the terminal during its
    # scheduled dwell.
    if last_arr <= t < last_dep:
        return {
            "state": "DWELLING",
            "lon": s["lon"],
            "lat": s["lat"],
            "bearing": None,
            "current_stop": last_stop,
            "current_stop_name": s["name"],
            "next_stop": None,
            "next_stop_name": None,
            "scheduled_eta_next": None,
            "scheduled_arrival_current": last_arr,
            "scheduled_departure_current": last_dep,
        }

    # After terminal departure the trip has ended.
    if t >= last_dep:
        return {
            "state": "AT_TERMINAL",
            "lon": s["lon"],
            "lat": s["lat"],
            "bearing": None,
            "current_stop": last_stop,
            "current_stop_name": s["name"],
            "next_stop": None,
            "next_stop_name": None,
            "scheduled_eta_next": None,
            "scheduled_arrival_current": last_arr,
            "scheduled_departure_current": last_dep,
        }

    return None


def parse_date_param(date_str: Optional[str]) -> date_cls:
    if date_str:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    return now_local().date()


def parse_time_param(time_str: Optional[str], for_date: date_cls) -> int:
    if time_str:
        h, m, s = map(int, time_str.split(":"))
        return h * 3600 + m * 60 + s

    now = now_local()
    return now.hour * 3600 + now.minute * 60 + now.second


# ---------------- Endpoints ----------------
@app.get("/api/metro/simulation/vehicles")
def get_all_vehicles(
    date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today (server local time)"),
    time: Optional[str] = Query(None, description="HH:MM:SS, defaults to now (server local time)"),
    route_id: Optional[str] = Query(None, description="Filter to RED/BLUE/GREEN"),
):
    d = parse_date_param(date)
    t = parse_time_param(time, d)
    svcs = active_services(d)

    vehicles = []
    first_start = None   # earliest first-stop time of the day
    last_end = None      # latest final-stop time of the day

    for trip_id, trip in STATE["trips"].items():
        if trip["service"] not in svcs:
            continue

        rows = STATE["stop_times"].get(trip_id)
        if rows:
            first_start = rows[0][2] if first_start is None else min(first_start, rows[0][2])
            last_end = rows[-1][3] if last_end is None else max(last_end, rows[-1][3])

        if route_id and trip["route"] != route_id:
            continue

        st = compute_train_state(trip_id, t)

        if not st or st["state"] in ("NOT_STARTED", "AT_TERMINAL","COMPLETED"):
            continue

        vehicles.append({
            "trip_id": trip_id,
            "route_id": trip["route"],
            "direction_id": trip["dir"],
            "headsign": trip["headsign"],
            "simulation_time": t,
            "source": "GTFS_SCHEDULE_SIMULATION",
            "mode": "simulated",
            **st,
        })

    if first_start is None:
        service_status = "NO_SERVICE_TODAY"
    elif t < first_start:
        service_status = "NOT_STARTED"
    elif t >= last_end:
        service_status = "ENDED"
    else:
        service_status = "RUNNING"

    return {
        "simulation_date": d.isoformat(),
        "timetable_date_used": map_to_dataset_date(d).isoformat(),
        "simulation_time_seconds": t,
        "service_status": service_status,
        "service_first_seconds": first_start,
        "service_last_seconds": last_end,
        "active_service_ids": sorted(svcs),
        "vehicle_count": len(vehicles),
        "disclaimer": (
            "SIMULATED positions calculated from GTFS schedule. "
            "Not live GPS or operational tracking."
        ),
        "vehicles": vehicles,
    }


@app.get("/api/metro/simulation/vehicles/{trip_id}")
def get_vehicle(
    trip_id: str,
    date: str = Query(
        ...,
        description="YYYY-MM-DD -- REQUIRED, since trip_id recurs across dates"
    ),
    time: Optional[str] = Query(None, description="HH:MM:SS, defaults to now (server local time)"),
):
    if trip_id not in STATE["trips"]:
        raise HTTPException(404, f"trip_id '{trip_id}' not found")

    d = parse_date_param(date)
    trip = STATE["trips"][trip_id]

    if trip["service"] not in active_services(d):
        raise HTTPException(
            404,
            f"trip_id '{trip_id}' does not run on {date} "
            f"(service_id={trip['service']})"
        )

    t = parse_time_param(time, d)
    st = compute_train_state(trip_id, t)

    if not st:
        raise HTTPException(404, "No stop_times found for this trip")

    return {
        "trip_id": trip_id,
        "route_id": trip["route"],
        "direction_id": trip["dir"],
        "headsign": trip["headsign"],
        "simulation_date": d.isoformat(),
        "simulation_time_seconds": t,
        "source": "GTFS_SCHEDULE_SIMULATION",
        "mode": "simulated",
        **st,
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
                    "trip_id": trip_id,
                    "route_id": trip["route"],
                    "headsign": trip["headsign"],
                    "scheduled_arrival_seconds": arr,
                    "scheduled_departure_seconds": dep,
                })

    results.sort(
        key=lambda r: r["scheduled_departure_seconds"]
    )

    return {
        "stop_id": stop_id,
        "stop_name": STATE["stops"][stop_id]["name"],
        "simulation_date": d.isoformat(),
        "as_of_seconds": t,
        "departures": results[:limit],
    }


@app.get("/api/metro/map")
def get_map_data():
    routes = []

    for route_id, route in STATE["routes"].items():
        shapes = []

        for shape_id in STATE["route_shapes"].get(route_id, []):
            points = STATE["shapes"].get(shape_id, [])

            if not points:
                continue

            shapes.append({
                "shape_id": shape_id,
                "coordinates": [
                    [lat, lon]
                    for lon, lat, _ in points
                ],
            })

        routes.append({
            "route_id": route_id,
            "short_name": route["short_name"],
            "long_name": route["long_name"],
            "color": route["color"],
            "shapes": shapes,
        })

    return {
        "routes": routes,
        "stations": list(STATE["stations"].values()),
        "station_count": len(STATE["stations"]),
    }


@app.get("/api/metro/routes/{route_id}")
def get_route(route_id: str):
    if route_id not in STATE["routes"]:
        raise HTTPException(404, f"route_id '{route_id}' not found")

    trip_count = sum(
        1 for tr in STATE["trips"].values()
        if tr["route"] == route_id
    )

    return {
        "route_id": route_id,
        **STATE["routes"][route_id],
        "scheduled_trips_total": trip_count,
    }


@app.get("/api/metro/routes")
def list_routes():
    return {
        "routes": [
            {"route_id": rid, **r}
            for rid, r in STATE["routes"].items()
        ]
    }


@app.get("/api/metro/simulation/week")
def get_week_schedule(
    start_date: Optional[str] = Query(
        None,
        description="Monday date in YYYY-MM-DD; defaults to this week's Monday (server local time)"
    )
):
    if start_date is None:
        today = parse_date_param(None)
        start_date = (today - timedelta(days=today.weekday())).isoformat()

    try:
        monday = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="start_date must be in YYYY-MM-DD format"
        )

    if monday.weekday() != 0:
        raise HTTPException(
            status_code=400,
            detail="start_date must be a Monday"
        )

    week = []

    for offset in range(7):
        current_date = monday + timedelta(days=offset)
        services = active_services(current_date)

        trip_count = sum(
            1
            for trip in STATE["trips"].values()
            if trip["service"] in services
        )

        week.append({
            "date": current_date.isoformat(),
            "day": current_date.strftime("%A"),
            "service_ids": sorted(services),
            "scheduled_trip_count": trip_count,
        })

    return {
        "week_start": monday.isoformat(),
        "week_end": (monday + timedelta(days=6)).isoformat(),
        "schedule": week,
    }


@app.get("/api/metro/debug/dwell")
def debug_dwell(trip_id: Optional[str] = None, rows: int = 12):
    """Shows what the timetable REALLY says about stops. Open in a browser."""
    if os.environ.get("DEBUG_ENDPOINTS") != "1":
        raise HTTPException(404, "Not found")
    if trip_id is None:
        trip_id = next(iter(sorted(STATE["stop_times"])), None)
    if trip_id not in STATE["stop_times"]:
        raise HTTPException(404, "trip_id not found")

    def hms(x):
        return f"{x // 3600:02d}:{x % 3600 // 60:02d}:{x % 60:02d}"

    st = STATE["stop_times"][trip_id]
    shape = STATE["shapes"].get(STATE["trips"][trip_id]["shape"], [])
    return {
        "dwell_report_from_database": STATE.get("dwell_report"),
        "trip_id": trip_id,
        "headsign": STATE["trips"][trip_id]["headsign"],
        "stops": [
            {
                "seq": seq,
                "stop": STATE["stops"].get(sid, {}).get("name", sid),
                "arrival": hms(arr),
                "departure": hms(dep),
                "dwell_seconds": dep - arr,
                "shape_dist_traveled": dist,
            }
            for seq, sid, arr, dep, dist in st[:rows]
        ],
        "distance_units_check": {
            "last_stop_dist_in_stop_times": st[-1][4],
            "shape_total_length": shape[-1][2] if shape else None,
            "note": "These two should be similar numbers. If one is ~1000x the other, units differ and trains will be drawn in the wrong place.",
        },
    }


@app.get("/api/metro/time")
def metro_time():
    """Authoritative clock for the website (same for every visitor)."""
    n = now_local()
    return {
        "timezone": METRO_TZ_NAME,
        "date": n.date().isoformat(),
        "seconds": n.hour * 3600 + n.minute * 60 + n.second,
        "epoch_ms": int(n.timestamp() * 1000),
        "utc_offset_seconds": int(n.utcoffset().total_seconds()),
    }


# ---------------- Serve the website from the same app ----------------
# Only these files are public (never app.py or anything else in the folder).
def _site_file(name, media_type=None):
    path = os.path.join(BASE_DIR, name)
    if not os.path.isfile(path):          # API-only deployment: no website files here
        raise HTTPException(404, f"{name} not found on this server")
    return FileResponse(path, media_type=media_type)


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def site_index():
    return _site_file("index.html")


@app.get("/leaflet.js", include_in_schema=False)
def site_leaflet_js():
    return _site_file("leaflet.js", "application/javascript")


@app.get("/leaflet.css", include_in_schema=False)
def site_leaflet_css():
    return _site_file("leaflet.css", "text/css")


if os.path.isdir(os.path.join(BASE_DIR, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(BASE_DIR, "assets")), name="assets")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "trips_loaded": len(STATE["trips"]),
        "dwell_report": STATE.get("dwell_report"),
        "dataset_window": [str(x) for x in (dataset_window() or [])],
    }