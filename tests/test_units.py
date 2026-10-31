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
