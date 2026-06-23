-- Run this in Supabase SQL Editor to create the required tables

-- Athlete profile: key-value store for dynamic fields (Max HR, Weight, etc.)
CREATE TABLE IF NOT EXISTS athlete_profile (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    field TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Weight log: persistent weight history
CREATE TABLE IF NOT EXISTS weight_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kg REAL NOT NULL,
    date DATE NOT NULL,
    logged_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_weight_log_date ON weight_log (date);

-- Food entries: daily nutrition log
CREATE TABLE IF NOT EXISTS food_entries (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    food TEXT NOT NULL DEFAULT 'unknown',
    calories INTEGER NOT NULL DEFAULT 0,
    protein_g REAL NOT NULL DEFAULT 0,
    carbs_g REAL NOT NULL DEFAULT 0,
    fat_g REAL NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    date DATE NOT NULL,
    time_label TEXT DEFAULT '',
    logged_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_food_entries_date ON food_entries (date);

-- Seed initial profile from about_me.md
INSERT INTO athlete_profile (field, value) VALUES
    ('Born', '11 April 1997 at Bekasi, Indonesia'),
    ('Gender', 'Male'),
    ('Height', '180 cm'),
    ('Weight', '78 kg'),
    ('Primary', 'Running, Gym (strength training)'),
    ('Max HR', '191')
ON CONFLICT (field) DO NOTHING;
