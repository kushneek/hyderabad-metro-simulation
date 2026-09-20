"""
HMRL GTFS Ingestion Pipeline (MySQL version)
Same logic as the PostgreSQL version:
  1. Time conversion to seconds-since-midnight (handles GTFS >24:00:00 safely)
  2. Shape geometry stored as ordered points with cumulative distance (plain columns, see schema note)
  3. Ameerpet platform correction (derived from clean July 2026 data)
  4. Referential integrity validation gates (must pass before data is committed)

Run: python3 load_gtfs.py <path_to_gtfs_folder>
"""
import csv
import sys
import os
import math
from collections import defaultdict, Counter
import pymysql

DB = dict(
    host=os.environ["MYSQL_HOST"],
    database=os.environ.get("MYSQL_DATABASE", "hmrl_metro"),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    port=int(os.environ.get("MYSQL_PORT", 3306)),
    charset="utf8mb4",
    ssl={"ca": os.environ["MYSQL_SSL_CA"]},
)

AMEERPET_PLATFORM_FIX = {
    ('RED', '0'): 'AME1',
    ('RED', '1'): 'AME2',
    ('BLUE', '0'): 'AME3',
    ('BLUE', '1'): 'AME4',
}


def time_to_seconds(t):
    h, m, s = map(int, t.split(':'))
    return h * 3600 + m * 60 + s


def load_csv(folder, fname):
    with open(f"{folder}/{fname}") as f:
        return list(csv.DictReader(f))


