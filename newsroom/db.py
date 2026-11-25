"""SQLite persistence. One file (data/newsroom.db), journal mode DELETE so it commits cleanly to git."""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  headline TEXT NOT NULL, headline_norm TEXT NOT NULL,
  source_org TEXT, original_url TEXT, primary_url TEXT,
  published_at TEXT, pub_known INTEGER DEFAULT 0,
  detected_at TEXT NOT NULL, last_item_at TEXT NOT NULL,
  category TEXT, score INTEGER DEFAULT 0, score_json TEXT,
  evidence_ok INTEGER DEFAULT 0, sensational INTEGER DEFAULT 0, lead_only INTEGER DEFAULT 0,
  verification_status TEXT DEFAULT 'UNVERIFIED',
  editorial_status TEXT DEFAULT 'new',
  publication_status TEXT DEFAULT 'unpublished',
  package_path TEXT, fact_register TEXT, script TEXT, article_draft TEXT, supporting_docs TEXT
);
CREATE INDEX IF NOT EXISTS ix_stories_last ON stories(last_item_at);
CREATE INDEX IF NOT EXISTS ix_stories_score ON stories(score);

CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id INTEGER NOT NULL REFERENCES stories(id),
  source_id TEXT, org TEXT, bot TEXT, tier TEXT,
  title TEXT, url TEXT, canonical_url TEXT UNIQUE, content_hash TEXT,
  published_at TEXT, pub_known INTEGER DEFAULT 0, detected_at TEXT NOT NULL,
  summary TEXT, has_image INTEGER DEFAULT 0, primary_links TEXT
);
CREATE INDEX IF NOT EXISTS ix_items_story ON items(story_id);
CREATE INDEX IF NOT EXISTS ix_items_hash ON items(content_hash);

CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id INTEGER NOT NULL REFERENCES stories(id),
  claim_type TEXT, claim_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'UNVERIFIED', manual INTEGER DEFAULT 0,
  source_url TEXT, evidence TEXT, note TEXT, checked_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_claims_story ON claims(story_id);

CREATE TABLE IF NOT EXISTS source_checks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id INTEGER, url TEXT, opened_at TEXT, http_status INTEGER, bytes INTEGER, sha256 TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE, kind TEXT, story_id INTEGER, sent_at TEXT, ok INTEGER, payload TEXT
);

CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot TEXT, started_at TEXT, finished_at TEXT, status TEXT,
  items_seen INTEGER DEFAULT 0, items_new INTEGER DEFAULT 0, detail TEXT,
  env TEXT, run_ref TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_bot ON runs(bot, id);

CREATE TABLE IF NOT EXISTS source_health(
  source_id TEXT PRIMARY KEY, bot TEXT, last_success TEXT, last_attempt TEXT, last_error TEXT,
  consecutive_failures INTEGER DEFAULT 0, last_items INTEGER DEFAULT 0, last_http INTEGER
);

CREATE TABLE IF NOT EXISTS api_usage(day TEXT, service TEXT, calls INTEGER DEFAULT 0, PRIMARY KEY(day, service));
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS content_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL REFERENCES stories(id), kind TEXT NOT NULL,
  version INTEGER NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL, author TEXT,
  UNIQUE(story_id, kind, version)
);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, kind TEXT, version INTEGER, content_hash TEXT,
  action TEXT NOT NULL, destination TEXT DEFAULT 'all', reason TEXT, at TEXT NOT NULL,
  valid INTEGER DEFAULT 1, invalidated_at TEXT, invalidated_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_approvals_story ON approvals(story_id, kind);
CREATE TABLE IF NOT EXISTS corrections(
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, text TEXT NOT NULL, severity TEXT,
  fixes_version INTEGER, created_at TEXT NOT NULL, status TEXT DEFAULT 'open', published_at TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER, title TEXT NOT NULL, documents TEXT, significance TEXT, angle TEXT,
  alternatives TEXT, missing_evidence TEXT, next_steps TEXT, parties TEXT, status TEXT DEFAULT 'new',
  originality TEXT DEFAULT 'NOT CHECKED', created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS live_coverage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER UNIQUE NOT NULL, title TEXT, state TEXT DEFAULT 'LIVE',
  opened_at TEXT, state_changed_at TEXT
);
CREATE TABLE IF NOT EXISTS live_updates(
  id INTEGER PRIMARY KEY AUTOINCREMENT, coverage_id INTEGER NOT NULL, story_id INTEGER NOT NULL, text TEXT NOT NULL, source_url TEXT,
  classification TEXT DEFAULT 'UNVERIFIED', status TEXT DEFAULT 'draft', item_id INTEGER, created_at TEXT,
  approved_at TEXT, approved_hash TEXT, content_hash TEXT
);
CREATE TABLE IF NOT EXISTS story_links(
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, created_at TEXT,
  UNIQUE(story_id, kind, ref)
);
"""


def connect(data_dir: str) -> sqlite3.Connection:
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(data_dir, "newsroom.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


STORY_COLUMNS = {"breaking": "INTEGER DEFAULT 0", "breaking_until": "TEXT", "article_id": "TEXT", "video_id": "TEXT",
                 "social_refs": "TEXT", "format": "TEXT DEFAULT 'story'"}


def migrate(conn):
    """Add columns to databases created before the editorial layer existed. Safe to run every time."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(stories)")}
    for col, decl in STORY_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE stories ADD COLUMN {col} {decl}")
    conn.commit()


def kv_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def kv_set(conn, key, value):
    conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    conn.commit()


def bump_usage(conn, service: str, n: int, day: str):
    conn.execute("INSERT INTO api_usage(day,service,calls) VALUES(?,?,?) "
                 "ON CONFLICT(day,service) DO UPDATE SET calls=calls+excluded.calls", (day, service, n))
    conn.commit()
