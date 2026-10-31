"""Real par2 / rar / 7z round trips. Skipped when the tools are not installed (they are in the Docker image)."""
import asyncio
import os
import shutil
import subprocess

import httpx
import pytest

from tests.fake_nntp import FakeServer
from tests.test_e2e import wait_job, wait_tasks

need = pytest.mark.skipif(not all(shutil.which(t) for t in ("par2", "unrar", "rar", "7z")),
                          reason="par2/unrar/rar/7z not installed")


async def setup_app(tmp_path, srv):
    from app import main as m
    m.init(str(tmp_path / "config"))
    m.S.settings.update({
        "servers": [{"name": "fake", "host": "127.0.0.1", "port": srv.port, "ssl": False, "username": "u",
                     "password": "p", "connections": 6}],
        "downloads": {"complete_dir": str(tmp_path / "complete"), "incomplete_dir": str(tmp_path / "inc"),
                      "watch_dir": str(tmp_path / "watch")},
    })
    return m


def post_dir(srv, group, d, release):
    files = sorted(os.listdir(d))
    for i, f in enumerate(files, 1):
        with open(os.path.join(d, f), "rb") as fh:
            srv.add_binary(group, f, fh.read(), part_size=20000, prefix=f"{release} [{i:02d}/{len(files):02d}]")


@need
async def test_rar_par2_repair_and_extract(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    payload = os.urandom(300_000)
    (src / "video.mkv").write_bytes(payload)
    rel = tmp_path / "rel"
    rel.mkdir()
    subprocess.run(["rar", "a", "-m0", "-v100k", "-idq", str(rel / "Test.Release.rar"), "video.mkv"],
                   cwd=src, check=True)
    subprocess.run(["par2", "c", "-q", "-r15", "Test.Release.par2"] + sorted(os.listdir(rel)), cwd=rel, check=True)
    srv = await FakeServer().start()
    post_dir(srv, "alt.binaries.test", rel, "Test.Release")
    # Lose two articles from a rar volume: par2 has to repair it.
    rar_ids = [h["Message-ID"] for _, h, _ in srv.groups["alt.binaries.test"] if "part2.rar" in h["Subject"]]
    srv.missing.update(rar_ids[:2])
    m = await setup_app(tmp_path, srv)
    async with m.app.router.lifespan_context(m.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url="http://t") as c:
            await c.post("/api/groups/subscribe", json={"names": ["alt.binaries.test"], "fetch": True})
            await wait_tasks(c)
            rels = (await c.get("/api/browse", params={"group": "alt.binaries.test"})).json()
            assert rels["total"] == 1, [i["name"] for i in rels["items"]]
            it = rels["items"][0]
            r = (await c.post("/api/download", json={"items": [{"kind": "release", "grp": it["grp"],
                                                                  "key": it["k"]}], "name": "Test.Release"})).json()
            job = await wait_job(c, r["jobs"][0], timeout=60)
            assert job["status"] == "completed", job
            assert "Repaired" in job["message"]
            out = os.listdir(job["out_dir"])
            assert out == ["video.mkv"], out
            assert open(os.path.join(job["out_dir"], "video.mkv"), "rb").read() == payload
    await srv.stop()


@need
async def test_password_7z_via_watch_folder(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.txt").write_text("secret contents")
    rel = tmp_path / "rel"
    rel.mkdir()
    subprocess.run(["7z", "a", "-pS3cret", "-mhe=on", str(rel / "Pw.Release.7z"), "doc.txt"], cwd=src, check=True,
                   stdout=subprocess.DEVNULL)
    srv = await FakeServer().start()
    post_dir(srv, "alt.binaries.test", rel, "Pw.Release")
    m = await setup_app(tmp_path, srv)
    from app import nzb
    files = [{"subject": h["Subject"], "poster": h["From"], "date": 0, "groups": ["alt.binaries.test"],
              "segments": [{"number": 1, "bytes": len(b), "msgid": h["Message-ID"]}]}
             for _, h, b in srv.groups["alt.binaries.test"]]
    async with m.app.router.lifespan_context(m.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url="http://t") as c:
            # password in the file name, category from the sub folder
            os.makedirs(tmp_path / "watch" / "books")
            p = tmp_path / "watch" / "books" / "Pw.Release{{S3cret}}.nzb"
            p.write_bytes(nzb.generate(files))
            os.utime(p, (0, 0))
            for _ in range(300):
                if (await c.get("/api/history")).json():
                    break
                await asyncio.sleep(0.1)
            job = (await c.get("/api/history")).json()[0]
            assert job["status"] == "completed", job
            assert job["name"] == "Pw.Release" and job["category"] == "books"
            assert open(os.path.join(job["out_dir"], "doc.txt")).read() == "secret contents"
            assert not os.path.exists(p)
    await srv.stop()
