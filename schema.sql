-- Weight entries with source tracking
CREATE TABLE weight_log (
  id BIGSERIAL PRIMARY KEY,
  weight_kg NUMERIC(5,2) NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual',  -- 'manual', 'strava', 'discord'
  logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  note TEXT
);

CREATE INDEX idx_weight_log_logged_at ON weight_log (logged_at DESC);

-- Strava activities (denormalized for fast queries)
CREATE TABLE activities (
  id BIGSERIAL PRIMARY KEY,
  strava_id BIGINT UNIQUE NOT NULL,
  sport_type TEXT NOT NULL,
  name TEXT,
  distance_m NUMERIC(10,1),
  moving_time_s INTEGER,
  elapsed_time_s INTEGER,
  total_elevation_gain NUMERIC(6,1),
  average_heartrate NUMERIC(4,1),
  max_heartrate NUMERIC(4,1),
  average_speed NUMERIC(6,3),
  start_date TIMESTAMPTZ NOT NULL,
  suffer_score INTEGER,
  raw_json JSONB,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_activities_start_date ON activities (start_date DESC);
CREATE INDEX idx_activities_sport_type ON activities (sport_type);

-- Food log entries
CREATE TABLE food_log (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  portion TEXT,
  calories INTEGER,
  protein NUMERIC(5,1),
  fat NUMERIC(5,1),
  carbs NUMERIC(5,1),
  sugar NUMERIC(5,1),
  fiber NUMERIC(5,1),
  source TEXT NOT NULL DEFAULT 'photo',  -- 'photo', 'text', 'combined'
  verdict TEXT,
  logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_food_log_logged_at ON food_log (logged_at DESC);

-- Weekly analysis snapshots
CREATE TABLE weekly_snapshots (
  id BIGSERIAL PRIMARY KEY,
  week_start DATE NOT NULL,
  week_end DATE NOT NULL,
  total_km NUMERIC(6,2),
  run_count INTEGER,
  gym_count INTEGER,
  avg_hr NUMERIC(4,1),
  zones_agg JSONB,
  avg_decoupling NUMERIC(5,2),
  avg_adj_pace_sec NUMERIC(6,1),
  goal_progress INTEGER,
  weight_kg NUMERIC(5,2),
  insight TEXT,
  goal_reflection TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(week_start)
);
