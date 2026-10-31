import asyncio
import os

import httpx
import pytest

from tests.fake_nntp import FakeServer, make_zip


async def wait_tasks(c, timeout=20):
    for _ in range(timeout * 20):
        t = (await c.get("/api/tasks")).json()
        if t and all(x["status"] != "running" for x in t):
            assert all(x["status"] == "done" for x in t), t
            return t
        await asyncio.sleep(0.05)
    raise AssertionError("tasks did not finish")


async def wait_job(c, jid, timeout=30):
    for _ in range(timeout * 20):
        h = [j for j in (await c.get("/api/history")).json() if j["id"] == jid]
        if h:
            return h[0]
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish: %s" % (await c.get("/api/queue")).json())


async def test_full_flow(tmp_path):
    payload = os.urandom(40000)
    srv = FakeServer()
    zipdata = make_zip({"movie.mkv": payload, "readme.txt": b"hello"})
    srv.add_binary("alt.binaries.test", "My.Release.zip", zipdata, prefix="[1/2]")
    srv.add_binary("alt.binaries.test", "My.Release.nfo", b"NFO \xdb\xdb art", prefix="[2/2]")
    root = srv.add("alt.test", "Linux question", b"How do I mount a disk?\r\n.leading dot line")
    srv.add("alt.test", "Re: Linux question", b"> How do I\r\nUse mount.", refs=root)
    await srv.start()

    from app import main as m
    m.init(str(tmp_path / "config"))
    m.S.settings.update({
        "servers": [{"name": "fake", "host": "127.0.0.1", "port": srv.port, "ssl": False, "username": "u",
                     "password": "p", "connections": 4}],
        "downloads": {"complete_dir": str(tmp_path / "dl/complete"), "incomplete_dir": str(tmp_path / "dl/inc"),
                      "watch_dir": ""},
    })
    async with m.app.router.lifespan_context(m.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url="http://t") as c:
            st = (await c.get("/api/settings")).json()
            assert st["servers"][0]["password"] == "********"
            r = await c.post("/api/servers/test", json=st["servers"][0])
            assert r.json()["ok"], r.json()

            assert (await c.post("/api/groups/refresh")).status_code == 200
            await wait_tasks(c)
            g = (await c.get("/api/groups", params={"q": "test"})).json()
            assert g["total"] == 2

            await c.post("/api/groups/subscribe", json={"names": ["alt.binaries.test", "alt.test"], "fetch": True})
            await wait_tasks(c)

            rel = (await c.get("/api/browse", params={"group": "alt.binaries.test"})).json()
            assert rel["total"] == 1, rel
            item = rel["items"][0]
            assert item["name"] == "My.Release" and item["files"] == 2 and item["complete"] == 100

            files = (await c.get("/api/browse", params={"group": "alt.binaries.test", "mode": "files"})).json()
            assert files["total"] == 2

            s = (await c.get("/api/search", params={"q": "linux"})).json()
            assert s["total"] == 2  # both articles in the thread (release mode groups by subject)
            s = (await c.get("/api/search", params={"q": "release", "mode": "files"})).json()
            assert s["total"] == 2

            arts = (await c.get("/api/browse", params={"group": "alt.test", "mode": "articles"})).json()
            first = [a for a in arts["items"] if a["subject"] == "Linux question"][0]
            a = (await c.get("/api/article", params={"msgid": first["msgid"]})).json()
            assert "mount a disk" in a["text"] and ".leading dot" in a["text"]
            th = (await c.get("/api/thread", params={"msgid": first["msgid"]})).json()
            assert len(th) == 2

            nfo_key = item["nfo"]
            pv = await c.get("/api/preview", params={"group": "alt.binaries.test", "key": nfo_key})
            assert pv.status_code == 200 and "NFO" in pv.text and "█" in pv.text

            # download with auto extract
            r = (await c.post("/api/download", json={"items": [{"kind": "release", "grp": "alt.binaries.test",
                                                                  "key": item["k"], "name": "My.Release"}],
                                                       "category": "movies", "extract": True})).json()
            job = await wait_job(c, r["jobs"][0])
            assert job["status"] == "completed", job
            out = job["out_dir"]
            assert out.startswith(str(tmp_path / "dl/complete/movies"))
            assert open(os.path.join(out, "movie.mkv"), "rb").read() == payload
            assert not os.path.exists(os.path.join(out, "My.Release.zip"))
            assert os.path.exists(os.path.join(out, "My.Release.nfo"))

            # NZB export + upload without extraction, one article missing on server
            nzb = await c.post("/api/nzb", json={"items": [{"kind": "release", "grp": "alt.binaries.test",
                                                            "key": item["k"]}], "name": "again"})
            assert b"<segment" in nzb.content
            srv.missing.add(srv.groups["alt.binaries.test"][-1][1]["Message-ID"])
            up = (await c.post("/api/queue/upload", files={"files": ("again.nzb", nzb.content)},
                               data={"extract": "0"})).json()
            job = await wait_job(c, up["jobs"][0])
            assert job["status"] == "completed", job
            assert open(os.path.join(job["out_dir"], "My.Release.zip"), "rb").read() == zipdata
            assert job["missing_segments"] == 1

            # posting
            await c.put("/api/settings", json={"posting": {"from_name": "Me", "from_email": "me@example.com"}})
            r = await c.post("/api/post", json={"groups": "alt.test", "subject": "Hi", "body": ".dot\nhello"})
            assert r.status_code == 200, r.text
            assert b"Subject: Hi" in srv.posted[0] and b"\r\n.dot\r\n" in srv.posted[0]
    await srv.stop()


