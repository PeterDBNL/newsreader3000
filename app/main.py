"""FastAPI app: REST API + static single-page UI."""
import asyncio
import base64
import contextlib
import logging
import mimetypes
import os
import secrets
import shutil
import time

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import nzb as nzblib
from . import postprocess as pp
from .config import CONFIG_DIR, Settings, normalize_server
from .db import DB
from .downloader import Downloader
from .groups import GroupService
from .nntp import CONN_ERRORS, NNTPConnection, NNTPError, Pools
from .reader import Reader
from .tasks import Tasks

VERSION = "1.2.1"
logging.basicConfig(level=os.environ.get("NR_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = os.path.join(os.path.dirname(__file__), "static")


class State:
    pass


S = State()


def init(config_dir=None):
    config_dir = config_dir or CONFIG_DIR
    S.settings = Settings(config_dir)
    S.db = DB(os.path.join(config_dir, "newsreader.db"))
    S.pools = Pools(S.settings)
    S.tasks = Tasks()
    S.groups = GroupService(S.db, S.settings, S.pools, S.tasks)
    S.reader = Reader(S.db, S.settings, S.pools)
    if S.settings.first_run:
        S.db.subscribe(S.settings.data["default_groups"], True)
    for key in ("complete_dir", "incomplete_dir", "watch_dir"):
        p = S.settings.get("downloads").get(key)
        if p:
            with contextlib.suppress(OSError):
                os.makedirs(p, exist_ok=True)


async def scheduler():
    last_update = time.time()
    while True:
        await asyncio.sleep(30)
        try:
            mins = int(S.settings.get("headers").get("auto_update_minutes") or 0)
            if mins > 0 and time.time() - last_update > mins * 60 and S.settings.servers():
                last_update = time.time()
                S.groups.update_subscribed()
                await asyncio.to_thread(S.groups.prune)
        except Exception:
            log.exception("scheduler")


@contextlib.asynccontextmanager
async def lifespan(app):
    if not hasattr(S, "settings"):
        init()
    S.downloader = Downloader(S.db, S.settings, S.pools, S.settings.config_dir)
    if S.db.needs_rebuild or S.db.needs_regroup:
        async def upgrade(t):
            t["message"] = "Grouping stored headers into releases (one-time upgrade)..."
            await asyncio.to_thread(S.groups.regroup, t)
            t["message"] = "Done"
        S.tasks.start("upgrade", "Upgrade header index", upgrade)
    S.downloader.start()
    sched = asyncio.get_running_loop().create_task(scheduler())
    yield
    sched.cancel()
    await S.downloader.stop()
    await S.pools.close_all()


app = FastAPI(title="Newsreader3000", version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def auth_and_errors(request: Request, call_next):
    user, pw = S.settings.ui_credentials if hasattr(S, "settings") else ("", "")
    if pw and request.url.path != "/api/health":
        ok = False
        hdr = request.headers.get("authorization", "")
        if hdr.lower().startswith("basic "):
            try:
                u, _, p = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(u, user) and secrets.compare_digest(p, pw)
            except Exception:
                ok = False
        if not ok:
            return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Newsreader3000"'})
    try:
        return await call_next(request)
    except NNTPError as e:
        code = 404 if e.code in (430, 423, 411) else (413 if e.code == 413 else 502)
        return JSONResponse({"detail": f"News server: {e}"}, code)
    except CONN_ERRORS as e:
        return JSONResponse({"detail": f"Cannot reach news server: {e or type(e).__name__}"}, 502)


def _need_server():
    if not S.settings.servers():
        raise HTTPException(400, "No news server configured. Add your provider (e.g. Eweka) under Settings.")


# ------------------------------------------------------------------ general
@app.get("/api/health")
async def health():
    return {"ok": True, "version": VERSION}


@app.get("/api/status")
async def status():
    d = S.settings.get("downloads")
    disk = {}
    for k in ("complete_dir", "incomplete_dir"):
        with contextlib.suppress(OSError):
            u = shutil.disk_usage(d[k])
            disk[k] = {"free": u.free, "total": u.total}
    return {"version": VERSION, "download": S.downloader.status(), "disk": disk,
            "configured": bool(S.settings.servers()), "tools": pp.tools(),
            "tasks": [t for t in S.tasks.list() if t["status"] == "running"]}


@app.get("/api/tasks")
async def tasks():
    return S.tasks.list()


@app.post("/api/tasks/{tid}/cancel")
async def cancel_task(tid: int):
    S.tasks.cancel(tid)
    return {"ok": True}


# ------------------------------------------------------------------ settings
@app.get("/api/settings")
async def get_settings():
    return S.settings.public()


@app.put("/api/settings")
async def put_settings(data: dict = Body(...)):
    S.settings.update(data)
    for key in ("complete_dir", "incomplete_dir", "watch_dir"):
        p = S.settings.get("downloads").get(key)
        if p:
            with contextlib.suppress(OSError):
                os.makedirs(p, exist_ok=True)
    return S.settings.public()


@app.post("/api/servers/test")
async def test_server(data: dict = Body(...)):
    old = {s["id"]: s for s in S.settings.data["servers"]}.get(data.get("id"))
    s = normalize_server(data, old)
    c = NNTPConnection(s["host"], s["port"], s["ssl"], s["username"], s["password"], timeout=20,
                       verify_ssl=s["verify_ssl"])
    t0 = time.time()
    try:
        await c.connect()
        await c.cmd("DATE")
        return {"ok": True, "message": f"Connected and logged in ({int((time.time() - t0) * 1000)} ms)"
                + ("" if c.can_post else ", posting not allowed")}
    except NNTPError as e:
        return {"ok": False, "message": str(e)}
    except CONN_ERRORS as e:
        return {"ok": False, "message": f"Connection failed: {e or type(e).__name__}"}
    finally:
        await c.quit()


# ------------------------------------------------------------------ groups
@app.post("/api/groups/refresh")
async def refresh_groups():
    _need_server()
    t = S.groups.refresh_list()
    return {"task": t["id"]}


@app.get("/api/groups")
async def list_groups(q: str = "", subscribed: int = -1, offset: int = 0, limit: int = 100, sort: str = "name"):
    return await asyncio.to_thread(S.groups.list_groups, q, None if subscribed < 0 else bool(subscribed), offset,
                                   min(limit, 1000), sort)


@app.get("/api/groups/info")
async def group_info(name: str):
    return await asyncio.to_thread(S.groups.group_info, name)


@app.get("/api/headers/log")
async def header_log(group: str = "", limit: int = 100):
    return S.groups.fetch_log(group or None, min(limit, 1000))


@app.post("/api/headers/log/clear")
async def clear_header_log():
    S.db.x("DELETE FROM fetch_log WHERE status != 'running'")
    return {"ok": True}


@app.post("/api/groups/subscribe")
async def subscribe(data: dict = Body(...)):
    names = [n.strip() for n in data.get("names", []) if n.strip()]
    S.groups.subscribe(names, data.get("subscribe", True))
    if data.get("subscribe", True) and data.get("fetch") and S.settings.servers():
        for n in names:
            S.groups.fetch_headers(n)
    return {"ok": True}


@app.post("/api/groups/fetch")
async def fetch_group(data: dict = Body(...)):
    _need_server()
    t = S.groups.fetch_headers(data["name"], data.get("mode", "new"), data.get("count"))
    return {"task": t["id"]}


@app.post("/api/groups/update-all")
async def update_all():
    _need_server()
    return {"groups": S.groups.update_subscribed()}


@app.post("/api/groups/mark-read")
async def mark_read(data: dict = Body(...)):
    await asyncio.to_thread(S.groups.mark_read, data.get("name"), data.get("read", True), data.get("ids"),
                            data.get("releases"))
    return {"ok": True}


@app.post("/api/groups/clear")
async def clear_group(data: dict = Body(...)):
    await asyncio.to_thread(S.groups.delete_headers, data["name"])
    return {"ok": True}


# ------------------------------------------------------------------ browse & search
def _q(group, groups, q, mode, offset, limit, min_size, max_size, max_age, poster, hide_read, sort):
    gl = [group] if group else [g for g in (groups or "").split(",") if g]
    if not gl and not group:
        gl = [r["name"] for r in S.db.q("SELECT name FROM groups WHERE subscribed=1")]
    return S.groups.query(gl, q, mode, offset, min(limit, 500), min_size, max_size, max_age, poster, bool(hide_read), sort)


@app.get("/api/browse")
async def browse(group: str, q: str = "", mode: str = "releases", offset: int = 0, limit: int = 100, min_size: float = 0,
           max_size: float = 0, max_age: float = 0, poster: str = "", hide_read: int = 0, sort: str = "date"):
    return await asyncio.to_thread(_q, group, None, q, mode, offset, limit, min_size, max_size, max_age, poster,
                                   hide_read, sort)


@app.get("/api/search")
async def search(q: str = "", groups: str = "", mode: str = "releases", offset: int = 0, limit: int = 100,
           min_size: float = 0, max_size: float = 0, max_age: float = 0, poster: str = "", sort: str = "date"):
    if not q.strip() and not poster:
        raise HTTPException(400, "Enter something to search for")
    return await asyncio.to_thread(_q, None, groups, q, mode, offset, limit, min_size, max_size, max_age, poster, 0,
                                   sort)


@app.get("/api/release")
async def release_files(group: str, key: str, kind: str = "release"):
    """List the files inside a release (or the parts of a file)."""
    return await asyncio.to_thread(S.groups.release_files, group, key, kind)


@app.get("/api/thread")
async def thread(msgid: str):
    root = S.db.one("SELECT * FROM articles WHERE msgid=?", (msgid,))
    if not root:
        return []
    return S.db.q("SELECT id, grp, subject, poster, date, msgid, parent, bytes size, is_read FROM articles "
                  "WHERE grp=? AND (msgid=? OR parent=? OR msgid=?) ORDER BY date",
                  (root["grp"], msgid, msgid, root["parent"] or ""))


# ------------------------------------------------------------------ article reading
@app.get("/api/article")
async def article(msgid: str):
    _need_server()
    return await S.reader.view(msgid)


@app.get("/api/article/attachment")
async def attachment(msgid: str, index: int = 0, download: int = 0):
    name, data = await S.reader.attachment(msgid, index)
    return _file_response(name, data, download)


@app.get("/api/preview")
async def preview(group: str, key: str, download: int = 0):
    _need_server()
    name, data = await S.reader.preview_file(group, key)
    return _file_response(name, data, download)


def _file_response(name, data, download):
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    low = name.lower()
    if low.endswith((".nfo", ".diz")) and not download:
        # NFO art uses the old DOS code page; convert so box-drawing characters render
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            data = data.decode("cp437", "replace").encode("utf-8")
        mime = "text/plain; charset=utf-8"
    elif low.endswith((".sfv", ".srt", ".txt", ".nzb")):
        mime = "text/plain; charset=utf-8"
    safe = name.encode("ascii", "replace").decode().replace('"', "")
    disp = "attachment" if download else "inline"
    return Response(data, media_type=mime, headers={"Content-Disposition": f'{disp}; filename="{safe}"'})


@app.post("/api/post")
async def post_article(data: dict = Body(...)):
    _need_server()
    groups = [g.strip() for g in data.get("groups", "").split(",") if g.strip()] if isinstance(
        data.get("groups"), str) else data.get("groups", [])
    if not groups or not data.get("subject"):
        raise HTTPException(400, "Group and subject are required")
    msg = await S.reader.post(groups, data["subject"], data.get("body", ""), data.get("references", ""))
    return {"ok": True, "message": msg}


# ------------------------------------------------------------------ NZB & downloads
@app.post("/api/nzb")
async def make_nzb(data: dict = Body(...)):
    files = await asyncio.to_thread(S.groups.collect, data.get("items", []))
    if not files:
        raise HTTPException(400, "Nothing selected")
    name = data.get("name") or "selection"
    return Response(nzblib.generate(files, {"name": name}), media_type="application/x-nzb",
                    headers={"Content-Disposition": f'attachment; filename="{pp.safe_name(name)}.nzb"'})


@app.post("/api/download")
async def download_selection(data: dict = Body(...)):
    items = data.get("items", [])
    if not items:
        raise HTTPException(400, "Nothing selected")
    ids = []
    if data.get("separate", True):
        groups = [[i] for i in items]
    else:
        groups = [items]
    for g in groups:
        files = await asyncio.to_thread(S.groups.collect, g)
        if not files:
            continue
        name = data.get("name") if len(groups) == 1 and data.get("name") else (g[0].get("name") or files[0]["subject"])
        ids.append(S.downloader.add_nzb(nzblib.generate(files, {"name": name}), name, data.get("category", ""),
                                        data.get("password", ""), data.get("priority", 0), data.get("extract"),
                                        data.get("repair")))
    return {"jobs": ids}


@app.post("/api/queue/upload")
async def upload_nzb(files: list[UploadFile] = File(...), category: str = Form(""), password: str = Form(""),
                     priority: int = Form(0), extract: str = Form("")):
    ids, errors = [], []
    ex = None if extract == "" else extract in ("1", "true", "on")
    for f in files:
        try:
            ids.append(S.downloader.add_nzb(await f.read(), f.filename, category, password, priority, ex))
        except Exception as e:
            errors.append(f"{f.filename}: {e}")
    if errors and not ids:
        raise HTTPException(400, "; ".join(errors))
    return {"jobs": ids, "errors": errors}


@app.post("/api/queue/url")
async def add_url(data: dict = Body(...)):
    try:
        jid = await S.downloader.add_url(data["url"], data.get("category", ""), data.get("name", ""),
                                         data.get("password", ""))
    except ValueError as e:
        raise HTTPException(400, f"Not a valid NZB: {e}")
    except OSError as e:
        raise HTTPException(400, f"Could not fetch URL: {e}")
    return {"jobs": [jid]}


@app.get("/api/queue")
async def queue():
    return {"status": S.downloader.status(), "items": S.downloader.queue()}


@app.post("/api/queue/pause")
async def pause_all():
    S.downloader.paused = True
    return S.downloader.status()


@app.post("/api/queue/resume")
async def resume_all():
    S.downloader.paused = False
    S.downloader.wake.set()
    return S.downloader.status()


@app.post("/api/queue/speed")
async def set_speed(data: dict = Body(...)):
    S.settings.update({"downloads": {"speed_limit_kbps": int(data.get("kbps", 0))}})
    return S.downloader.status()


@app.post("/api/queue/{jid}/{action}")
async def job_action(jid: int, action: str, data: dict = Body(default={})):
    d = S.downloader
    if action == "pause":
        d.pause_job(jid)
    elif action == "resume":
        d.resume_job(jid)
    elif action == "delete":
        d.delete_job(jid, data.get("delete_files", True))
    elif action == "retry":
        d.retry(jid, data.get("password"))
    elif action == "priority":
        d.set_priority(jid, data.get("priority", 0))
    elif action == "update":
        d.update_job(jid, data)
    else:
        raise HTTPException(404, "Unknown action")
    return {"ok": True}


@app.get("/api/queue/{jid}/nzb")
async def job_nzb(jid: int):
    j = S.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not j or not j["nzb_path"] or not os.path.exists(j["nzb_path"]):
        raise HTTPException(404, "NZB not found")
    return FileResponse(j["nzb_path"], filename=f"{pp.safe_name(j['name'])}.nzb", media_type="application/x-nzb")


@app.get("/api/history")
async def history(limit: int = 200, offset: int = 0):
    return S.downloader.history(limit, offset)


@app.post("/api/history/clear")
async def clear_history(data: dict = Body(default={})):
    status = data.get("status")
    rows = S.db.q("SELECT * FROM jobs WHERE status IN ('completed','failed')" +
                  (" AND status=?" if status else ""), (status,) if status else ())
    for j in rows:
        S.downloader.delete_job(j["id"], data.get("delete_files", False))
    return {"removed": len(rows)}


# ------------------------------------------------------------------ static UI
@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
