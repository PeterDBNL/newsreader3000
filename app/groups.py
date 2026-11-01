"""Group list, header (overview) fetching, and querying of the local header index."""
import asyncio
import logging
import re
import time

from .nntp import parse_over_data

from .subjects import file_counter, is_binary_subject, is_obfuscated, obfuscated_prefix, parse_subject

log = logging.getLogger("groups")


class GroupService:
    # Files of an obfuscated post (random file names) are bundled when the same poster posted the same
    # number of files within this many seconds of each other.
    OBF_WINDOW = 3 * 3600

    def __init__(self, db, settings, pools, tasks):
        self.db, self.settings, self.pools, self.tasks = db, settings, pools, tasks
        self._clusters = {}

    # ---------- group list ----------
    def refresh_list(self):
        async def job(t):
            pool = self.pools.reader()
            t["message"] = "Downloading group list (this can take a minute)..."
            rows = await pool.run(lambda c: c.list_active())
            t["total"] = len(rows)
            for i in range(0, len(rows), 20000):
                self.db.upsert_groups(rows[i:i + 20000])
                t["progress"] = min(len(rows), i + 20000)
                await asyncio.sleep(0)
            if self.settings.get("headers").get("fetch_descriptions"):
                t["message"] = "Downloading group descriptions..."
                try:
                    desc = await pool.run(lambda c: c.list_newsgroups())
                    self.db.set_descriptions(desc)
                except Exception as e:
                    log.warning("descriptions failed: %s", e)
            t["message"] = f"{len(rows):,} groups available"
        return self.tasks.start("grouplist", "Refresh group list", job)

    def list_groups(self, q="", subscribed=None, offset=0, limit=100, sort="name"):
        where, args = [], []
        if q:
            # simple wildcard support: alt.binaries.* or plain substring
            if "*" in q or "?" in q:
                where.append("name GLOB ?")
                args.append(q.lower())
            else:
                for word in q.lower().split():
                    where.append("(name LIKE ? OR description LIKE ?)")
                    args += [f"%{word}%", f"%{word}%"]
        if subscribed is not None:
            where.append("subscribed=?")
            args.append(1 if subscribed else 0)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        order = {"name": "name", "size": "(high-low) DESC", "updated": "updated_at DESC"}.get(sort, "name")
        total = self.db.one(f"SELECT COUNT(*) n FROM groups{w}", args)["n"]
        rows = self.db.q(f"SELECT * FROM groups{w} ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset])
        for r in rows:
            r["count"] = max(0, (r["high"] or 0) - (r["low"] or 0) + 1) if r["high"] else 0
        if subscribed:
            stats = {r["grp"]: r for r in self.db.q(
                "SELECT grp, COUNT(*) n, SUM(is_read=0) unread FROM releases GROUP BY grp")}
            for r in rows:
                s = stats.get(r["name"], {})
                r["stored"] = s.get("n", 0)
                r["unread"] = s.get("unread", 0) or 0
        return {"total": total, "items": rows}

    def subscribe(self, names, on=True):
        self.db.subscribe(names, on)

    def delete_headers(self, group):
        for t in ("articles", "files", "releases"):
            self.db.x(f"DELETE FROM {t} WHERE grp=?", (group,))
        self.db.x("UPDATE groups SET first_fetched=0, last_fetched=0 WHERE name=?", (group,))

    def mark_read(self, group, read=True, ids=None, releases=None):
        v = 1 if read else 0
        if ids:
            self.db.many("UPDATE articles SET is_read=? WHERE id=?", [(v, i) for i in ids])
        if releases:
            self.db.many("UPDATE releases SET is_read=? WHERE grp=? AND release_key=?",
                         [(v, r["grp"], r["key"]) for r in releases])
        if group and not ids and not releases:
            self.db.x("UPDATE articles SET is_read=? WHERE grp=?", (v, group))
            self.db.x("UPDATE releases SET is_read=? WHERE grp=?", (v, group))

    def group_info(self, group):
        g = self.db.one("SELECT * FROM groups WHERE name=?", (group,)) or {"name": group}
        st = self.db.one("SELECT COUNT(*) releases, MIN(date) oldest, MAX(date) newest, SUM(is_read=0) unread "
                         "FROM releases WHERE grp=?", (group,))
        g.update(st or {})
        g["files"] = self.db.one("SELECT COUNT(*) n FROM files WHERE grp=?", (group,))["n"]
        g["stored_articles"] = (g.get("last_fetched") or 0) - (g.get("first_fetched") or 0) + 1 \
            if g.get("last_fetched") else 0
        g["available"] = max(0, (g.get("high") or 0) - (g.get("low") or 0) + 1) if g.get("high") else 0
        g["log"] = self.fetch_log(group, 10)
        return g

    def fetch_log(self, group=None, limit=100):
        if group:
            return self.db.q("SELECT * FROM fetch_log WHERE grp=? ORDER BY id DESC LIMIT ?", (group, limit))
        return self.db.q("SELECT * FROM fetch_log ORDER BY id DESC LIMIT ?", (limit,))

    # ---------- headers ----------
    def fetch_headers(self, group, mode="new", count=None):
        label = f"Headers: {group}" + (" (older)" if mode == "older" else "")

        async def job(t):
            await self._fetch(group, mode, count, t)
        return self.tasks.start("headers", label, job)

    def _release_key(self, group, subject, fk, rk, fname, poster, date):
        if not is_binary_subject(subject) or not is_obfuscated(rk, fname):
            return rk
        _, nfiles = file_counter(fk)
        prefix = obfuscated_prefix(poster, nfiles)
        ck = (group, prefix)
        clusters = self._clusters.get(ck)
        if clusters is None:
            clusters = [[r["release_key"], r["lo"], r["hi"]] for r in self.db.q(
                "SELECT release_key, MIN(date) lo, MAX(date) hi FROM files WHERE grp=? AND release_key >= ? "
                "AND release_key < ? GROUP BY release_key", (group, prefix, prefix + "~"))]
            self._clusters[ck] = clusters
            if len(self._clusters) > 20000:
                self._clusters.clear()
                self._clusters[ck] = clusters
        for c in clusters:
            if c[1] - self.OBF_WINDOW <= date <= c[2] + self.OBF_WINDOW:
                c[1], c[2] = min(c[1], date), max(c[2], date)
                return c[0]
        key = f"{prefix}{date}"
        clusters.append([key, date, date])
        return key

    def _ingest(self, group, raw):
        """Parse raw overview data and store it (runs in a worker thread)."""
        items = parse_over_data(raw)
        with self.db.wlock:  # release bundling must see a consistent view across parallel batches
            rows = []
            for it in items:
                fk, rk, part, total, fname = parse_subject(it["subject"])
                rk = self._release_key(group, it["subject"], fk, rk, fname, it["poster"], it["date"])
                rows.append((group, it["num"], it["subject"], it["poster"], it["date"], it["msgid"],
                             it["parent"], it["bytes"], it["lines"], fk, rk, part, total, fname))
            return len(rows), (self.db.ingest(group, rows) if rows else 0)

    def regroup(self, t=None):
        """Recompute release grouping for all stored headers (after the grouping rules improved)."""
        groups = [r["grp"] for r in self.db.q("SELECT DISTINCT grp FROM articles")]
        for gi, g in enumerate(groups):
            if t is not None:
                t["message"] = f"Re-grouping {g} ({gi + 1}/{len(groups)})"
            with self.db.wlock:
                self._clusters = {k: v for k, v in self._clusters.items() if k[0] != g}
                self.db.x("DELETE FROM files WHERE grp=? AND release_key >= ? AND release_key < ?",
                          (g, "Obfuscated post", "Obfuscated post~"))
                last = 0
                while True:
                    rows = self.db.q("SELECT id, subject, poster, date FROM articles WHERE grp=? AND id>? "
                                     "ORDER BY id LIMIT 20000", (g, last))
                    if not rows:
                        break
                    last = rows[-1]["id"]
                    upd = []
                    for r in rows:
                        fk, rk, _, _, fname = parse_subject(r["subject"])
                        upd.append((self._release_key(g, r["subject"], fk, rk, fname, r["poster"], r["date"]), r["id"]))
                    self.db.many("UPDATE articles SET release_key=? WHERE id=?", upd)
                self.db.rebuild(g)
                self._clusters = {k: v for k, v in self._clusters.items() if k[0] != g}
        self.db.set_version()

    async def _fetch(self, group, mode, count, t):
        cfg = self.settings.get("headers")
        count = int(count or cfg.get("initial_fetch", 20000))
        batch = max(100, int(cfg.get("batch_size", 5000)))
        compress = bool(cfg.get("compression", True))
        pool = self.pools.reader()
        started = int(time.time())
        log_id = self.db.x("INSERT INTO fetch_log(grp, mode, started, status) VALUES(?,?,?,?)",
                           (group, mode, started, "running"))
        t["log_id"] = log_id
        state = {"received": 0, "inserted": 0, "compressed": False}

        def finish(status, msg, first=0, last=0, requested=0):
            self.db.x("UPDATE fetch_log SET first=?, last=?, requested=?, received=?, inserted=?, finished=?, "
                      "status=?, message=?, compressed=? WHERE id=?",
                      (first, last, requested, state["received"], state["inserted"], int(time.time()), status, msg,
                       int(state["compressed"]), log_id))
        try:
            info = await pool.run(lambda c: c.group(group))
            first, last = info["first"], info["last"]
            g = self.db.one("SELECT * FROM groups WHERE name=?", (group,)) or {}
            self.db.x("INSERT INTO groups(name, low, high) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
                      "low=excluded.low, high=excluded.high", (group, first, last))
            lf, ff = g.get("last_fetched") or 0, g.get("first_fetched") or 0
            if mode == "older" and ff:
                end = ff - 1
                start = max(first, end - count + 1)
            elif mode == "reset" or not lf:
                if mode == "reset":
                    await asyncio.to_thread(self.delete_headers, group)
                start, end = max(first, last - count + 1), last
                ff = 0
            else:
                start, end = max(lf + 1, first, last - max(count, 1) * 5 + 1), last
            if end < start or last == 0:
                t["message"] = "Up to date"
                finish("done", "Up to date")
                return
            ranges = [(a, min(a + batch - 1, end)) for a in range(start, end + 1, batch)]
            ranges.reverse()  # newest first so recent posts appear quickly
            t["total"] = end - start + 1
            t["message"] = f"Fetching {t['total']:,} headers"
            q = asyncio.Queue()
            for r in ranges:
                q.put_nowait(r)
            t0 = time.time()

            async def worker():
                while not q.empty():
                    a, b = q.get_nowait()

                    async def get(c):
                        if compress:
                            await c.enable_compression()
                        if c.current_group != group:
                            await c.group(group)
                        data = await c.over_raw(a, b)
                        state["compressed"] = state["compressed"] or c.last_compressed
                        return data
                    t["message"] = t["message"] if state["received"] else f"Waiting for server ({end - start + 1:,} headers to fetch)"
                    raw = await pool.run(get)
                    received, inserted = await asyncio.to_thread(self._ingest, group, raw)
                    state["received"] += received
                    state["inserted"] += inserted
                    t["progress"] += b - a + 1
                    rate = state["received"] / max(0.1, time.time() - t0)
                    t["message"] = f"{state['received']:,} headers ({rate:,.0f}/s)" + \
                        (" · compressed" if state["compressed"] else "")
            workers = max(1, min(pool.max, len(ranges)))
            await asyncio.gather(*[worker() for _ in range(workers)])
            new_first = start if (not ff or start < ff) else ff
            self.db.x("UPDATE groups SET first_fetched=?, last_fetched=MAX(last_fetched, ?), updated_at=? "
                      "WHERE name=?", (new_first, end, int(time.time()), group))
            secs = max(1, int(time.time()) - started)
            t["message"] = f"{state['inserted']:,} new headers in {secs}s"
            finish("done", t["message"], start, end, end - start + 1)
        except asyncio.CancelledError:
            finish("cancelled", "Cancelled")
            raise
        except Exception as e:
            finish("error", str(e))
            raise

    def update_subscribed(self):
        groups = [r["name"] for r in self.db.q("SELECT name FROM groups WHERE subscribed=1 ORDER BY name")]
        for g in groups:
            self.fetch_headers(g, "new")
        return len(groups)

    def prune(self):
        days = int(self.settings.get("headers").get("retention_days") or 0)
        if days > 0:
            cutoff = int(time.time()) - days * 86400
            groups = [r["grp"] for r in self.db.q("SELECT DISTINCT grp FROM releases WHERE date < ?", (cutoff,))]
            self.db.x("DELETE FROM articles WHERE date < ?", (cutoff,))
            for g in groups:
                self.db.rebuild(g)

    # ---------- querying ----------
    @staticmethod
    def terms(q):
        """Split a query into (include, exclude) word lists. Supports "exact phrase" and -exclude."""
        inc, exc = [], []
        for tok in re.findall(r'-?"[^"]+"|\S+', q or ""):
            neg = tok.startswith("-") and len(tok) > 1
            tok = tok.lstrip("-").strip('"')
            parts = [tok] if " " in tok else re.findall(r"[\w\-]+", tok)
            for p in parts:
                if p:
                    (exc if neg else inc).append(p)
        return inc, exc

    @classmethod
    def fts_query(cls, q):
        """Turn user input into a safe FTS5 query."""
        tokens = re.findall(r'-?"[^"]+"|\S+', q or "")
        inc, exc = [], []
        for tok in tokens:
            neg = tok.startswith("-") and len(tok) > 1
            tok = tok.lstrip("-").strip('"')
            for part in re.findall(r"[\w\-]+", tok) if not tok.count(" ") else [tok]:
                term = '"' + part.replace('"', '') + '"'
                if part and not neg:
                    term += "*" if not tok.count(" ") else ""
                (exc if neg else inc).append(term)
        if not inc:
            return None
        s = " AND ".join(inc)
        if exc:
            s += " NOT " + " NOT ".join(exc)
        return s

    def query(self, groups=None, q="", mode="releases", offset=0, limit=100, min_size=0, max_size=0,
              max_age_days=0, poster="", hide_read=False, sort="date"):
        if mode == "articles":
            t, size = "articles", "a.bytes"
        elif mode == "files":
            t, size = "files", "a.size"
        else:
            t, size = "releases", "a.size"
        where, args, join = [], [], ""
        if q:
            if mode == "releases":
                fq = self.fts_query(q)
                if not fq:
                    return {"total": 0, "items": []}
                join = "JOIN releases_fts f ON f.rowid = a.id"
                where.append("releases_fts MATCH ?")
                args.append(fq)
            else:
                inc, exc = self.terms(q)
                if not inc:
                    return {"total": 0, "items": []}
                for w in inc:
                    where.append("a.subject LIKE ?")
                    args.append(f"%{w}%")
                for w in exc:
                    where.append("a.subject NOT LIKE ?")
                    args.append(f"%{w}%")
        if groups:
            where.append("a.grp IN (%s)" % ",".join("?" * len(groups)))
            args += list(groups)
        if max_age_days:
            where.append("a.date >= ?")
            args.append(int(time.time() - float(max_age_days) * 86400))
        if poster:
            where.append("a.poster LIKE ?")
            args.append(f"%{poster}%")
        if hide_read and mode != "files":
            where.append("a.is_read=0")
        if min_size:
            where.append(f"{size} >= ?")
            args.append(int(float(min_size) * 1024 * 1024))
        if max_size:
            where.append(f"{size} <= ?")
            args.append(int(float(max_size) * 1024 * 1024))
        w = (" WHERE " + " AND ".join(where)) if where else ""
        order = {"date": "a.date DESC", "size": f"{size} DESC", "name": "a.subject COLLATE NOCASE",
                 "oldest": "a.date ASC"}.get(sort, "a.date DESC")
        base = f"FROM {t} a {join}{w}"
        total = self.db.one(f"SELECT COUNT(*) n {base}", args)["n"]
        if mode == "articles":
            items = self.db.q(
                f"SELECT a.id, a.grp, a.num, a.subject, a.poster, a.date, a.msgid, a.parent, a.bytes size, "
                f"a.lines, a.is_read, a.part, a.total {base} ORDER BY {order} LIMIT ? OFFSET ?",
                args + [limit, offset])
            return {"total": total, "items": items}
        if mode == "files":
            items = self.db.q(
                f"SELECT a.grp, a.file_key k, a.file_key name, a.subject, a.filename, a.poster, a.date, a.size, "
                f"a.parts, a.total expected, 1 files, 0 is_read, NULL nfo, 0 par2 {base} ORDER BY {order} "
                f"LIMIT ? OFFSET ?", args + [limit, offset])
        else:
            items = self.db.q(
                f"SELECT a.id, a.grp, a.release_key k, a.release_key name, a.subject, a.poster, a.date, a.size, "
                f"a.parts, a.expected, a.files, a.is_read, a.nfo, a.par2 {base} ORDER BY {order} "
                f"LIMIT ? OFFSET ?", args + [limit, offset])
        for it in items:
            it["complete"] = round(min(100.0, 100.0 * it["parts"] / it["expected"]), 1) if it["expected"] else None
        return {"total": total, "items": items}

    def release_files(self, group, key, kind="release"):
        if kind == "release":
            return self.db.q("SELECT file_key k, subject, filename, size, parts, total expected, date FROM files "
                             "WHERE grp=? AND release_key=? ORDER BY filename, subject", (group, key))
        return self.db.q("SELECT file_key k, subject, filename, size, parts, total expected, date FROM files "
                         "WHERE grp=? AND file_key=?", (group, key))

    def collect(self, selections):
        """selections: [{grp, kind: release|file|article, key?, id?}] -> NZB-style file list."""
        rows = []
        for s in selections:
            kind = s.get("kind")
            if kind == "article":
                rows += self.db.q("SELECT * FROM articles WHERE id=?", (int(s["id"]),))
            elif kind == "file":
                rows += self.db.q("SELECT * FROM articles WHERE grp=? AND file_key=?", (s["grp"], s["key"]))
            elif kind == "release":
                fks = [r["file_key"] for r in self.db.q(
                    "SELECT file_key FROM files WHERE grp=? AND release_key=?", (s["grp"], s["key"]))]
                for i in range(0, len(fks), 400):
                    ks = fks[i:i + 400]
                    rows += self.db.q(f"SELECT * FROM articles WHERE grp=? AND file_key IN "
                                      f"({','.join('?' * len(ks))})", [s["grp"]] + ks)
        files = {}
        seen = set()
        for r in rows:
            if r["msgid"] in seen:
                continue
            seen.add(r["msgid"])
            fkey = (r["file_key"] if (r["total"] or 1) > 1 or r["part"] != 1 else r["msgid"])
            f = files.setdefault(fkey, {"subject": r["subject"], "poster": r["poster"], "date": r["date"],
                                         "groups": set(), "segments": {}, "filename": r["filename"]})
            f["groups"].add(r["grp"])
            if r["part"] == 1:
                f["subject"] = r["subject"]
            f["segments"].setdefault(r["part"] or 1, {"number": r["part"] or 1, "bytes": r["bytes"],
                                                      "msgid": r["msgid"].strip("<>")})
        out = []
        for f in files.values():
            f["groups"] = sorted(f["groups"])
            f["segments"] = [f["segments"][k] for k in sorted(f["segments"])]
            out.append(f)
        out.sort(key=lambda f: f["subject"])
        return out
