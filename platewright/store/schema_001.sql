-- Platewright schema, version 1. Derived numbers (good parts, stock, coverage) are computed,
-- never stored. Every write records who made it (*_by, the Bambuddy username).

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL  -- JSON
);

CREATE TABLE models (
    id               INTEGER PRIMARY KEY,
    slug             TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    ref              TEXT NOT NULL DEFAULT '',
    url              TEXT NOT NULL DEFAULT '',
    designer         TEXT NOT NULL DEFAULT '',
    license          TEXT NOT NULL DEFAULT '',
    distribution_ok  TEXT NOT NULL DEFAULT 'unknown' CHECK (distribution_ok IN ('yes', 'no', 'unknown')),
    min_age          INTEGER,
    small_parts      TEXT NOT NULL DEFAULT 'unknown' CHECK (small_parts IN ('yes', 'no', 'unknown')),
    parts_per_unit   INTEGER NOT NULL DEFAULT 1 CHECK (parts_per_unit >= 1),
    status           TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'qualified', 'failed-test')),
    bambuddy_file_id INTEGER,
    notes            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    created_by       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE model_aliases (
    model_id INTEGER NOT NULL REFERENCES models (id) ON DELETE CASCADE,
    alias    TEXT NOT NULL,
    PRIMARY KEY (model_id, alias)
);

CREATE TABLE qualifications (
    id       INTEGER PRIMARY KEY,
    model_id INTEGER NOT NULL REFERENCES models (id) ON DELETE CASCADE,
    result   TEXT NOT NULL CHECK (result IN ('pass', 'fail')),
    notes    TEXT NOT NULL DEFAULT '',
    at       TEXT NOT NULL,
    by       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE plates (
    id                 INTEGER PRIMARY KEY,
    code               TEXT NOT NULL UNIQUE,  -- "N"; the library file is named PLATE-N_...
    description        TEXT NOT NULL DEFAULT '',
    active             INTEGER NOT NULL DEFAULT 1,
    current_version_id INTEGER REFERENCES plate_versions (id)
);

CREATE TABLE plate_versions (
    id               INTEGER PRIMARY KEY,
    plate_id         INTEGER NOT NULL REFERENCES plates (id) ON DELETE CASCADE,
    version          INTEGER NOT NULL,
    sha256           TEXT NOT NULL,
    bambuddy_file_id INTEGER,
    file_path        TEXT NOT NULL DEFAULT '',  -- Platewright's own copy, under the data directory
    units            INTEGER NOT NULL,
    objects          INTEGER NOT NULL,
    hours            REAL NOT NULL,
    grams            REAL NOT NULL,
    color_changes    INTEGER NOT NULL DEFAULT 0,
    filaments        TEXT NOT NULL DEFAULT '{}',  -- JSON: palette name -> grams
    printer_model    TEXT NOT NULL DEFAULT '',
    printer_model_id TEXT NOT NULL DEFAULT '',
    nozzle           TEXT NOT NULL DEFAULT '',
    warnings         TEXT NOT NULL DEFAULT '[]',  -- JSON: what the checks said when it was locked
    registered_at    TEXT NOT NULL,
    registered_by    TEXT NOT NULL DEFAULT '',
    UNIQUE (plate_id, version)
);
CREATE INDEX plate_versions_sha ON plate_versions (sha256);

CREATE TABLE plate_mix (
    plate_version_id INTEGER NOT NULL REFERENCES plate_versions (id) ON DELETE CASCADE,
    model_id         INTEGER REFERENCES models (id),  -- NULL = slicer objects not linked to a model
    units            INTEGER NOT NULL,
    objects          INTEGER NOT NULL
);

CREATE TABLE runs (
    id               INTEGER PRIMARY KEY,
    bambuddy_run_id  TEXT UNIQUE,  -- print-log id; NULL when entered by hand
    bambuddy_archive_id INTEGER,
    plate_version_id INTEGER NOT NULL REFERENCES plate_versions (id),
    date             TEXT NOT NULL,
    printer          TEXT NOT NULL DEFAULT '',
    outcome          TEXT NOT NULL CHECK (outcome IN ('ok', 'rejected', 'failed', 'stopped', 'cancelled', 'aborted')),
    source           TEXT NOT NULL CHECK (source IN ('bambuddy', 'manual')),
    hours            REAL NOT NULL DEFAULT 0,
    grams            REAL NOT NULL DEFAULT 0,
    filaments        TEXT NOT NULL DEFAULT '{}',
    aborted_pct      INTEGER,
    verdict          TEXT NOT NULL DEFAULT '',  -- Bambuddy's good / reject, '' = none yet
    matched_by_name  INTEGER NOT NULL DEFAULT 0,
    reviewed_at      TEXT,
    reviewed_by      TEXT NOT NULL DEFAULT '',
    notes            TEXT NOT NULL DEFAULT '',
    synced_at        TEXT
);
CREATE INDEX runs_date ON runs (date);

CREATE TABLE run_failures (
    run_id   INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    model_id INTEGER REFERENCES models (id),
    scope    TEXT NOT NULL DEFAULT 'model' CHECK (scope IN ('model', 'unlinked', 'any')),
    count    INTEGER NOT NULL CHECK (count >= 0)
);

CREATE TABLE demands (
    id                  INTEGER PRIMARY KEY,
    slug                TEXT NOT NULL UNIQUE,
    kind                TEXT NOT NULL DEFAULT 'event' CHECK (kind IN ('order', 'event', 'restock')),
    name                TEXT NOT NULL,
    due                 TEXT NOT NULL,
    target              INTEGER NOT NULL DEFAULT 0,  -- 0 = the sum of the needs
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    notes               TEXT NOT NULL DEFAULT '',
    bambuddy_project_id INTEGER,
    created_at          TEXT NOT NULL,
    created_by          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE demand_needs (
    demand_id INTEGER NOT NULL REFERENCES demands (id) ON DELETE CASCADE,
    model_id  INTEGER NOT NULL REFERENCES models (id),
    qty       INTEGER NOT NULL CHECK (qty > 0),
    PRIMARY KEY (demand_id, model_id)
);

CREATE TABLE ledger (
    id        INTEGER PRIMARY KEY,
    date      TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('out', 'adjust')),
    model_id  INTEGER REFERENCES models (id),  -- NULL = parts not linked to a model
    qty       INTEGER NOT NULL,
    demand_id INTEGER REFERENCES demands (id),
    notes     TEXT NOT NULL DEFAULT '',
    by        TEXT NOT NULL DEFAULT ''
);

-- What the last sync saw: counts and messages for the Today page and the badge.
CREATE TABLE sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL  -- JSON
);
