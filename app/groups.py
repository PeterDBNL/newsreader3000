"""Group list, header (overview) fetching, and querying of the local header index."""
import asyncio
import logging
import re
import time

from .subjects import parse_subject

log = logging.getLogger("groups")


class GroupService:
    def __init__(self, db, settings, pools, tasks):
        self.db, self.settings, self.pools, self.tasks = db, settings, pools, tasks

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
                "SELECT grp, COUNT(*) n, SUM(is_read=0) unread FROM articles GROUP BY grp")}
            for r in rows:
                s = stats.get(r["name"], {})
                r["stored"] = s.get("n", 0)
                r["unread"] = s.get("unread", 0) or 0
        return {"total": total, "items": rows}

    def subscribe(self, names, on=True):
        self.db.subscribe(names, on)

    def delete_headers(self, group):
        self.db.x("DELETE FROM articles WHERE grp=?", (group,))
        self.db.x("UPDATE groups SET first_fetched=0, last_fetched=0 WHERE name=?", (group,))

    def mark_read(self, group, read=True, ids=None):
        if ids:
            self.db.many("UPDATE articles SET is_read=? WHERE id=?", [(1 if read else 0, i) for i in ids])
        else:
            self.db.x("UPDATE articles SET is_read=? WHERE grp=?", (1 if read else 0, group))

    # ---------- headers ----------
    def fetch_headers(self, group, mode="new", count=None):
        label = f"Headers: {group}" + (" (older)" if mode == "older" else "")

        async def job(t):
            await self._fetch(group, mode, count, t)
        return self.tasks.start("headers", label, job)

    async def _fetch(self, group, mode, count, t):
        cfg = self.settings.get("headers")
        count = int(count or cfg.get("initial_fetch", 20000))
        batch = max(100, int(cfg.get("batch_size", 5000)))
        pool = self.pools.reader()
        info = await pool.run(lambda c: c.group(group))
        first, last = info["first"], info["last"]
        g = self.db.one("SELECT * FROM groups WHERE name=?", (group,)) or {}
        self.db.x("INSERT INTO groups(name, low, high) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET low=?, high=?",
                  (group, first, last, first, last))
        lf, ff = g.get("last_fetched") or 0, g.get("first_fetched") or 0
        if mode == "older" and ff:
            end = ff - 1
            start = max(first, end - count + 1)
        elif mode == "reset" or not lf:
            start, end = max(first, last - count + 1), last
            if mode == "reset":
                self.delete_headers(group)
            ff = 0
        else:
            start, end = max(lf + 1, first, last - max(count, 1) * 5 + 1), last
        if end < start or last == 0:
            t["message"] = "Up to date"
            return
        ranges = [(a, min(a + batch - 1, end)) for a in range(start, end + 1, batch)]
        ranges.reverse()  # newest first so recent posts appear quickly
        t["total"] = end - start + 1
        t["message"] = f"Fetching {t['total']:,} headers"
        q = asyncio.Queue()
        for r in ranges:
            q.put_nowait(r)
        state = {"inserted": 0}

        async def worker():
            while not q.empty():
                a, b = q.get_nowait()

                async def get(c):
                    if c.current_group != group:
                        await c.group(group)
                    return await c.over(a, b)
                items = await pool.run(get)
                rows = []
                for it in items:
                    fk, rk, part, total, fname = parse_subject(it["subject"])
                    rows.append((group, it["num"], it["subject"], it["poster"], it["date"], it["msgid"],
                                 it["parent"], it["bytes"], it["lines"], fk, rk, part, total, fname))
                if rows:
                    self.db.insert_articles(rows)
                state["inserted"] += len(rows)
                t["progress"] += b - a + 1
        workers = max(1, min(pool.max, len(ranges)))
        await asyncio.gather(*[worker() for _ in range(workers)])
        new_first = start if (not ff or start < ff) else ff
        self.db.x("UPDATE groups SET first_fetched=?, last_fetched=MAX(last_fetched, ?), updated_at=? WHERE name=?",
                  (new_first, end, int(time.time()), group))
        t["message"] = f"{state['inserted']:,} new headers"

    def update_subscribed(self):
        groups = [r["name"] for r in self.db.q("SELECT name FROM groups WHERE subscribed=1 ORDER BY name")]
        for g in groups:
            self.fetch_headers(g, "new")
        return len(groups)

    def prune(self):
        days = int(self.settings.get("headers").get("retention_days") or 0)
        if days > 0:
            cutoff = int(time.time()) - days * 86400
            self.db.x("DELETE FROM articles WHERE date < ?", (cutoff,))

    # ---------- querying ----------
    @staticmethod
    def fts_query(q):
        """Turn user input into a safe FTS5 query. Supports -exclude and "exact phrase"."""
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
        where, args = [], []
        join = ""
        fq = self.fts_query(q)
        if q and not fq:
            return {"total": 0, "items": []}
        if fq:
            join = "JOIN articles_fts f ON f.rowid = a.id"
            where.append("articles_fts MATCH ?")
            args.append(fq)
        if groups:
            where.append("a.grp IN (%s)" % ",".join("?" * len(groups)))
            args += list(groups)
        if max_age_days:
            where.append("a.date >= ?")
            args.append(int(time.time() - float(max_age_days) * 86400))
        if poster:
            where.append("a.poster LIKE ?")
            args.append(f"%{poster}%")
        if hide_read:
            where.append("a.is_read=0")
        w = (" WHERE " + " AND ".join(where)) if where else ""
        having = []
        hargs = []
        if min_size:
            having.append("size >= ?")
            hargs.append(int(float(min_size) * 1024 * 1024))
        if max_size:
            having.append("size <= ?")
            hargs.append(int(float(max_size) * 1024 * 1024))
        h = (" HAVING " + " AND ".join(having)) if having else ""
        order = {"date": "date DESC", "size": "size DESC", "name": "subject COLLATE NOCASE",
                 "oldest": "date ASC"}.get(sort, "date DESC")

        if mode == "articles":
            if having:
                w += (" AND " if w else " WHERE ") + " AND ".join(x.replace("size", "a.bytes") for x in having)
                args += hargs
            sql_base = f"FROM articles a {join}{w}"
            total = self.db.one(f"SELECT COUNT(*) n {sql_base}", args)["n"]
            items = self.db.q(
                f"SELECT a.id, a.grp, a.num, a.subject, a.poster, a.date, a.msgid, a.parent, a.bytes size, "
                f"a.lines, a.is_read, a.part, a.total {sql_base} ORDER BY {order.replace('size', 'a.bytes')} "
                f"LIMIT ? OFFSET ?", args + [limit, offset])
            return {"total": total, "items": items}

        key = "file_key" if mode == "files" else "release_key"
        inner = (f"SELECT a.grp, a.{key} k, MIN(a.subject) subject, MAX(a.poster) poster, MAX(a.date) date, "
                 f"SUM(a.bytes) size, COUNT(*) parts, COUNT(DISTINCT a.file_key) files, "
                 f"SUM(CASE WHEN a.part=1 THEN a.total ELSE 0 END) expected, MIN(a.is_read) is_read, "
                 f"MAX(CASE WHEN a.filename LIKE '%.nfo' THEN a.file_key END) nfo, "
                 f"SUM(CASE WHEN a.filename LIKE '%.par2' THEN 1 ELSE 0 END) par2 "
                 f"FROM articles a {join}{w} GROUP BY a.grp, a.{key}{h}")
        total = self.db.one(f"SELECT COUNT(*) n FROM ({inner})", args + hargs)["n"]
        items = self.db.q(f"{inner} ORDER BY {order} LIMIT ? OFFSET ?", args + hargs + [limit, offset])
        for it in items:
            it["name"] = it["k"]
            it["complete"] = round(min(100.0, 100.0 * it["parts"] / it["expected"]), 1) if it["expected"] else None
        return {"total": total, "items": items}

    def collect(self, selections):
        """selections: [{grp, kind: release|file|article, key?, id?}] -> NZB-style file list."""
        rows = []
        for s in selections:
            kind = s.get("kind")
            if kind == "article":
                rows += self.db.q("SELECT * FROM articles WHERE id=?", (int(s["id"]),))
            elif kind in ("file", "release"):
                col = "file_key" if kind == "file" else "release_key"
                rows += self.db.q(f"SELECT * FROM articles WHERE grp=? AND {col}=?", (s["grp"], s["key"]))
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
