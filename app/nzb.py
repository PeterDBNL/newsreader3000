"""Parse and generate NZB files."""
import html
import re
import xml.etree.ElementTree as ET

NS = "http://www.newzbin.com/DTD/2003/nzb"


def parse(data: bytes):
    data = re.sub(rb"<!DOCTYPE[^>]*>", b"", data, count=1)
    root = ET.fromstring(data)

    def tag(el):
        return el.tag.split("}", 1)[-1]

    meta, files = {}, []
    for el in root:
        if tag(el) == "head":
            for m in el:
                if tag(m) == "meta" and m.get("type"):
                    meta[m.get("type").lower()] = (m.text or "").strip()
        elif tag(el) == "file":
            f = {"subject": el.get("subject", ""), "poster": el.get("poster", ""),
                 "date": int(el.get("date", "0") or 0), "groups": [], "segments": []}
            for sub in el:
                if tag(sub) == "groups":
                    f["groups"] = [(g.text or "").strip() for g in sub if (g.text or "").strip()]
                elif tag(sub) == "segments":
                    for s in sub:
                        mid = (s.text or "").strip()
                        if mid:
                            f["segments"].append({"number": int(s.get("number", "0") or 0),
                                                  "bytes": int(s.get("bytes", "0") or 0),
                                                  "msgid": mid.strip("<>")})
            f["segments"].sort(key=lambda s: s["number"])
            # drop duplicate segment numbers
            seen, segs = set(), []
            for s in f["segments"]:
                if s["number"] not in seen:
                    seen.add(s["number"])
                    segs.append(s)
            f["segments"] = segs
            if segs:
                files.append(f)
    if not files:
        raise ValueError("NZB contains no files")
    return {"meta": meta, "files": files}


def generate(files, meta=None):
    """files: list of dict(subject, poster, date, groups, segments=[{number, bytes, msgid}])"""
    e = lambda s: html.escape(str(s), quote=True)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">',
           f'<nzb xmlns="{NS}">']
    if meta:
        out.append("  <head>")
        for k, v in meta.items():
            if v:
                out.append(f'    <meta type="{e(k)}">{e(v)}</meta>')
        out.append("  </head>")
    for f in files:
        out.append(f'  <file poster="{e(f.get("poster", ""))}" date="{int(f.get("date", 0))}" subject="{e(f["subject"])}">')
        out.append("    <groups>")
        for g in f.get("groups", []):
            out.append(f"      <group>{e(g)}</group>")
        out.append("    </groups>")
        out.append("    <segments>")
        for s in f["segments"]:
            out.append(f'      <segment bytes="{int(s.get("bytes", 0))}" number="{int(s["number"])}">{e(s["msgid"].strip("<>"))}</segment>')
        out.append("    </segments>")
        out.append("  </file>")
    out.append("</nzb>")
    return "\n".join(out).encode("utf-8")


def filename_from_subject(subject):
    m = re.search(r'"([^"]+)"', subject or "")
    if m:
        return m.group(1)
    m = re.search(r"(\S+\.[A-Za-z0-9]{2,5})(?:\s|$)", subject or "")
    return m.group(1) if m else ""
