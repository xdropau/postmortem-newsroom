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
CREATE TABLE IF NOT EXISTS second_checks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, run_at TEXT NOT NULL, by TEXT, docs_read INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS second_check_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, story_id INTEGER NOT NULL, key TEXT, label TEXT, claim TEXT,
  result TEXT, evidence TEXT, source_url TEXT, material INTEGER DEFAULT 1,
  resolved_result TEXT, resolved_note TEXT, resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_sci_run ON second_check_items(run_id);

CREATE TABLE IF NOT EXISTS evidence_archive(
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, url TEXT NOT NULL, issuer TEXT, published_at TEXT,
  retrieved_at TEXT NOT NULL, http_status INTEGER, sha256 TEXT, bytes INTEGER, extract TEXT,
  wayback_url TEXT, wayback_status TEXT, full_copy_path TEXT, official INTEGER DEFAULT 0, error TEXT,
  UNIQUE(story_id, url, sha256)
);

CREATE TABLE IF NOT EXISTS link_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, ok INTEGER NOT NULL, claims_sig TEXT
);
CREATE INDEX IF NOT EXISTS ix_link_runs ON link_runs(story_id, kind, id);
CREATE TABLE IF NOT EXISTS link_checks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, story_id INTEGER NOT NULL, claim_id INTEGER, url TEXT,
  ok INTEGER NOT NULL, http_status TEXT, text_found INTEGER, archived INTEGER, reason TEXT
);
CREATE TABLE IF NOT EXISTS change_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, story_id INTEGER, kind TEXT NOT NULL, ref TEXT, detail TEXT,
  prev_hash TEXT, row_hash TEXT
);
CREATE INDEX IF NOT EXISTS ix_cl_story ON change_log(story_id);
CREATE TRIGGER IF NOT EXISTS cl_no_delete BEFORE DELETE ON change_log BEGIN SELECT RAISE(ABORT,'change_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS cl_no_edit BEFORE UPDATE ON change_log WHEN OLD.row_hash IS NOT NULL
  BEGIN SELECT RAISE(ABORT,'change_log is append-only'); END;

-- history tables can be appended to, never deleted from; versions are immutable
CREATE TRIGGER IF NOT EXISTS ap_no_delete BEFORE DELETE ON approvals BEGIN SELECT RAISE(ABORT,'approvals history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS co_no_delete BEFORE DELETE ON corrections BEGIN SELECT RAISE(ABORT,'corrections history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS cv_no_delete BEFORE DELETE ON content_versions BEGIN SELECT RAISE(ABORT,'versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS cv_no_edit BEFORE UPDATE ON content_versions BEGIN SELECT RAISE(ABORT,'a saved version is immutable; save a new version'); END;
CREATE TRIGGER IF NOT EXISTS ea_no_delete BEFORE DELETE ON evidence_archive BEGIN SELECT RAISE(ABORT,'evidence archive is append-only'); END;
CREATE TRIGGER IF NOT EXISTS sc_no_delete BEFORE DELETE ON second_checks BEGIN SELECT RAISE(ABORT,'second checks are append-only'); END;
CREATE TRIGGER IF NOT EXISTS sci_no_delete BEFORE DELETE ON second_check_items BEGIN SELECT RAISE(ABORT,'second checks are append-only'); END;

-- automatic logging: every change to claims, approvals, versions, corrections, status and second checks is recorded
CREATE TRIGGER IF NOT EXISTS lg_claim_ins AFTER INSERT ON claims BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'claim_created','claim '||NEW.id,NEW.status||': '||substr(NEW.claim_text,1,160)); END;
CREATE TRIGGER IF NOT EXISTS lg_claim_upd AFTER UPDATE OF status,note,evidence,source_url ON claims
  WHEN IFNULL(OLD.status,'')<>IFNULL(NEW.status,'') OR IFNULL(OLD.note,'')<>IFNULL(NEW.note,'') BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'claim_changed','claim '||NEW.id,
    IFNULL(OLD.status,'')||' -> '||NEW.status||IFNULL(' | note: '||substr(NEW.note,1,120),'')||IFNULL(' | evidence: '||substr(NEW.evidence,1,120),'')); END;
CREATE TRIGGER IF NOT EXISTS lg_approval AFTER INSERT ON approvals BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'approval_'||NEW.action,IFNULL(NEW.kind,'story')||IFNULL(' v'||NEW.version,''),
    IFNULL('dest='||NEW.destination,'')||IFNULL(' hash='||substr(NEW.content_hash,1,12),'')||IFNULL(' | '||substr(NEW.reason,1,160),'')); END;