def haversine_m(lon1, lat1, lon2, lat2):
    """Great-circle distance in meters -- used to compute shape length without relying on
    MySQL's spatial functions (see schema note on axis-order risk)."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def validate(folder):
    print("=== Running validation checks ===")
    trips = load_csv(folder, 'trips.txt')
    stop_times = load_csv(folder, 'stop_times.txt')
    stops = load_csv(folder, 'stops.txt')
    shapes = load_csv(folder, 'shapes.txt')

    trip_ids = {t['trip_id'] for t in trips}
    shape_ids_trips = {t['shape_id'] for t in trips}
    shape_ids_shapes = {s['shape_id'] for s in shapes}
    stop_ids = {s['stop_id'] for s in stops}

    st_trip_ids = {s['trip_id'] for s in stop_times}
    st_stop_ids = {s['stop_id'] for s in stop_times}

    errors = []
    orphan_trips = st_trip_ids - trip_ids
    if orphan_trips:
        errors.append(f"{len(orphan_trips)} trip_ids in stop_times.txt missing from trips.txt")
    orphan_stops = st_stop_ids - stop_ids
    if orphan_stops:
        errors.append(f"{len(orphan_stops)} stop_ids in stop_times.txt missing from stops.txt")
    orphan_shapes = shape_ids_trips - shape_ids_shapes
    if orphan_shapes:
        errors.append(f"{len(orphan_shapes)} shape_ids in trips.txt missing from shapes.txt")

    seen = Counter((s['trip_id'], s['stop_sequence']) for s in stop_times)
    dupes = [k for k, v in seen.items() if v > 1]
    if dupes:
        errors.append(f"{len(dupes)} duplicate (trip_id, stop_sequence) pairs")

    by_trip = defaultdict(list)
    for s in stop_times:
        by_trip[s['trip_id']].append(int(s['stop_sequence']))
    bad_seq = 0
    for tid, seq in by_trip.items():
        seq_sorted = sorted(seq)
        if seq_sorted != list(range(seq_sorted[0], seq_sorted[0] + len(seq_sorted))):
            bad_seq += 1
    if bad_seq:
        errors.append(f"{bad_seq} trips with non-contiguous stop_sequence")

    by_shape = defaultdict(list)
    for s in shapes:
        by_shape[s['shape_id']].append(float(s['shape_dist_traveled']))
    non_monotonic = [sid for sid, d in by_shape.items() if d != sorted(d)]
    if non_monotonic:
        errors.append(f"Non-monotonic shape_dist_traveled in shapes: {non_monotonic}")

    parent_of = {s['stop_id']: s['parent_station'] for s in stops}
    platform_routes = defaultdict(Counter)
    trip_route = {t['trip_id']: t['route_id'] for t in trips}
    for row in stop_times:
        r = trip_route.get(row['trip_id'])
        if r:
            platform_routes[row['stop_id']][r] += 1

    inconsistent_platforms = []
    for stop_id, counter in platform_routes.items():
        if len(counter) > 1:
            total = sum(counter.values())
            top = counter.most_common(1)[0][1]
            if (total - top) / total > 0.05:
                inconsistent_platforms.append((stop_id, dict(counter)))

    if inconsistent_platforms:
        print(f"  WARNING (non-fatal, auto-correctable): {len(inconsistent_platforms)} platform(s) show mixed route usage:")
        for p in inconsistent_platforms:
            print(f"    {p}")
    else:
        print("  Interchange platform consistency: OK")

    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    print("  All hard integrity checks passed.\n")


def apply_ameerpet_fix(stop_times, trips_by_id):
    fixed = 0
    for row in stop_times:
        if row['stop_id'].startswith('AME') and row['stop_id'] != 'AME':
            t = trips_by_id[row['trip_id']]
            key = (t['route_id'], t['direction_id'])
            correct = AMEERPET_PLATFORM_FIX.get(key)
            if correct and row['stop_id'] != correct:
                row['stop_id'] = correct
                fixed += 1
    print(f"Ameerpet platform correction applied: {fixed} rows fixed.\n")
    return stop_times


def main(folder):
    validate(folder)

    agency = load_csv(folder, 'agency.txt')
    stops = load_csv(folder, 'stops.txt')
    routes = load_csv(folder, 'routes.txt')
    calendar = load_csv(folder, 'calendar.txt')
    shapes = load_csv(folder, 'shapes.txt')
    trips = load_csv(folder, 'trips.txt')
    stop_times = load_csv(folder, 'stop_times.txt')
    fare_attrs = load_csv(folder, 'fare_attributes.txt')
    fare_rules = load_csv(folder, 'fare_rules.txt')
    feed_info = load_csv(folder, 'feed_info.txt')

    trips_by_id = {t['trip_id']: t for t in trips}
    stop_times = apply_ameerpet_fix(stop_times, trips_by_id)

    conn = pymysql.connect(**DB)
    cur = conn.cursor()

    print("Loading into database...")

    fi = feed_info[0]
    cur.execute(
        "INSERT INTO gtfs_feed_versions (feed_publisher_name, feed_start_date, feed_end_date, source_filename) "
        "VALUES (%s, STR_TO_DATE(%s,'%%Y%%m%%d'), STR_TO_DATE(%s,'%%Y%%m%%d'), %s)",
        (fi['feed_publisher_name'], fi['feed_start_date'], fi['feed_end_date'], folder)
    )

    for a in agency:
        cur.execute(
            "INSERT INTO metro_agency VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (a['agency_id'], a['agency_name'], a['agency_url'], a['agency_timezone'],
             a['agency_lang'], a.get('agency_fare_url'), a.get('agency_email'), a.get('agency_phone'))
        )

    for r in routes:
        cur.execute(
            "INSERT INTO metro_routes VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (r['route_id'], r['agency_id'], r['route_short_name'], r['route_long_name'],
             int(r['route_type']), r['route_color'], r['route_text_color'], int(r['route_sort_order']))
        )

    for c in calendar:
        cur.execute(
            "INSERT INTO metro_calendar VALUES (%s,%s,%s,%s,%s,%s,%s,%s,STR_TO_DATE(%s,'%%Y%%m%%d'),STR_TO_DATE(%s,'%%Y%%m%%d'))",
            (c['service_id'], int(c['monday']), int(c['tuesday']), int(c['wednesday']),
             int(c['thursday']), int(c['friday']), int(c['saturday']), int(c['sunday']),
             c['start_date'], c['end_date'])
        )

    # shapes: compute total length via haversine over the ordered points (no MySQL spatial functions needed)
    by_shape = defaultdict(list)
    for s in shapes:
        by_shape[s['shape_id']].append(s)
    for shape_id, pts in by_shape.items():
        pts.sort(key=lambda p: int(p['shape_pt_sequence']))
        total = 0.0
        for i in range(len(pts) - 1):
            total += haversine_m(float(pts[i]['shape_pt_lon']), float(pts[i]['shape_pt_lat']),
                                  float(pts[i+1]['shape_pt_lon']), float(pts[i+1]['shape_pt_lat']))
        cur.execute("INSERT INTO metro_shapes (shape_id, total_length_m) VALUES (%s, %s)", (shape_id, total))

    shape_point_rows = [
        (s['shape_id'], int(s['shape_pt_sequence']), float(s['shape_dist_traveled']),
         float(s['shape_pt_lon']), float(s['shape_pt_lat']))
        for s in shapes
    ]
    cur.executemany(
        "INSERT INTO metro_shape_points (shape_id, shape_pt_sequence, shape_dist_traveled, lon, lat) VALUES (%s,%s,%s,%s,%s)",
        shape_point_rows
    )

    stop_rows = [
        (s['stop_id'], s['stop_name'], s.get('zone_id') or None,
         int(s['location_type']) if s['location_type'] else 0,
         s.get('parent_station') or None, s.get('platform_code') or None,
         float(s['stop_lon']), float(s['stop_lat']))
        for s in stops
    ]
    cur.executemany(
        "INSERT INTO metro_stops (stop_id, stop_name, zone_id, location_type, parent_station, platform_code, lon, lat) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        stop_rows
    )

    trip_rows = [(t['trip_id'], t['route_id'], t['service_id'], int(t['direction_id']),
                  t['trip_headsign'], t['block_id'], t['shape_id']) for t in trips]
    cur.executemany(
        "INSERT INTO metro_trips (trip_id, route_id, service_id, direction_id, trip_headsign, block_id, shape_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        trip_rows
    )

    st_rows = [
        (s['trip_id'], int(s['stop_sequence']), s['stop_id'],
         time_to_seconds(s['arrival_time']), time_to_seconds(s['departure_time']),
         float(s['shape_dist_traveled']) if s.get('shape_dist_traveled') else None)
        for s in stop_times
    ]
    cur.executemany(
        "INSERT INTO metro_stop_times (trip_id, stop_sequence, stop_id, arrival_time, departure_time, shape_dist_traveled) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        st_rows
    )

    for f in fare_attrs:
        cur.execute(
            "INSERT INTO metro_fare_attributes VALUES (%s,%s,%s,%s,%s,%s)",
            (f['fare_id'], f['price'], f['currency_type'], int(f['payment_method']),
             int(f['transfers']) if f['transfers'] else None, f['agency_id'])
        )
    fare_rule_rows = [(f['origin_id'], f['destination_id'], f['fare_id']) for f in fare_rules]
    cur.executemany("INSERT INTO metro_fare_rules VALUES (%s,%s,%s)", fare_rule_rows)

    conn.commit()
    print("Load complete.\n")

    cur.execute("SELECT count(*) FROM metro_trips")
    print("Trips loaded:", cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM metro_stop_times")
    print("Stop-time rows loaded:", cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM metro_stops")
    print("Stops loaded:", cur.fetchone()[0])
    cur.execute("SELECT shape_id, ROUND(total_length_m,1) FROM metro_shapes ORDER BY shape_id")
    print("Shape lengths (meters):")
    for row in cur.fetchall():
        print(" ", row)

    cur.close()
    conn.close()


if __name__ == "__main__":
    default_folder = os.path.join(os.path.dirname(__file__), "..", "gtfs_data")
    folder = sys.argv[1] if len(sys.argv) > 1 else default_folder
    main(folder)
