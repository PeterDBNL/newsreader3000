"""SQLite storage for groups, article headers (with full-text search) and jobs."""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups(
  name TEXT PRIMARY KEY, low INTEGER DEFAULT 0, high INTEGER DEFAULT 0, status TEXT DEFAULT 'y',
  description TEXT DEFAULT '', subscribed INTEGER DEFAULT 0,
  first_fetched INTEGER DEFAULT 0, last_fetched INTEGER DEFAULT 0, updated_at INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_groups_sub ON groups(subscribed);

CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY, grp TEXT NOT NULL, num INTEGER NOT NULL, subject TEXT, poster TEXT,
  date INTEGER, msgid TEXT, parent TEXT, bytes INTEGER, lines INTEGER,
  file_key TEXT, release_key TEXT, part INTEGER, total INTEGER, filename TEXT,
  is_read INTEGER DEFAULT 0, UNIQUE(grp, num));
CREATE INDEX IF NOT EXISTS idx_art_grp_date ON articles(grp, date);
CREATE INDEX IF NOT EXISTS idx_art_file ON articles(grp, file_key);
CREATE INDEX IF NOT EXISTS idx_art_rel ON articles(grp, release_key);
CREATE INDEX IF NOT EXISTS idx_art_msgid ON articles(msgid);
CREATE INDEX IF NOT EXISTS idx_art_parent ON articles(parent);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
  subject, poster, content='articles', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS art_ai AFTER INSERT ON articles BEGIN
  INSERT INTO articles_fts(rowid, subject, poster) VALUES (new.id, new.subject, new.poster);
END;
CREATE TRIGGER IF NOT EXISTS art_ad AFTER DELETE ON articles BEGIN
  INSERT INTO articles_fts(articles_fts, rowid, subject, poster) VALUES('delete', old.id, old.subject, old.poster);
END;

CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, name TEXT, category TEXT DEFAULT '', status TEXT DEFAULT 'queued',
  priority INTEGER DEFAULT 0, total_bytes INTEGER DEFAULT 0, done_bytes INTEGER DEFAULT 0,
  missing_segments INTEGER DEFAULT 0, total_segments INTEGER DEFAULT 0,
  password TEXT DEFAULT '', extract INTEGER DEFAULT 1, repair INTEGER DEFAULT 1, delete_archives INTEGER DEFAULT 1,
  nzb_path TEXT, work_dir TEXT DEFAULT '', out_dir TEXT DEFAULT '', message TEXT DEFAULT '',
  created INTEGER, started INTEGER, finished INTEGER);
"""


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    def many(self, sql, rows):
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ---- groups ----
    def upsert_groups(self, rows):
        """rows: (name, low, high, status)"""
        now = int(time.time())
        self.many(
            "INSERT INTO groups(name, low, high, status, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET low=excluded.low, high=excluded.high, "
            "status=excluded.status, updated_at=excluded.updated_at",
            [(n, lo, hi, st, now) for n, lo, hi, st in rows])

    def set_descriptions(self, rows):
        self.many("UPDATE groups SET description=? WHERE name=?", [(d, n) for n, d in rows])

    def subscribe(self, names, on=True):
        now = int(time.time())
        self.many("INSERT INTO groups(name, subscribed, updated_at) VALUES(?,?,?) "
                  "ON CONFLICT(name) DO UPDATE SET subscribed=excluded.subscribed",
                  [(n, 1 if on else 0, now) for n in names])

    def insert_articles(self, rows):
        self.many(
            "INSERT OR IGNORE INTO articles(grp, num, subject, poster, date, msgid, parent, bytes, lines, "
            "file_key, release_key, part, total, filename) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
