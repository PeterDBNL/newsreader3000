"""SQLite storage for groups, article headers, precomputed files/releases, jobs and the header-fetch log.

Every thread gets its own connection (WAL mode), so browsing and searching keep working
while a header download is writing. Writes are serialised with a lock.
"""
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
CREATE INDEX IF NOT EXISTS idx_art_msgid ON articles(msgid);
CREATE INDEX IF NOT EXISTS idx_art_parent ON articles(parent);

-- One row per file (all parts of one posted file), kept up to date while headers arrive.
CREATE TABLE IF NOT EXISTS files(
  grp TEXT NOT NULL, file_key TEXT NOT NULL, release_key TEXT, subject TEXT, filename TEXT, poster TEXT,
  date INTEGER, size INTEGER, parts INTEGER, total INTEGER, PRIMARY KEY(grp, file_key));
CREATE INDEX IF NOT EXISTS idx_files_rel ON files(grp, release_key);
CREATE INDEX IF NOT EXISTS idx_files_date ON files(grp, date);

-- One row per release (all files of one post), so browsing/searching never has to group millions of rows.
CREATE TABLE IF NOT EXISTS releases(
  id INTEGER PRIMARY KEY, grp TEXT NOT NULL, release_key TEXT NOT NULL, subject TEXT, poster TEXT, date INTEGER,
  size INTEGER, parts INTEGER, expected INTEGER, files INTEGER, nfo TEXT, par2 INTEGER, is_read INTEGER DEFAULT 0,
  UNIQUE(grp, release_key));
