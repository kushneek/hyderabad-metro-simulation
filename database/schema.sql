-- HMRL Metro Timetable Simulation Database Schema (MySQL 8.0+ version)
-- Same structure as the PostgreSQL/PostGIS schema, adapted for MySQL's spatial types.
-- Note: total_length_m is computed in Python at load time (haversine over shape points),
-- not via a MySQL spatial function -- MySQL's ST_Length doesn't give meters for lon/lat
-- geometry without extra work, and the backend doesn't actually use this value anyway
-- (position math is done in plain Python from GTFS shape_dist_traveled, not database geometry).
USE hmrl_metro;
SHOW TABLES;

CREATE USER IF NOT EXISTS 'metro_user'@'localhost'
IDENTIFIED BY 'Kushagra@8221';

GRANT ALL PRIVILEGES ON hmrl_metro.* TO 'metro_user'@'localhost';

FLUSH PRIVILEGES;

DROP TABLE IF EXISTS metro_stop_times;
DROP TABLE IF EXISTS metro_trips;
DROP TABLE IF EXISTS metro_calendar;
DROP TABLE IF EXISTS metro_shape_points;
DROP TABLE IF EXISTS metro_shapes;
DROP TABLE IF EXISTS metro_routes;
DROP TABLE IF EXISTS metro_stops;
DROP TABLE IF EXISTS metro_agency;
DROP TABLE IF EXISTS metro_fare_rules;
DROP TABLE IF EXISTS metro_fare_attributes;
DROP TABLE IF EXISTS gtfs_feed_versions;

CREATE TABLE gtfs_feed_versions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    feed_publisher_name VARCHAR(255),
    feed_start_date DATE,
    feed_end_date DATE,
    loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source_filename VARCHAR(255),
    is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE metro_agency (
    agency_id VARCHAR(50) PRIMARY KEY,
    agency_name VARCHAR(255),
    agency_url VARCHAR(500),
    agency_timezone VARCHAR(100),
    agency_lang VARCHAR(10),
    agency_fare_url VARCHAR(500),
    agency_email VARCHAR(255),
    agency_phone VARCHAR(50)
);

-- stops.txt: platforms, parent stations, AND entrances (location_type 0/1/2)
-- Storing lon/lat as plain DOUBLE columns rather than MySQL's POINT type: MySQL's spatial
-- functions expect (lat, lon) axis order for SRID 4326, opposite of the (lon, lat) convention
-- used elsewhere in this project and in GTFS itself. Since nothing here actually needs a
-- MySQL spatial query, plain columns avoid that entire class of silent coordinate-order bugs.
CREATE TABLE metro_stops (
    stop_id VARCHAR(50) PRIMARY KEY,
    stop_name VARCHAR(255),
    zone_id VARCHAR(50),
    location_type INT,          -- 0 = platform, 1 = parent station, 2 = entrance/exit
    parent_station VARCHAR(50),
    platform_code VARCHAR(20),
    lon DOUBLE,
    lat DOUBLE
);
CREATE INDEX idx_metro_stops_parent ON metro_stops (parent_station);

CREATE TABLE metro_routes (
    route_id VARCHAR(50) PRIMARY KEY,
    agency_id VARCHAR(50),
    route_short_name VARCHAR(100),
    route_long_name VARCHAR(255),
    route_type INT,
    route_color VARCHAR(10),
    route_text_color VARCHAR(10),
    route_sort_order INT,
    FOREIGN KEY (agency_id) REFERENCES metro_agency(agency_id)
);

-- calendar.txt: which days each service pattern runs
CREATE TABLE metro_calendar (
    service_id VARCHAR(50) PRIMARY KEY,
    monday BOOLEAN, tuesday BOOLEAN, wednesday BOOLEAN, thursday BOOLEAN,
    friday BOOLEAN, saturday BOOLEAN, sunday BOOLEAN,
    start_date DATE,
    end_date DATE
);
-- Note: calendar_dates.txt (exceptions) is NOT present in this HMRL feed (confirmed by inspection).

-- shapes.txt: one row per line geometry
CREATE TABLE metro_shapes (
    shape_id VARCHAR(50) PRIMARY KEY,
    total_length_m DOUBLE       -- computed in Python (haversine over shape points), see load_gtfs.py
);

-- Raw shape points, in order, with cumulative distance -- this is what position math actually uses
CREATE TABLE metro_shape_points (
    shape_id VARCHAR(50),
    shape_pt_sequence INT,
    shape_dist_traveled DOUBLE,
    lon DOUBLE,
    lat DOUBLE,
    PRIMARY KEY (shape_id, shape_pt_sequence)
);

-- trips.txt: one scheduled journey per row
CREATE TABLE metro_trips (
    trip_id VARCHAR(50) PRIMARY KEY,
    route_id VARCHAR(50),
    service_id VARCHAR(50),
    direction_id INT,
    trip_headsign VARCHAR(255),
    block_id VARCHAR(50),        -- physical vehicle rotation grouping (not used in Phase 1 simulation math)
    shape_id VARCHAR(50),
    FOREIGN KEY (route_id) REFERENCES metro_routes(route_id),
    FOREIGN KEY (service_id) REFERENCES metro_calendar(service_id),
    FOREIGN KEY (shape_id) REFERENCES metro_shapes(shape_id),
    INDEX idx_metro_trips_service (service_id),
    INDEX idx_metro_trips_route (route_id),
    INDEX idx_metro_trips_block (block_id)
);

-- stop_times.txt: arrival/departure at each stop for each trip
CREATE TABLE metro_stop_times (
    trip_id VARCHAR(50),
    stop_sequence INT,
    stop_id VARCHAR(50),
    arrival_time INT,           -- SECONDS SINCE SERVICE-DAY MIDNIGHT, handles GTFS >24:00:00 safely
    departure_time INT,
    shape_dist_traveled DOUBLE,
    PRIMARY KEY (trip_id, stop_sequence),
    FOREIGN KEY (trip_id) REFERENCES metro_trips(trip_id),
    FOREIGN KEY (stop_id) REFERENCES metro_stops(stop_id),
    INDEX idx_metro_stop_times_stop (stop_id)
);

CREATE TABLE metro_fare_attributes (
    fare_id VARCHAR(50) PRIMARY KEY,
    price DECIMAL(10,2),
    currency_type VARCHAR(10),
    payment_method INT,
    transfers INT,
    agency_id VARCHAR(50)
);

CREATE TABLE metro_fare_rules (
    origin_id VARCHAR(50),
    destination_id VARCHAR(50),
    fare_id VARCHAR(50),
    FOREIGN KEY (fare_id) REFERENCES metro_fare_attributes(fare_id)
);

SELECT user, host
FROM mysql.user;

SELECT user, host, plugin
FROM mysql.user
WHERE user = 'metro_user';

ALTER USER 'metro_user'@'localhost'
IDENTIFIED BY 'Kushagra@8221';

GRANT ALL PRIVILEGES ON hmrl_metro.* TO 'metro_user'@'localhost';

FLUSH PRIVILEGES;