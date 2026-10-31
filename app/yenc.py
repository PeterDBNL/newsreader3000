"""yEnc and UUencode decoding of article bodies."""
import binascii
import re
import zlib

_TABLE = bytes((i - 42) & 0xFF for i in range(256))
_ESC_RE = re.compile(rb"=(.)", re.S)
_PARAM_RE = re.compile(rb"(\w+)=(\S+)")


class YencError(Exception):
    pass


def _params(line: bytes):
    out = {}
    name_idx = line.find(b" name=")
    if name_idx != -1:
        out["name"] = line[name_idx + 6:].strip().decode("utf-8", "replace")
        line = line[:name_idx]
    for k, v in _PARAM_RE.findall(line):
        out[k.decode()] = v.decode("latin-1")
    return out


def decode(body: bytes):
    """Decode a yEnc article body.
    Returns dict(name, size, begin, end, part, total, data, crc_ok)."""
    start = body.find(b"=ybegin ")
    if start == -1:
        raise YencError("not yEnc")
    lines = body[start:].split(b"\r\n")
    head = _params(lines[0])
    i = 1
    part = {}
    if i < len(lines) and lines[i].startswith(b"=ypart "):
        part = _params(lines[i])
        i += 1
    data_lines = []
    trailer = {}
    for line in lines[i:]:
        if line.startswith(b"=yend"):
            trailer = _params(line)
            break
        data_lines.append(line)
    raw = b"".join(data_lines)
    if b"=" in raw:
        raw = _ESC_RE.sub(lambda m: bytes(((m.group(1)[0] - 64) & 0xFF,)), raw)
    data = raw.translate(_TABLE)
    crc_ok = None
    crc = trailer.get("pcrc32") or (trailer.get("crc32") if not part else None)
    if crc:
        try:
            crc_ok = (zlib.crc32(data) & 0xFFFFFFFF) == int(crc, 16)
        except ValueError:
            pass
    begin = int(part.get("begin", 1)) if part else 1
    return {
        "name": head.get("name", ""),
        "size": int(head.get("size", len(data)) or 0),
        "part": int(head.get("part", 1) or 1),
        "total": int(head.get("total", 1) or 1),
        "begin": begin,
        "multipart": bool(part),
        "data": data,
        "crc_ok": crc_ok,
    }


def decode_uu(body: bytes):
    lines = body.split(b"\r\n")
    name, out, started = "", bytearray(), False
    for line in lines:
        if not started:
            if line.startswith(b"begin "):
                parts = line.split(None, 2)
                name = parts[2].decode("utf-8", "replace") if len(parts) > 2 else ""
                started = True
            continue
        if line in (b"end", b"`"):
            if line == b"end":
                break
            continue
        try:
            out += binascii.a2b_uu(line)
        except binascii.Error:
            nbytes = (((line[0] - 32) & 63) * 4 + 5) // 3 if line else 0
            out += binascii.a2b_uu(line[:nbytes])
    if not started:
        return None
    return {"name": name, "data": bytes(out)}


def encode(data: bytes, name: str, line_len=128, part=None, total=None, begin=None, size=None):
    """yEnc encoder (used by tests and for posting attachments)."""
    out = bytearray()
    col = 0
    lines = []
    for b in data:
        c = (b + 42) & 0xFF
        if c in (0, 10, 13, 61) or (col == 0 and c in (9, 32, 46)) or (col == line_len - 1 and c in (9, 32)):
            out += bytes((61, (c + 64) & 0xFF))
            col += 2
        else:
            out.append(c)
            col += 1
        if col >= line_len:
            lines.append(bytes(out))
            out = bytearray()
            col = 0
    if out:
        lines.append(bytes(out))
    size = size or len(data)
    crc = "%08x" % (zlib.crc32(data) & 0xFFFFFFFF)
    if part:
        header = f"=ybegin part={part} total={total} line={line_len} size={size} name={name}".encode()
        ypart = f"=ypart begin={begin} end={begin + len(data) - 1}".encode()
        trailer = f"=yend size={len(data)} part={part} pcrc32={crc}".encode()
        return b"\r\n".join([header, ypart] + lines + [trailer])
    header = f"=ybegin line={line_len} size={size} name={name}".encode()
    trailer = f"=yend size={len(data)} crc32={crc}".encode()
    return b"\r\n".join([header] + lines + [trailer])