async def test_ui_auth(tmp_path):
    from app import main as m
    m.init(str(tmp_path / "config2"))
    m.S.settings.update({"ui": {"username": "admin", "password": "secret"}})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url="http://t") as c:
        assert (await c.get("/api/settings")).status_code == 401
        assert (await c.get("/api/settings", auth=("admin", "secret"))).status_code == 200
        assert (await c.get("/api/health")).status_code == 200


async def test_status_endpoint(tmp_path):
    from app import main as m
    m.init(str(tmp_path / "config3"))
    async with m.app.router.lifespan_context(m.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url="http://t") as c:
            s = (await c.get("/api/status")).json()
            assert s["download"]["queue"] == 0 and s["configured"] is False
            assert (await c.get("/api/queue")).status_code == 200
            assert (await c.get("/")).status_code == 200


@pytest.mark.parametrize("compression", [False, True])
async def test_server_without_over_command(tmp_path, compression):
    srv = FakeServer()
    srv.reject_over = True
    srv.compression = compression
    srv.add("alt.binaries.cores", ".starts with a dot", b"x")
    srv.add_binary("alt.binaries.cores", "Some.App.zip", os.urandom(9000))
    await srv.start()
    from app import main as m
    m.init(str(tmp_path / "config4"))
    m.S.settings.update({"servers": [{"name": "x", "host": "127.0.0.1", "port": srv.port, "ssl": False,
                                      "username": "u", "password": "p", "connections": 4}]})
    async with m.app.router.lifespan_context(m.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url="http://t") as c:
            await c.post("/api/groups/subscribe", json={"names": ["alt.binaries.cores"], "fetch": True})
            await wait_tasks(c)
            r = (await c.get("/api/browse", params={"group": "alt.binaries.cores", "mode": "files"})).json()
            assert r["total"] == 2
            assert ".starts with a dot" in [i["subject"] for i in r["items"]]
            log = (await c.get("/api/headers/log", params={"group": "alt.binaries.cores"})).json()
            assert log[0]["status"] == "done" and log[0]["received"] == 4
            assert log[0]["compressed"] == int(compression)
            info = (await c.get("/api/groups/info", params={"name": "alt.binaries.cores"})).json()
            assert info["releases"] == 2 and info["files"] == 2
    await srv.stop()
