"""Reading articles: text, MIME attachments, yEnc binaries and multi-part previews; posting."""
import asyncio
import email
import email.policy
import email.utils
import mimetypes
import time
import uuid

from . import yenc
from .nntp import NNTPError

MAX_PREVIEW = 64 * 1024 * 1024


def _split_article(raw: bytes):
    idx = raw.find(b"\r\n\r\n")
    return (raw, b"") if idx == -1 else (raw[:idx], raw[idx + 4:])


class Reader:
    def __init__(self, db, settings, pools):
        self.db, self.settings, self.pools = db, settings, pools
        self.cache = {}

    async def _article(self, msgid):
        if msgid in self.cache:
            return self.cache[msgid][1]
        pool = self.pools.reader()
        raw = await pool.run(lambda c: c.article(msgid))
        self.cache[msgid] = (time.time(), raw)
        if len(self.cache) > 30:
            oldest = min(self.cache, key=lambda k: self.cache[k][0])
            self.cache.pop(oldest, None)
        return raw

    async def view(self, msgid):
        raw = await self._article(msgid)
        self.db.x("UPDATE articles SET is_read=1 WHERE msgid=?", (msgid if msgid.startswith("<") else f"<{msgid}>",))
        head, body = _split_article(raw)
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        headers = {}
        for k in ("From", "Subject", "Date", "Newsgroups", "Message-ID", "References", "Organization",
                  "X-Newsreader", "User-Agent", "Followup-To", "Reply-To"):
            v = msg.get(k)
            if v:
                headers[k] = str(v)
        out = {"headers": headers, "text": "", "attachments": [], "yenc": None}
        if b"=ybegin " in body:
            try:
                d = yenc.decode(body)
                pre = body[:body.find(b"=ybegin ")].decode("utf-8", "replace").strip()
                out["text"] = pre
                out["yenc"] = {k: d[k] for k in ("name", "size", "part", "total", "begin", "multipart", "crc_ok")}
                out["yenc"]["bytes"] = len(d["data"])
                out["yenc"]["mime"] = mimetypes.guess_type(d["name"])[0] or "application/octet-stream"
            except yenc.YencError:
                out["text"] = body.decode("utf-8", "replace")
            return out
        if b"\r\nbegin " in b"\r\n" + body:
            uu = yenc.decode_uu(body)
            if uu:
                out["attachments"].append({"index": 0, "name": uu["name"], "size": len(uu["data"]),
                                           "mime": mimetypes.guess_type(uu["name"])[0] or "application/octet-stream",
                                           "kind": "uu"})
        texts = []
        idx = 0
        for part in msg.walk():
            if part.is_multipart():
                continue
            fname = part.get_filename()
            ctype = part.get_content_type()
            if fname or not ctype.startswith("text/"):
                payload = part.get_payload(decode=True) or b""
                out["attachments"].append({"index": idx, "name": fname or f"attachment{idx}", "size": len(payload),
                                           "mime": ctype, "kind": "mime"})
                idx += 1
            elif ctype == "text/plain" or not texts:
                try:
                    texts.append(part.get_content())
                except Exception:
                    texts.append((part.get_payload(decode=True) or b"").decode("utf-8", "replace"))
        if out["attachments"] and out["attachments"][0].get("kind") == "uu":
            texts = [body.split(b"\r\nbegin ")[0].decode("utf-8", "replace")]
        out["text"] = "\n".join(texts)
        return out

    async def attachment(self, msgid, index=0):
        raw = await self._article(msgid)
        _, body = _split_article(raw)
        if b"=ybegin " in body:
            d = yenc.decode(body)
            return d["name"], d["data"]
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        i = 0
        for part in msg.walk():
            if part.is_multipart():
                continue
            if part.get_filename() or not part.get_content_type().startswith("text/"):
                if i == index:
                    return part.get_filename() or f"attachment{i}", part.get_payload(decode=True) or b""
                i += 1
        uu = yenc.decode_uu(body)
        if uu:
            return uu["name"], uu["data"]
        raise NNTPError(404, "Attachment not found")

    async def preview_file(self, grp, key, max_bytes=MAX_PREVIEW):
        """Download and decode all parts of one multi-part file (for images, nfo, small videos...)."""
        rows = self.db.q("SELECT msgid, part, bytes, filename FROM articles WHERE grp=? AND file_key=? ORDER BY part",
                         (grp, key))
        if not rows:
            raise NNTPError(404, "File not found in index")
        if sum(r["bytes"] or 0 for r in rows) > max_bytes:
            raise NNTPError(413, "File too large to preview; download it instead")
        pool = self.pools.reader()
        sem = asyncio.Semaphore(pool.max)

        async def get(r):
            async with sem:
                try:
                    return r, await pool.run(lambda c: c.body(r["msgid"]))
                except NNTPError:
                    return r, None
        results = await asyncio.gather(*[get(r) for r in rows])
        chunks, name = [], rows[0]["filename"] or "file"
        size = 0
        for r, body in results:
            if body is None:
                continue
            try:
                d = yenc.decode(body)
                name = d["name"] or name
                size = max(size, d["size"])
                chunks.append((d["begin"] - 1 if d["multipart"] else len(chunks), d["data"]))
            except yenc.YencError:
                chunks.append((r["part"], body))
        if not chunks:
            raise NNTPError(430, "Articles not available on server")
        if size:
            buf = bytearray(size)
            for off, data in chunks:
                buf[off:off + len(data)] = data
            return name, bytes(buf[:size])
        return name, b"".join(c for _, c in sorted(chunks, key=lambda x: x[0]))

    async def post(self, groups, subject, body, references="", from_override=""):
        p = self.settings.get("posting")
        frm = from_override or (email.utils.formataddr((p.get("from_name", ""), p.get("from_email", "")))
                                if p.get("from_email") else "")
        if not frm:
            raise NNTPError(441, "Set your name and e-mail under Settings > Posting first")
        domain = (p.get("from_email", "x@usenet.local").split("@")[-1]) or "usenet.local"
        headers = [
            ("From", frm), ("Newsgroups", ",".join(groups)), ("Subject", subject),
            ("Date", email.utils.formatdate(localtime=False)),
            ("Message-ID", f"<{uuid.uuid4().hex}@{domain}>"),
            ("User-Agent", "Newsreader3000/1.2"), ("MIME-Version", "1.0"),
            ("Content-Type", "text/plain; charset=UTF-8"), ("Content-Transfer-Encoding", "8bit"),
        ]
        if p.get("organization"):
            headers.append(("Organization", p["organization"]))
        if references:
            headers.append(("References", references))
        raw = "\r\n".join(f"{k}: {v}" for k, v in headers) + "\r\n\r\n" + body.replace("\r\n", "\n")
        pool = self.pools.reader()
        return await pool.run(lambda c: c.post(raw.encode("utf-8")), retries=0)