CREATE TRIGGER IF NOT EXISTS lg_approval_void AFTER UPDATE OF valid ON approvals WHEN OLD.valid=1 AND NEW.valid=0 BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'approval_voided',IFNULL(NEW.kind,'story')||IFNULL(' v'||NEW.version,''),IFNULL(NEW.invalidated_reason,'')); END;
CREATE TRIGGER IF NOT EXISTS lg_version AFTER INSERT ON content_versions BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'version_saved',NEW.kind||' v'||NEW.version,'hash='||substr(NEW.content_hash,1,12)||' by '||IFNULL(NEW.author,'')); END;
CREATE TRIGGER IF NOT EXISTS lg_correction AFTER INSERT ON corrections BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'correction_logged','correction '||NEW.id,IFNULL(NEW.severity,'')||': '||substr(NEW.text,1,200)); END;
CREATE TRIGGER IF NOT EXISTS lg_correction_pub AFTER UPDATE OF status ON corrections WHEN OLD.status<>NEW.status BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'correction_'||NEW.status,'correction '||NEW.id,IFNULL(NEW.note,'')); END;
CREATE TRIGGER IF NOT EXISTS lg_status AFTER UPDATE OF editorial_status,publication_status ON stories
  WHEN IFNULL(OLD.editorial_status,'')<>IFNULL(NEW.editorial_status,'') OR IFNULL(OLD.publication_status,'')<>IFNULL(NEW.publication_status,'') BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.id,
    CASE WHEN NEW.publication_status<>IFNULL(OLD.publication_status,'') THEN 'publication_record' ELSE 'editorial_status' END,'story',
    'editorial '||IFNULL(OLD.editorial_status,'')||' -> '||NEW.editorial_status||'; publication '||IFNULL(OLD.publication_status,'')||' -> '||NEW.publication_status); END;
CREATE TRIGGER IF NOT EXISTS lg_links AFTER INSERT ON link_runs BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'link_check_run','run '||NEW.id,NEW.kind||': '||CASE NEW.ok WHEN 1 THEN 'PASS' ELSE 'FAIL' END); END;
CREATE TRIGGER IF NOT EXISTS lk_no_delete BEFORE DELETE ON link_runs BEGIN SELECT RAISE(ABORT,'link checks are append-only'); END;
CREATE TRIGGER IF NOT EXISTS lkc_no_delete BEFORE DELETE ON link_checks BEGIN SELECT RAISE(ABORT,'link checks are append-only'); END;
CREATE TRIGGER IF NOT EXISTS lg_second AFTER INSERT ON second_checks BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'second_check_run','run '||NEW.id,'by '||IFNULL(NEW.by,'')||'; pages read '||NEW.docs_read); END;
CREATE TRIGGER IF NOT EXISTS lg_second_resolve AFTER UPDATE OF resolved_result ON second_check_items WHEN NEW.resolved_result IS NOT NULL BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'second_check_resolved',NEW.key,
    'human set '||NEW.resolved_result||' (was '||NEW.result||'): '||IFNULL(substr(NEW.resolved_note,1,160),'')); END;
CREATE TRIGGER IF NOT EXISTS lg_evidence AFTER INSERT ON evidence_archive BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'evidence_archived',NEW.url,
    'http '||IFNULL(NEW.http_status,'-')||' sha256='||IFNULL(substr(NEW.sha256,1,16),'-')||IFNULL(' wayback='||NEW.wayback_url,'')); END;
CREATE TRIGGER IF NOT EXISTS perm_stories BEFORE DELETE ON stories WHEN EXISTS(SELECT 1 FROM stories s WHERE s.id=OLD.id AND (s.editorial_status IN ('verifying','ready','approved','published','rejected') OR s.article_id IS NOT NULL OR s.video_id IS NOT NULL OR EXISTS(SELECT 1 FROM approvals a WHERE a.story_id=s.id) OR EXISTS(SELECT 1 FROM evidence_archive e WHERE e.story_id=s.id) OR EXISTS(SELECT 1 FROM corrections c WHERE c.story_id=s.id) OR EXISTS(SELECT 1 FROM content_versions v WHERE v.story_id=s.id) OR EXISTS(SELECT 1 FROM second_checks k WHERE k.story_id=s.id) OR EXISTS(SELECT 1 FROM story_links l WHERE l.story_id=s.id)))
  BEGIN SELECT RAISE(ABORT,'permanent record: story has editorial history and cannot be pruned'); END;
