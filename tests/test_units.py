import os

from app import nzb, subjects, yenc
from app.groups import GroupService


def test_yenc_roundtrip_with_critical_bytes():
    data = bytes(range(256)) * 50 + b"\x00\r\n=.." + os.urandom(3000)
    enc = yenc.encode(data, "test file.bin", part=2, total=3, begin=101, size=99999)
    d = yenc.decode(b"leading text\r\n" + enc)
    assert d["data"] == data
    assert d["crc_ok"] is True
    assert d["name"] == "test file.bin"
    assert d["begin"] == 101 and d["multipart"]


def test_yenc_single_part():
    d = yenc.decode(yenc.encode(b"hello world", "a.txt"))
    assert d["data"] == b"hello world" and not d["multipart"] and d["crc_ok"]


def test_subject_parsing():
    fk, rk, part, total, fn = subjects.parse_subject('[03/15] - "Some.Release.part02.rar" yEnc (12/137) 98765432')
    assert (part, total, fn) == (12, 137, "Some.Release.part02.rar")
    assert rk == "Some.Release"
    fk2, rk2, *_ = subjects.parse_subject('[04/15] - "Some.Release.part03.rar" yEnc (1/137)')
    assert rk2 == rk and fk2 != fk
    _, rk3, *_ = subjects.parse_subject('[15/15] - "Some.Release.vol07+08.par2" yEnc (1/9)')
    assert rk3 == rk
    assert subjects.parse_subject("Re: Question about linux")[2:4] == (1, 1)


def test_nzb_roundtrip():
    files = [{"subject": 'x "a.rar" yEnc (1/2)', "poster": "p <p@x>", "date": 5, "groups": ["alt.test"],
              "segments": [{"number": 2, "bytes": 10, "msgid": "b@x"}, {"number": 1, "bytes": 10, "msgid": "<a@x>"}]}]
    out = nzb.parse(nzb.generate(files, {"name": "My & Name", "password": "pw"}))
    assert out["meta"] == {"name": "My & Name", "password": "pw"}
    assert [s["msgid"] for s in out["files"][0]["segments"]] == ["a@x", "b@x"]


def test_fts_query_sanitises():
    assert GroupService.fts_query('foo "bar baz" -qux') == '"foo"* AND "bar baz" NOT "qux"'
    assert GroupService.fts_query('-only') is None
    assert GroupService.fts_query('a"b') == '"a"* AND "b"*'


def test_upgrade_from_v1_0_database(tmp_path):
    """A 1.0.x database (article-level FTS, no files/releases tables) is migrated and indexed."""
    import sqlite3
    from app.db import DB
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE articles(id INTEGER PRIMARY KEY, grp TEXT NOT NULL, num INTEGER NOT NULL, subject TEXT, poster TEXT,
      date INTEGER, msgid TEXT, parent TEXT, bytes INTEGER, lines INTEGER, file_key TEXT, release_key TEXT,
      part INTEGER, total INTEGER, filename TEXT, is_read INTEGER DEFAULT 0, UNIQUE(grp, num));
    CREATE VIRTUAL TABLE articles_fts USING fts5(subject, poster, content='articles', content_rowid='id');
    CREATE TRIGGER art_ai AFTER INSERT ON articles BEGIN
      INSERT INTO articles_fts(rowid, subject, poster) VALUES (new.id, new.subject, new.poster); END;
    """)
    for i in range(1, 7):
        subj = f'[1/2] - "Old.Release.part{(i - 1) // 3 + 1}.rar" yEnc ({(i - 1) % 3 + 1}/3)'
        fk, rk, part, total, fn = subjects.parse_subject(subj)
        c.execute("INSERT INTO articles(grp,num,subject,poster,date,msgid,bytes,file_key,release_key,part,total,filename) "
                  "VALUES('g',?,?,?,?,?,?,?,?,?,?,?)", (i, subj, "p", 100, f"<{i}>", 10, fk, rk, part, total, fn))
    c.execute("INSERT INTO articles(grp,num,subject,poster,date,msgid,bytes,file_key,release_key,part,total,filename) "
              "VALUES('g',7,'Hi','p',100,'<7>',10,'Hi','Hi',1,1,'')")
    c.commit()
    c.close()
    d = DB(path)
    assert d.needs_rebuild and d.needs_regroup
    from app.groups import GroupService
    GroupService(d, None, None, None).regroup()
    assert not d.needs_regroup and not DB(path).needs_regroup
    rel = d.q("SELECT release_key, files, parts, expected, size FROM releases ORDER BY release_key")
    assert rel == [{"release_key": "Hi", "files": 1, "parts": 1, "expected": 1, "size": 10},
                   {"release_key": "Old.Release", "files": 2, "parts": 6, "expected": 6, "size": 60}]
    assert not d.q("SELECT name FROM sqlite_master WHERE name='articles_fts'")
    # new inserts work without the old trigger
    d.ingest("g", [("g", 99, "x", "p", 1, "<99>", "", 5, 1, "x", "x", 1, 1, "")])
