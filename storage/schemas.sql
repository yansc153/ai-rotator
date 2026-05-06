CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  company_name TEXT NOT NULL,
  side TEXT NOT NULL,
  horizon TEXT NOT NULL,
  sector TEXT NOT NULL,
  pool TEXT NOT NULL,
  thesis TEXT NOT NULL,
  conviction REAL NOT NULL,
  current_price REAL NOT NULL,
  entry_low REAL,
  entry_high REAL,
  target_1 REAL,
  target_2 REAL,
  stop_loss REAL,
  rr REAL,
  leading_sector_json TEXT NOT NULL,
  transmission_event_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recommendation_id INTEGER NOT NULL,
  review_horizon TEXT NOT NULL,
  review_date TEXT NOT NULL,
  close_price REAL NOT NULL,
  max_favorable_excursion REAL,
  max_adverse_excursion REAL,
  pnl_pct REAL NOT NULL,
  thesis_valid INTEGER NOT NULL,
  failure_layer TEXT,
  failure_reason TEXT,
  reviewer_patch TEXT,
  FOREIGN KEY (recommendation_id) REFERENCES recommendations(id)
);

CREATE TABLE IF NOT EXISTS weekly_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  week_start TEXT NOT NULL,
  week_end TEXT NOT NULL,
  summary_md TEXT NOT NULL,
  worst_five_json TEXT NOT NULL,
  best_rules_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
