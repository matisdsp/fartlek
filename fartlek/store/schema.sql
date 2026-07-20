-- Fartlek per-account store schema (DESIGN.md §3.3).
-- One database per Garmin account at ~/.fartlek/<garmin-user-id>/store.db.
-- All dates are Garmin calendarDate strings (YYYY-MM-DD, athlete-local).

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ~25 digested scalars per day. Sleep belongs to its wake-date.
-- daily_load is the materialized sum of activities.load for the day (rest day = 0).
CREATE TABLE IF NOT EXISTS days (
    date               TEXT PRIMARY KEY,
    steps              INTEGER,
    resting_hr         INTEGER,
    min_hr             INTEGER,
    max_hr             INTEGER,
    avg_stress         INTEGER,
    max_stress         INTEGER,
    body_battery_high  INTEGER,
    body_battery_low   INTEGER,
    body_battery_wake  INTEGER,
    spo2_avg           REAL,
    intensity_mod_min  INTEGER,
    intensity_vig_min  INTEGER,
    calories_total     INTEGER,
    calories_active    INTEGER,
    distance_m         REAL,
    floors             REAL,
    weight_g           INTEGER,
    sleep_score        INTEGER,
    sleep_duration_h   REAL,
    sleep_deep_h       REAL,
    sleep_light_h      REAL,
    sleep_rem_h        REAL,
    sleep_awake_h      REAL,
    sleep_need_h       REAL,          -- NULL => renderer discloses "default 8h"
    sleep_start_ts     TEXT,          -- local ISO timestamp
    sleep_end_ts       TEXT,
    hrv_last_night     REAL,          -- avgOvernightHrv (ms)
    hrv_status         TEXT,          -- Garmin enum (BALANCED/UNBALANCED/LOW/...)
    hrv_weekly_avg     REAL,
    daily_load         REAL NOT NULL DEFAULT 0,
    srpe_load          REAL,          -- parallel internal-load ledger, NEVER mixed into PMC
    synced_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id     INTEGER PRIMARY KEY,
    date            TEXT NOT NULL,             -- local calendar date
    sport           TEXT NOT NULL,             -- Garmin typeKey
    name            TEXT,
    start_local     TEXT,
    duration_s      REAL,
    moving_s        REAL,
    distance_m      REAL,
    avg_hr          INTEGER,
    max_hr          INTEGER,
    avg_speed       REAL,                      -- m/s
    calories        INTEGER,
    elevation_gain  REAL,
    load            REAL,                      -- canonical load (see load_source)
    load_source     TEXT NOT NULL DEFAULT 'garmin',
        -- 'garmin' | 'trimp_calibrated' | 'trimp_uncalibrated'
        -- | 'srpe_calibrated' | 'srpe_uncalibrated' | 'estimated' | 'none'
    trimp           REAL,                      -- Edwards TRIMP raw (pre-calibration)
    rpe             INTEGER,                   -- CR-10 scale after conversion
    rpe_source      TEXT,                      -- 'athlete' | 'watch' | NULL
    feel            INTEGER,                   -- 1-5 (watch-native, annotation only)
    aerobic_te      REAL,
    anaerobic_te    REAL,
    vo2max          REAL,
    hr_z1_s         REAL, hr_z2_s REAL, hr_z3_s REAL, hr_z4_s REAL, hr_z5_s REAL,
    extra_json      TEXT,                      -- compact spillover from the list payload
    synced_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);

-- Compact per-night state intervals for SRI (~1 KB/night), raw payload discarded.
CREATE TABLE IF NOT EXISTS sleep_timeline (
    date           TEXT PRIMARY KEY,           -- wake-date
    intervals_json TEXT NOT NULL               -- [["deep"|"light"|"rem"|"awake", start_iso, end_iso], ...]
);

-- EF / decoupling / durability / interval digests. Raw streams discarded after digest.
CREATE TABLE IF NOT EXISTS activity_digests (
    activity_id  INTEGER PRIMARY KEY REFERENCES activities(activity_id),
    kind         TEXT NOT NULL,                -- 'steady' | 'long' | 'interval' | 'other'
    method       TEXT NOT NULL,                -- 'splits' | 'stream'
    ef           REAL,                         -- m/min per bpm (grade-adjusted where available)
    decoupling   REAL,                         -- fraction, (EF_h1-EF_h2)/EF_h1
    durability   REAL,                         -- EF last-third / first-third (runs >=90min)
    hot          INTEGER NOT NULL DEFAULT 0,   -- avg temp >= 24C -> excluded from EF trend
    interval_json TEXT,                        -- per-rep digest for interval sessions
    computed_at  TEXT NOT NULL
);

-- Baseline cache (recomputed at sync; source of truth is the days table).
CREATE TABLE IF NOT EXISTS baselines (
    metric   TEXT NOT NULL,
    date     TEXT NOT NULL,
    window   INTEGER NOT NULL,                 -- 7 | 28 | 60 | 90
    mean     REAL, median REAL, mad_sd REAL, n INTEGER NOT NULL,
    PRIMARY KEY (metric, date, window)
);

CREATE TABLE IF NOT EXISTS pmc (
    date TEXT PRIMARY KEY,
    load REAL NOT NULL,
    ctl  REAL NOT NULL,
    atl  REAL NOT NULL,
    tsb  REAL NOT NULL                          -- CTL_yesterday - ATL_yesterday
);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,               -- first day the condition tripped
    metric        TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('RED','AMBER','WATCH')),
    message       TEXT NOT NULL,
    resolved      INTEGER NOT NULL DEFAULT 0,
    resolved_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(resolved, severity);

CREATE TABLE IF NOT EXISTS wellness_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    flag        TEXT,                           -- 'illness' | 'injury' | NULL
    resolved    INTEGER NOT NULL DEFAULT 0,     -- for injury entries
    rpe         INTEGER,                        -- CR-10
    fatigue     INTEGER, soreness INTEGER, stress INTEGER, sleep_quality INTEGER,  -- Hooper 1-7
    note        TEXT,
    activity_id INTEGER,                        -- optional link for RPE entries
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS athlete_profile (
    key   TEXT PRIMARY KEY,                     -- goal_race_date, goal_race_distance, phase,
    value TEXT NOT NULL                         -- availability, tid_target, lt1_hr_override, ...
);

CREATE TABLE IF NOT EXISTS plan_calendar (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT NOT NULL,
    sport               TEXT,
    name                TEXT,
    source              TEXT NOT NULL,          -- 'calendar' | 'garmin_coach' | 'fartlek'
    planned_json        TEXT,                   -- compact planned-workout digest
    garmin_workout_id   TEXT,
    matched_activity_id INTEGER,                -- NULL => unmatched (missed if date past)
    match_method        TEXT                    -- 'garmin_link' | 'heuristic' | NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_date ON plan_calendar(date);

CREATE TABLE IF NOT EXISTS capability_map (
    key       TEXT PRIMARY KEY,                 -- e.g. 'activityTrainingLoad', 'training_readiness'
    available INTEGER NOT NULL,
    detail    TEXT,
    probed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,                     -- 'last_sync', 'tier2_cursor', per-domain stamps
    value TEXT NOT NULL
);
