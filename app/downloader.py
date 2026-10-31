"""Download queue: fetches NZB segments over all configured servers, decodes yEnc,
assembles files, then runs par2 repair, extraction and moves the result."""
import asyncio
import collections
import json
import logging
import os
import re
import shutil
import time
import urllib.parse
import urllib.request

from . import nzb as nzblib
from . import postprocess as pp
from . import yenc
from .nntp import CONN_ERRORS, NNTPError

log = logging.getLogger("downloader")
ACTIVE = ("downloading",)
POST = ("verifying", "repairing", "extracting", "moving")


class Speed:
    def __init__(self):
        self.samples = collections.deque()

    def add(self, n):
        now = time.time()
        self.samples.append((now, n))
        while self.samples and now - self.samples[0][0] > 5:
            self.samples.popleft()

    @property
    def bps(self):
        if not self.samples:
            return 0
        now = time.time()
        while self.samples and now - self.samples[0][0] > 5:
            self.samples.popleft()
        return int(sum(n for _, n in self.samples) / 5)


class Limiter:
    def __init__(self, settings):
        self.settings = settings
        self.tokens = 0.0
        self.last = time.time()

    async def take(self, n):
        limit = int(self.settings.get("downloads").get("speed_limit_kbps") or 0) * 1024
        if limit <= 0:
            return
        while True:
            now = time.time()
            self.tokens = min(limit, self.tokens + (now - self.last) * limit)
            self.last = now
            if self.tokens >= 0:
                self.tokens -= n
                return
            await asyncio.sleep(min(1.0, -self.tokens / limit))


