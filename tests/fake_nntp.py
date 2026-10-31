"""A small in-memory NNTP server for tests."""
import asyncio
import email.utils
import io
import time
import zipfile

from app import yenc


class FakeServer:
    def __init__(self, user="u", password="p"):
        self.user, self.password = user, password
        self.groups = {}  # name -> list of (num, headers dict, body bytes)
        self.by_id = {}
        self.posted = []
        self.missing = set()
        self.reject_over = False  # mimic providers that only know XOVER

    def add(self, group, subject, body: bytes, poster="Tester <t@example.com>", msgid=None, refs=""):
        arts = self.groups.setdefault(group, [])
        num = len(arts) + 1
        msgid = msgid or f"<{group}.{num}@fake>"
        h = {"From": poster, "Subject": subject, "Date": email.utils.formatdate(time.time() - 3600),
             "Message-ID": msgid, "Newsgroups": group, "References": refs}
        arts.append((num, h, body))
        self.by_id[msgid] = (h, body)
        return msgid

    def add_binary(self, group, filename, data, part_size=3000, prefix="[1/1]"):
        parts = [data[i:i + part_size] for i in range(0, len(data), part_size)]
        ids = []
        for i, chunk in enumerate(parts, 1):
            body = yenc.encode(chunk, filename, part=i, total=len(parts), begin=(i - 1) * part_size + 1,
                               size=len(data))
            ids.append(self.add(group, f'{prefix} - "{filename}" yEnc ({i}/{len(parts)})', body))
        return ids

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, r, w):
        def send(line):
            w.write(line.encode() + b"\r\n")

        def send_block(data: bytes):
            lines = data.split(b"\r\n") if data else []
            for l in lines:
                w.write((b"." + l if l.startswith(b".") else l) + b"\r\n")
            w.write(b".\r\n")
        send("200 fake server ready")
        authed = False
        group = None
        while True:
            line = await r.readline()
            if not line:
                break
            cmd = line.decode().strip()
            up = cmd.upper()
            if up.startswith("AUTHINFO USER"):
                authed = cmd.split()[-1] == self.user
                send("381 password required")
            elif up.startswith("AUTHINFO PASS"):
                authed = authed and cmd.split()[-1] == self.password
                send("281 ok" if authed else "481 bad auth")
            elif up == "QUIT":
                send("205 bye")
                await w.drain()
                break
            elif not authed:
                send("480 auth required")
            elif up == "MODE READER":
                send("200 posting ok")
            elif up == "DATE":
                send("111 20260101000000")
            elif up.startswith("LIST ACTIVE") or up == "LIST":
                send("215 list follows")
                send_block("\r\n".join(f"{g} {len(a)} 1 y" for g, a in self.groups.items()).encode())
            elif up.startswith("LIST NEWSGROUPS"):
                send("215 desc follow")
                send_block("\r\n".join(f"{g}\tTest group {g}" for g in self.groups).encode())
            elif up.startswith("GROUP "):
                group = cmd.split()[1]
                a = self.groups.get(group)
                if a is None:
                    send("411 no such group")
                else:
                    send(f"211 {len(a)} 1 {len(a)} {group}")
            elif up.startswith("OVER") and self.reject_over:
                send("400 Unrecognized command")
            elif up.startswith(("OVER", "XOVER")):
                lo, hi = (int(x) for x in cmd.split()[1].split("-"))
                send("224 overview")
                out = []
                for num, h, body in self.groups.get(group, []):
                    if lo <= num <= hi:
                        out.append("\t".join([str(num), h["Subject"], h["From"], h["Date"], h["Message-ID"],
                                              h["References"], str(len(body)), str(body.count(b"\r\n") + 1)]))
                send_block("\r\n".join(out).encode())
            elif up.startswith(("BODY", "ARTICLE", "STAT")):
                mid = cmd.split()[1]
                if mid not in self.by_id or mid in self.missing:
                    send("430 no such article")
                    continue
                h, body = self.by_id[mid]
                if up.startswith("STAT"):
                    send(f"223 0 {mid}")
                elif up.startswith("BODY"):
                    send(f"222 0 {mid}")
                    send_block(body)
                else:
                    send(f"220 0 {mid}")
                    hdr = "\r\n".join(f"{k}: {v}" for k, v in h.items() if v).encode()
                    send_block(hdr + b"\r\n\r\n" + body)
            elif up == "POST":
                send("340 send article")
                buf = []
                while True:
                    l = await r.readline()
                    if l == b".\r\n":
                        break
                    buf.append(l[1:] if l.startswith(b"..") else l)
                self.posted.append(b"".join(buf))
                send("240 article posted")
            else:
                send("500 unknown command")
            await w.drain()
        w.close()


def make_zip(files):
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)
    return bio.getvalue()