CREATE INDEX IF NOT EXISTS idx_rel_date ON releases(grp, date);
CREATE INDEX IF NOT EXISTS idx_rel_size ON releases(grp, size);
CREATE VIRTUAL TABLE IF NOT EXISTS releases_fts USING fts5(release_key, poster, content='releases', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS rel_ai AFTER INSERT ON releases BEGIN
  INSERT INTO releases_fts(rowid, release_key, poster) VALUES (new.id, new.release_key, new.poster);
END;
CREATE TRIGGER IF NOT EXISTS rel_ad AFTER DELETE ON releases BEGIN
  INSERT INTO releases_fts(releases_fts, rowid, release_key, poster) VALUES('delete', old.id, old.release_key, old.poster);
END;
CREATE TRIGGER IF NOT EXISTS rel_au AFTER UPDATE OF release_key, poster ON releases BEGIN
  INSERT INTO releases_fts(releases_fts, rowid, release_key, poster) VALUES('delete', old.id, old.release_key, old.poster);
  INSERT INTO releases_fts(rowid, release_key, poster) VALUES (new.id, new.release_key, new.poster);
END;

CREATE TABLE IF NOT EXISTS fetch_log(
  id INTEGER PRIMARY KEY, grp TEXT, mode TEXT, first INTEGER, last INTEGER, requested INTEGER DEFAULT 0,
  received INTEGER DEFAULT 0, inserted INTEGER DEFAULT 0, started INTEGER, finished INTEGER,
  status TEXT, message TEXT DEFAULT '', compressed INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_fetchlog_grp ON fetch_log(grp, id);

CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, name TEXT, category TEXT DEFAULT '', status TEXT DEFAULT 'queued',
  priority INTEGER DEFAULT 0, total_bytes INTEGER DEFAULT 0, done_bytes INTEGER DEFAULT 0,
  missing_segments INTEGER DEFAULT 0, total_segments INTEGER DEFAULT 0,
  password TEXT DEFAULT '', extract INTEGER DEFAULT 1, repair INTEGER DEFAULT 1, delete_archives INTEGER DEFAULT 1,
  nzb_path TEXT, work_dir TEXT DEFAULT '', out_dir TEXT DEFAULT '', message TEXT DEFAULT '',
  created INTEGER, started INTEGER, finished INTEGER);
"""

# Version 1.0.x kept a full-text index on every article; it made header downloads slow and is replaced
# by the much smaller release index.
MIGRATIONS = """
DROP TRIGGER IF EXISTS art_ai;
DROP TRIGGER IF EXISTS art_ad;
DROP TABLE IF EXISTS articles_fts;
DROP INDEX IF EXISTS idx_art_rel;
"""

CHUNK = 400  # max keys per IN (...) list


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.local = threading.local()
        self.wlock = threading.RLock()
        c = self.conn
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(MIGRATIONS)
        c.executescript(SCHEMA)
        self.needs_rebuild = bool(c.execute("SELECT 1 FROM articles LIMIT 1").fetchone()) and \
            not c.execute("SELECT 1 FROM files LIMIT 1").fetchone()

    @property
    def conn(self):
        c = getattr(self.local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, isolation_level=None, timeout=60)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA cache_size=-65536")  # 64 MB page cache per connection
            c.execute("PRAGMA temp_store=MEMORY")
            c.execute("PRAGMA busy_timeout=60000")
            self.local.conn = c
        return c

    def q(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def x(self, sql, args=()):
        with self.wlock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    def many(self, sql, rows):
        with self.wlock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                c.executemany(sql, rows)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
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

    # ---- headers ----
    def ingest(self, grp, rows):
        """Insert article rows and refresh the files/releases they belong to, in one transaction.
        rows: (grp, num, subject, poster, date, msgid, parent, bytes, lines, file_key, release_key, part, total, filename)
        Returns the number of new articles."""
        file_keys = list({r[9] for r in rows})
        with self.wlock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                before = c.total_changes
                c.executemany(
                    "INSERT OR IGNORE INTO articles(grp, num, subject, poster, date, msgid, parent, bytes, lines, "
                    "file_key, release_key, part, total, filename) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                inserted = c.total_changes - before
                if inserted:
                    self._refresh(c, grp, file_keys)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return inserted

    def _refresh(self, c, grp, file_keys):
        rel_keys = set()
        for i in range(0, len(file_keys), CHUNK):
            ks = file_keys[i:i + CHUNK]
            ph = ",".join("?" * len(ks))
            c.execute(
                f"INSERT OR REPLACE INTO files(grp, file_key, release_key, subject, filename, poster, date, size, "
                f"parts, total) SELECT grp, file_key, MAX(release_key), MIN(subject), MAX(filename), MAX(poster), "
                f"MAX(date), SUM(bytes), COUNT(*), MAX(total) FROM articles WHERE grp=? AND file_key IN ({ph}) "
                f"GROUP BY file_key", [grp] + ks)
            rel_keys.update(r[0] for r in c.execute(
                f"SELECT DISTINCT release_key FROM files WHERE grp=? AND file_key IN ({ph})", [grp] + ks))
        self._refresh_releases(c, grp, list(rel_keys))

    def _refresh_releases(self, c, grp, rel_keys):
        for i in range(0, len(rel_keys), CHUNK):
            ks = rel_keys[i:i + CHUNK]
            ph = ",".join("?" * len(ks))
            c.execute(
                f"INSERT INTO releases(grp, release_key, subject, poster, date, size, parts, expected, files, nfo, par2) "
                f"SELECT grp, release_key, MIN(subject), MAX(poster), MAX(date), SUM(size), SUM(parts), SUM(total), "
                f"COUNT(*), MAX(CASE WHEN filename LIKE '%.nfo' THEN file_key END), "
                f"SUM(CASE WHEN filename LIKE '%.par2' THEN 1 ELSE 0 END) "
                f"FROM files WHERE grp=? AND release_key IN ({ph}) GROUP BY release_key "
                f"ON CONFLICT(grp, release_key) DO UPDATE SET subject=excluded.subject, poster=excluded.poster, "
                f"date=excluded.date, size=excluded.size, parts=excluded.parts, expected=excluded.expected, "
                f"files=excluded.files, nfo=excluded.nfo, par2=excluded.par2", [grp] + ks)
            # releases whose files are all gone
            c.execute(f"DELETE FROM releases WHERE grp=? AND release_key IN ({ph}) AND NOT EXISTS "
                      f"(SELECT 1 FROM files f WHERE f.grp=releases.grp AND f.release_key=releases.release_key)",
                      [grp] + ks)

    def rebuild(self, grp=None):
        """Recompute files and releases from the stored articles (after an upgrade or retention cleanup)."""
        groups = [grp] if grp else [r["grp"] for r in self.q("SELECT DISTINCT grp FROM articles")]
        for g in groups:
            with self.wlock:
                c = self.conn
                c.execute("BEGIN IMMEDIATE")
                try:
                    read = {r[0]: r[1] for r in c.execute(
                        "SELECT release_key, is_read FROM releases WHERE grp=?", (g,))}
                    c.execute("DELETE FROM files WHERE grp=?", (g,))
                    c.execute("DELETE FROM releases WHERE grp=?", (g,))
                    c.execute(
                        "INSERT INTO files(grp, file_key, release_key, subject, filename, poster, date, size, parts, "
                        "total) SELECT grp, file_key, MAX(release_key), MIN(subject), MAX(filename), MAX(poster), "
                        "MAX(date), SUM(bytes), COUNT(*), MAX(total) FROM articles WHERE grp=? GROUP BY file_key", (g,))
                    keys = [r[0] for r in c.execute("SELECT DISTINCT release_key FROM files WHERE grp=?", (g,))]
                    self._refresh_releases(c, g, keys)
                    c.executemany("UPDATE releases SET is_read=? WHERE grp=? AND release_key=?",
                                  [(v, g, k) for k, v in read.items() if v])
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
        self.needs_rebuild = False