class Downloader:
    def __init__(self, db, settings, pools, config_dir):
        self.db, self.settings, self.pools = db, settings, pools
        self.nzb_dir = os.path.join(config_dir, "nzb")
        os.makedirs(self.nzb_dir, exist_ok=True)
        self.paused = False
        self.wake = asyncio.Event()
        self.speed = Speed()
        self.limiter = Limiter(settings)
        self.current = None  # job id being downloaded
        self.job_flags = {}
        self.post_lock = asyncio.Lock()
        self.post_tasks = set()
        self.tasks = []
        # Anything interrupted by a restart goes back in the queue.
        self.db.x("UPDATE jobs SET status='queued' WHERE status IN ('downloading','fetching')")
        self.db.x("UPDATE jobs SET status='queued_post' WHERE status IN (%s)" % ",".join("?" * len(POST)), POST)

    # ------------------------------------------------------------ queue management
    def add_nzb(self, data: bytes, name="", category="", password="", priority=0, extract=None, repair=None):
        parsed = nzblib.parse(data)
        meta = parsed["meta"]
        name = name or meta.get("name") or meta.get("title") or "download"
        name = re.sub(r"\.nzb$", "", name, flags=re.I)
        m = re.search(r"\{\{(.+?)\}\}", name)
        if m:
            password = password or m.group(1)
            name = name.replace(m.group(0), "").strip()
        password = password or meta.get("password", "")
        category = category or meta.get("category", "") or self.settings.get("downloads").get("default_category", "")
        d = self.settings.get("downloads")
        total = sum(s["bytes"] for f in parsed["files"] for s in f["segments"])
        segs = sum(len(f["segments"]) for f in parsed["files"])
        jid = self.db.x(
            "INSERT INTO jobs(name, category, status, priority, total_bytes, total_segments, password, extract, repair, "
            "delete_archives, created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (name, category, "queued", int(priority), total, segs, password,
             int(d["auto_extract"] if extract is None else bool(extract)),
             int(d["par2_repair"] if repair is None else bool(repair)), int(d["delete_archives"]), int(time.time())))
        path = os.path.join(self.nzb_dir, f"{jid}.nzb")
        with open(path, "wb") as f:
            f.write(data)
        self.db.x("UPDATE jobs SET nzb_path=? WHERE id=?", (path, jid))
        self.wake.set()
        return jid

    async def add_url(self, url, category="", name="", password=""):
        def fetch():
            req = urllib.request.Request(url, headers={"User-Agent": "UsenetReader/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read(50 * 1024 * 1024)
                cd = r.headers.get("Content-Disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
            fname = urllib.parse.unquote(m.group(1)) if m else os.path.basename(urllib.parse.urlparse(url).path)
            return data, fname
        data, fname = await asyncio.to_thread(fetch)
        return self.add_nzb(data, name or fname, category, password)

    def queue(self):
        rows = self.db.q("SELECT * FROM jobs WHERE status NOT IN ('completed','failed') "
                         "ORDER BY CASE WHEN status IN ('downloading','verifying','repairing','extracting','moving') "
                         "THEN 0 ELSE 1 END, priority DESC, id")
        for r in rows:
            r["password"] = "set" if r["password"] else ""
        return rows

    def history(self, limit=200, offset=0):
        rows = self.db.q("SELECT * FROM jobs WHERE status IN ('completed','failed') ORDER BY finished DESC LIMIT ? OFFSET ?",
                         (limit, offset))
        for r in rows:
            r["password"] = "set" if r["password"] else ""
        return rows

    def set_status(self, jid, status, message=None):
        if message is None:
            self.db.x("UPDATE jobs SET status=? WHERE id=?", (status, jid))
        else:
            self.db.x("UPDATE jobs SET status=?, message=? WHERE id=?", (status, message, jid))

    def pause_job(self, jid):
        j = self.db.one("SELECT status FROM jobs WHERE id=?", (jid,))
        if j and j["status"] in ("queued", "downloading"):
            self.set_status(jid, "paused")
            self.job_flags[jid] = "pause"

    def resume_job(self, jid):
        self.db.x("UPDATE jobs SET status='queued' WHERE id=? AND status='paused'", (jid,))
        self.wake.set()

    def delete_job(self, jid, delete_files=True):
        j = self.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not j:
            return
        self.job_flags[jid] = "delete"
        if j["status"] in POST:
            return  # let post-processing finish; history entry can be removed later
        self.db.x("DELETE FROM jobs WHERE id=?", (jid,))
        if j["status"] != "downloading":
            self._remove_job_files(j, delete_files)

    def _remove_job_files(self, j, delete_files):
        if j.get("nzb_path") and os.path.exists(j["nzb_path"]):
            os.remove(j["nzb_path"])
        if delete_files and j.get("work_dir") and os.path.isdir(j["work_dir"]):
            shutil.rmtree(j["work_dir"], ignore_errors=True)

    def retry(self, jid, password=None):
        j = self.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not j:
            return
        if password:
            self.db.x("UPDATE jobs SET password=? WHERE id=?", (password, jid))
        status = "queued"
        if j["work_dir"] and os.path.isdir(j["work_dir"]):
            status = "queued"  # will resume, completed segments are skipped
        self.db.x("UPDATE jobs SET status=?, message='', finished=NULL WHERE id=?", (status, jid))
        self.wake.set()

    def set_priority(self, jid, priority):
        self.db.x("UPDATE jobs SET priority=? WHERE id=?", (int(priority), jid))

    def update_job(self, jid, fields):
        allowed = {"name", "category", "password", "extract", "repair", "delete_archives", "priority"}
        for k, v in fields.items():
            if k in allowed:
                self.db.x(f"UPDATE jobs SET {k}=? WHERE id=?", (v, jid))

    def status(self):
        q = self.db.one("SELECT COUNT(*) n, COALESCE(SUM(total_bytes-done_bytes),0) remaining FROM jobs "
                        "WHERE status NOT IN ('completed','failed')")
        return {"paused": self.paused, "speed": self.speed.bps, "queue": q["n"], "remaining": q["remaining"],
                "current": self.current,
                "limit_kbps": int(self.settings.get("downloads").get("speed_limit_kbps") or 0)}

    # ------------------------------------------------------------ worker loop
    def start(self):
        self.tasks.append(asyncio.get_running_loop().create_task(self._loop()))
        self.tasks.append(asyncio.get_running_loop().create_task(self._watch_loop()))

    async def stop(self):
        for t in self.tasks + list(self.post_tasks):
            t.cancel()

    async def _loop(self):
        # resume interrupted post-processing first
        for j in self.db.q("SELECT id FROM jobs WHERE status='queued_post'"):
            self._spawn_post(j["id"])
        while True:
            try:
                job = None
                if not self.paused and self.pools.download_pools():
                    job = self.db.one("SELECT * FROM jobs WHERE status='queued' ORDER BY priority DESC, id LIMIT 1")
                if not job:
                    self.wake.clear()
                    try:
                        await asyncio.wait_for(self.wake.wait(), 5)
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._download(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("download loop error")
                if job:
                    self._fail(job["id"], f"Error: {e}")
                await asyncio.sleep(2)

    def _fail(self, jid, msg):
        self.db.x("UPDATE jobs SET status='failed', message=?, finished=? WHERE id=?", (msg, int(time.time()), jid))

    async def _download(self, job):
        jid = job["id"]
        self.job_flags.pop(jid, None)
        with open(job["nzb_path"], "rb") as f:
            parsed = nzblib.parse(f.read())
        dcfg = self.settings.get("downloads")
        work = job["work_dir"] or os.path.join(dcfg["incomplete_dir"], f"{pp.safe_name(job['name'])}.{jid}")
        os.makedirs(work, exist_ok=True)
        self.db.x("UPDATE jobs SET status='downloading', work_dir=?, started=COALESCE(started, ?), message='' WHERE id=?",
                  (work, int(time.time()), jid))
        self.current = jid
        state_path = os.path.join(work, ".state.json")
        state = {"done": {}, "names": {}, "missing": {}}
        if os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    state.update(json.load(f))
            except Exception:
                pass

        files = parsed["files"]
        # Download par2 index files last-ish? No: small files first (nfo, par2 index) helps previews.
        queue = asyncio.Queue()
        done_bytes = 0
        for fi, f in enumerate(files):
            done = set(state["done"].get(str(fi), []))
            for s in f["segments"]:
                if s["number"] in done:
                    done_bytes += s["bytes"]
                else:
                    queue.put_nowait((fi, s))
        self.db.x("UPDATE jobs SET done_bytes=? WHERE id=?", (done_bytes, jid))
        fds = {}
        used_names = set(state["names"].values())
        loose = collections.defaultdict(dict)  # fi -> {number: data} for non-yEnc-part articles
        counters = {"done": done_bytes, "missing": sum(len(v) for v in state["missing"].values()),
                    "last_save": time.time()}
        pools = self.pools.download_pools()
        retries = int(dcfg.get("max_retries", 3))

        def file_name(fi, dec_name):
            if str(fi) in state["names"]:
                return state["names"][str(fi)]
            name = pp.safe_name(dec_name or nzblib.filename_from_subject(files[fi]["subject"]) or f"file{fi:04d}")
            base, n = name, 1
            while name in used_names:
                stem, ext = os.path.splitext(base)
                name = f"{stem}.{n}{ext}"
                n += 1
            used_names.add(name)
            state["names"][str(fi)] = name
            return name

        def get_fd(fi, name):
            if fi not in fds:
                fds[fi] = os.open(os.path.join(work, name), os.O_RDWR | os.O_CREAT, 0o666)
            return fds[fi]

        def save_state():
            tmp = state_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(state, fh)
            os.replace(tmp, state_path)
            self.db.x("UPDATE jobs SET done_bytes=?, missing_segments=? WHERE id=?",
                      (counters["done"], counters["missing"], jid))

        async def fetch(msgid):
            for pool in self.pools.download_pools():
                for attempt in range(retries):
                    try:
                        async with pool.connection() as c:
                            return await c.body(msgid)
                    except NNTPError as e:
                        if e.code in (430, 423, 420):
                            break  # not on this server, try next
                        if e.code in (480, 481, 482, 502):
                            log.warning("%s: auth/permission error %s", pool.cfg["host"], e)
                            break
                        await asyncio.sleep(1)
                    except CONN_ERRORS as e:
                        log.debug("connection error on %s: %s", pool.cfg["host"], e)
                        await asyncio.sleep(1 + attempt * 2)
            return None

        async def worker():
            while True:
                flag = self.job_flags.get(jid)
                if flag or self.paused:
                    return
                try:
                    fi, seg = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                body = await fetch(seg["msgid"])
                if self.job_flags.get(jid) or self.paused:
                    if body is None or self.paused:
                        queue.put_nowait((fi, seg))
                        return
                if body is None:
                    state["missing"].setdefault(str(fi), []).append(seg["number"])
                    counters["missing"] += 1
                else:
                    await self.limiter.take(len(body))
                    self.speed.add(len(body))
                    await self._write(body, fi, seg, file_name, get_fd, loose, len(files[fi]["segments"]))
                state["done"].setdefault(str(fi), []).append(seg["number"])
                counters["done"] += seg["bytes"]
                if time.time() - counters["last_save"] > 3:
                    counters["last_save"] = time.time()
                    save_state()

        nworkers = sum(p.max for p in pools)
        await asyncio.gather(*[worker() for _ in range(max(1, nworkers))])
        for fd in fds.values():
            os.close(fd)
        for fi, parts in loose.items():
            name = file_name(fi, "")
            with open(os.path.join(work, name), "wb") as out:
                for k in sorted(parts):
                    out.write(parts[k])
        save_state()
        self.current = None

        flag = self.job_flags.pop(jid, None)
        if flag == "delete":
            j = self.db.one("SELECT * FROM jobs WHERE id=?", (jid,)) or {"nzb_path": job["nzb_path"], "work_dir": work}
            j["work_dir"] = work
            self.db.x("DELETE FROM jobs WHERE id=?", (jid,))
            self._remove_job_files(j, True)
            return
        if flag == "pause" or self.paused:
            if self.paused:
                self.set_status(jid, "queued")
            return
        if not queue.empty():
            self.set_status(jid, "queued")
            return
        total = job["total_segments"] or 1
        if counters["missing"] >= total:
            self._fail(jid, "All articles are missing from the server(s) (expired or removed)")
            return
        self._spawn_post(jid)

    async def _write(self, body, fi, seg, file_name, get_fd, loose, nseg):
        try:
            dec = yenc.decode(body)
        except yenc.YencError:
            dec = None
        if dec:
            name = file_name(fi, dec["name"])
            fd = get_fd(fi, name)
            offset = dec["begin"] - 1 if dec["multipart"] else 0
            if not dec["multipart"] and nseg > 1:
                loose[fi][seg["number"]] = dec["data"]
                return
            await asyncio.to_thread(os.pwrite, fd, dec["data"], offset) if len(dec["data"]) > 1 << 20 \
                else os.pwrite(fd, dec["data"], offset)
            return
        uu = yenc.decode_uu(body)
        if uu:
            file_name(fi, uu["name"])
            loose[fi][seg["number"]] = uu["data"]
        else:
            file_name(fi, "")
            loose[fi][seg["number"]] = body

    # ------------------------------------------------------------ post-processing
    def _spawn_post(self, jid):
        t = asyncio.get_running_loop().create_task(self._post(jid))
        self.post_tasks.add(t)
        t.add_done_callback(self.post_tasks.discard)

    async def _post(self, jid):
        async with self.post_lock:
            job = self.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
            if not job:
                return
            work = job["work_dir"]
            dcfg = self.settings.get("downloads")
            msgs = []

            def note(m):
                self.db.x("UPDATE jobs SET message=? WHERE id=?", (m, jid))
            try:
                if job["repair"] and (job["missing_segments"] or dcfg.get("always_verify")):
                    self.set_status(jid, "repairing")
                    ok, m = await asyncio.to_thread(pp.par2_repair, work, True, note)
                    msgs.append(m)
                    if ok is False:
                        self._fail(jid, "; ".join(msgs))
                        return
                    if ok is None and job["missing_segments"]:
                        msgs.append(f"{job['missing_segments']} article(s) missing")
                if job["extract"]:
                    self.set_status(jid, "extracting")
                    ok, m = await asyncio.to_thread(pp.extract_all, work, job["password"] or "",
                                                    dcfg.get("nested_extract", True), bool(job["delete_archives"]), note)
                    if ok is False and job["repair"] and job["missing_segments"] == 0 and pp.par2_files(work):
                        # Extraction failed on an apparently complete download: try repair once.
                        self.set_status(jid, "repairing")
                        r_ok, rm = await asyncio.to_thread(pp.par2_repair, work, True, note)
                        if r_ok:
                            msgs.append(rm)
                            self.set_status(jid, "extracting")
                            ok, m = await asyncio.to_thread(pp.extract_all, work, job["password"] or "",
                                                            dcfg.get("nested_extract", True),
                                                            bool(job["delete_archives"]), note)
                    msgs.append(m)
                    if ok is False:
                        self._fail(jid, "; ".join(msgs))
                        return
                self.set_status(jid, "moving")
                await asyncio.to_thread(pp.cleanup, work, dcfg.get("delete_par2", True) and job["repair"])
                dest = await asyncio.to_thread(pp.move_to_complete, work, dcfg["complete_dir"], job["category"],
                                               job["name"])
                self.db.x("UPDATE jobs SET status='completed', out_dir=?, work_dir='', message=?, finished=?, "
                          "done_bytes=total_bytes WHERE id=?",
                          (dest, "; ".join(m for m in msgs if m and m != "No archives found") or "OK",
                           int(time.time()), jid))
            except Exception as e:
                log.exception("post-processing failed")
                self._fail(jid, f"Post-processing error: {e}")

    # ------------------------------------------------------------ watch folder
    async def _watch_loop(self):
        while True:
            await asyncio.sleep(10)
            d = self.settings.get("downloads").get("watch_dir")
            if not d or not os.path.isdir(d):
                continue
            try:
                for root, _, files in os.walk(d):
                    for f in files:
                        if not f.lower().endswith(".nzb"):
                            continue
                        p = os.path.join(root, f)
                        if time.time() - os.path.getmtime(p) < 3:
                            continue  # still being written
                        rel = os.path.relpath(root, d)
                        cat = "" if rel == "." else rel.split(os.sep)[0]
                        try:
                            with open(p, "rb") as fh:
                                self.add_nzb(fh.read(), f, cat)
                            os.remove(p)
                        except Exception as e:
                            log.warning("watch folder: cannot import %s: %s", f, e)
                            os.replace(p, p + ".failed")
            except Exception:
                log.exception("watch folder error")