CREATE TRIGGER IF NOT EXISTS perm_items BEFORE DELETE ON items WHEN EXISTS(SELECT 1 FROM stories s WHERE s.id=OLD.story_id AND (s.editorial_status IN ('verifying','ready','approved','published','rejected') OR s.article_id IS NOT NULL OR s.video_id IS NOT NULL OR EXISTS(SELECT 1 FROM approvals a WHERE a.story_id=s.id) OR EXISTS(SELECT 1 FROM evidence_archive e WHERE e.story_id=s.id) OR EXISTS(SELECT 1 FROM corrections c WHERE c.story_id=s.id) OR EXISTS(SELECT 1 FROM content_versions v WHERE v.story_id=s.id) OR EXISTS(SELECT 1 FROM second_checks k WHERE k.story_id=s.id) OR EXISTS(SELECT 1 FROM story_links l WHERE l.story_id=s.id)))
  BEGIN SELECT RAISE(ABORT,'permanent record: story has editorial history and cannot be pruned'); END;
CREATE TRIGGER IF NOT EXISTS perm_claims BEFORE DELETE ON claims WHEN EXISTS(SELECT 1 FROM stories s WHERE s.id=OLD.story_id AND (s.editorial_status IN ('verifying','ready','approved','published','rejected') OR s.article_id IS NOT NULL OR s.video_id IS NOT NULL OR EXISTS(SELECT 1 FROM approvals a WHERE a.story_id=s.id) OR EXISTS(SELECT 1 FROM evidence_archive e WHERE e.story_id=s.id) OR EXISTS(SELECT 1 FROM corrections c WHERE c.story_id=s.id) OR EXISTS(SELECT 1 FROM content_versions v WHERE v.story_id=s.id) OR EXISTS(SELECT 1 FROM second_checks k WHERE k.story_id=s.id) OR EXISTS(SELECT 1 FROM story_links l WHERE l.story_id=s.id)))
  BEGIN SELECT RAISE(ABORT,'permanent record: story has editorial history and cannot be pruned'); END;
CREATE TRIGGER IF NOT EXISTS perm_source_checks BEFORE DELETE ON source_checks WHEN EXISTS(SELECT 1 FROM stories s WHERE s.id=OLD.story_id AND (s.editorial_status IN ('verifying','ready','approved','published','rejected') OR s.article_id IS NOT NULL OR s.video_id IS NOT NULL OR EXISTS(SELECT 1 FROM approvals a WHERE a.story_id=s.id) OR EXISTS(SELECT 1 FROM evidence_archive e WHERE e.story_id=s.id) OR EXISTS(SELECT 1 FROM corrections c WHERE c.story_id=s.id) OR EXISTS(SELECT 1 FROM content_versions v WHERE v.story_id=s.id) OR EXISTS(SELECT 1 FROM second_checks k WHERE k.story_id=s.id) OR EXISTS(SELECT 1 FROM story_links l WHERE l.story_id=s.id)))
  BEGIN SELECT RAISE(ABORT,'permanent record: story has editorial history and cannot be pruned'); END;

CREATE TRIGGER IF NOT EXISTS lg_live AFTER UPDATE OF status ON live_updates WHEN OLD.status<>NEW.status BEGIN
  INSERT INTO change_log(at,story_id,kind,ref,detail) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),NEW.story_id,'live_update_'||NEW.status,'update '||NEW.id,substr(NEW.text,1,120)); END;
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
                 "social_refs": "TEXT", "format": "TEXT DEFAULT 'story'", "test_only": "INTEGER DEFAULT 0"}


ITEM_COLUMNS = {"tags": "TEXT"}


CLAIM_COLUMNS = {"cite_text": "TEXT", "link_override": "TEXT"}


def migrate(conn):
    """Add columns to databases created before the editorial layer existed. Safe to run every time."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(stories)")}
    for col, decl in STORY_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE stories ADD COLUMN {col} {decl}")
    conn.execute("UPDATE stories SET test_only=1 WHERE category='TEST CARD' AND COALESCE(test_only,0)=0")      # test cards are always test-only
    have = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    for col, decl in ITEM_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {decl}")
    have = {r["name"] for r in conn.execute("PRAGMA table_info(claims)")}
    for col, decl in CLAIM_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE claims ADD COLUMN {col} {decl}")
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
