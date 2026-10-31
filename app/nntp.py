"""Minimal asyncio NNTP client (RFC 3977 + AUTHINFO) with connection pooling.
Python 3.13 removed nntplib, so we speak the protocol directly."""
import asyncio
import contextlib
import email.header
import email.utils
import logging
import ssl as ssllib
import time
import zlib

log = logging.getLogger("nntp")
# Hosts known to reject OVER (we then use the older XOVER for every connection to them)
_XOVER_HOSTS = set()
# Hosts where compressed headers failed or were refused
_NO_COMPRESS = set()
CONN_ERRORS = (ConnectionError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError,
               asyncio.LimitOverrunError, EOFError)


class NNTPError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code} {msg}")
        self.code = code
        self.msg = msg


class NNTPConnection:
    def __init__(self, host, port=563, use_ssl=True, username="", password="", timeout=60, verify_ssl=True):
        self.host, self.port, self.use_ssl = host, int(port), use_ssl
        self.username, self.password = username, password
        self.timeout, self.verify_ssl = timeout, verify_ssl
        self.reader = self.writer = None
        self.current_group = None
        self.last_used = time.time()
        self.can_post = False
        self.key = f"{host}:{self.port}"
        self._over_cmd = "XOVER" if self.key in _XOVER_HOSTS else "OVER"
        self.compressed = None  # None = not negotiated yet

    async def connect(self):
        ctx = None
        if self.use_ssl:
            ctx = ssllib.create_default_context()
            if not self.verify_ssl:
                ctx.check_hostname = False
                ctx.verify_mode = ssllib.CERT_NONE
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ctx, limit=1 << 24), self.timeout)
        code, msg = await self._resp()
        if code not in (200, 201):
            raise NNTPError(code, msg)
        self.can_post = code == 200
        if self.username:
            await self._auth()
        with contextlib.suppress(NNTPError):
            code, _ = await self.cmd("MODE READER")
            self.can_post = code == 200 or self.can_post
        return self

    async def _auth(self):
        code, msg = await self._send_resp(f"AUTHINFO USER {self.username}")
        if code == 381:
            code, msg = await self._send_resp(f"AUTHINFO PASS {self.password}")
        if code not in (281, 250):
            raise NNTPError(code, f"Authentication failed: {msg}")

    async def _readline(self):
        line = await asyncio.wait_for(self.reader.readline(), self.timeout)
        if not line:
            raise ConnectionError("Server closed the connection")
        return line

    async def _resp(self):
        line = await self._readline()
        try:
            code = int(line[:3])
        except ValueError:
            raise ConnectionError(f"Bad response: {line[:80]!r}")
        return code, line[4:].decode("utf-8", "replace").strip()

    async def _send_resp(self, command):
        self.writer.write(command.encode("utf-8") + b"\r\n")
        await self.writer.drain()
        self.last_used = time.time()
        return await self._resp()

    async def cmd(self, command):
        code, msg = await self._send_resp(command)
        if code == 480 and self.username:
            await self._auth()
            code, msg = await self._send_resp(command)
        if code >= 400:
            raise NNTPError(code, msg)
        return code, msg

    async def _multiline(self, prefix=b""):
        first = prefix + await self._readline() if prefix[-1:] != b"\n" else prefix
        if first == b".\r\n":
            return b""
        data = bytearray(first)
        while not data.endswith(b"\r\n.\r\n"):
            data += await asyncio.wait_for(self.reader.readuntil(b".\r\n"), self.timeout)
        return _unstuff(bytes(data[:-3]))

    async def enable_compression(self):
        """Ask for gzip-compressed header listings (XFEATURE COMPRESS GZIP, offered by many big providers).
        Headers then travel 3-5x smaller. Silently stays off when the server doesn't support it."""
        if self.compressed is not None:
            return self.compressed
        self.compressed = False
        if self.key in _NO_COMPRESS:
            return False
        try:
            code, _ = await self.cmd("XFEATURE COMPRESS GZIP TERMINATOR")
            self.compressed = code == 290
        except NNTPError:
            pass
        if not self.compressed:
            _NO_COMPRESS.add(self.key)
        return self.compressed

    async def _compressed_block(self):
        head = await asyncio.wait_for(self.reader.readexactly(2), self.timeout)
        if not (head[0] == 0x78 or head == b"\x1f\x8b"):
            return await self._multiline(head)  # server chose to answer uncompressed
        d = zlib.decompressobj(47)  # accepts zlib or gzip framing
        out = [d.decompress(head)]
        while not d.eof:
            chunk = await asyncio.wait_for(self.reader.read(1 << 16), self.timeout)
            if not chunk:
                raise ConnectionError("Server closed the connection")
            out.append(d.decompress(chunk))
        rest = d.unused_data
        while len(rest) < 3:
            more = await asyncio.wait_for(self.reader.read(3 - len(rest)), self.timeout)
            if not more:
                raise ConnectionError("Server closed the connection")
            rest += more
        if rest != b".\r\n":
            raise ConnectionError("Unexpected data after compressed block")
        text = b"".join(out)
        if text == b".\r\n" or text.endswith(b"\r\n.\r\n"):
            text = text[:-3]
        return _unstuff(text)

    async def group(self, name):
        _, msg = await self.cmd(f"GROUP {name}")
        parts = msg.split()
        self.current_group = name
        return {"count": int(parts[0]), "first": int(parts[1]), "last": int(parts[2])}

    async def over_raw(self, first, last):
        """Overview lines for an article range as raw bytes (parse with parse_over_data)."""
        try:
            await self.cmd(f"{self._over_cmd} {first}-{last}")
        except NNTPError as e:
            # Some providers answer OVER with "400/500/501 Unrecognized command"; they support XOVER.
            if e.code in (400, 500, 501) and self._over_cmd == "OVER":
                self._over_cmd = "XOVER"
                _XOVER_HOSTS.add(self.key)
                try:
                    await self.cmd(f"XOVER {first}-{last}")
                except NNTPError as e2:
                    if e2.code == 423:
                        return b""
                    raise
            elif e.code == 423:
                return b""
            else:
                raise
        if not self.compressed:
            return await self._multiline()
        try:
            return await self._compressed_block()
        except (zlib.error, ConnectionError, asyncio.IncompleteReadError) as e:
            log.warning("%s: compressed headers failed (%s); using plain headers from now on", self.host, e)
            _NO_COMPRESS.add(self.key)
            self.close()
            raise ConnectionError("compressed header transfer failed")

    async def over(self, first, last):
        return parse_over_data(await self.over_raw(first, last))

    async def body(self, msgid):
        await self.cmd(f"BODY {fmt_id(msgid)}")
        return await self._multiline()

    async def article(self, msgid):
        await self.cmd(f"ARTICLE {fmt_id(msgid)}")
        return await self._multiline()

    async def stat(self, msgid):
        await self.cmd(f"STAT {fmt_id(msgid)}")
        return True

    async def list_active(self, pattern=None):
        await self.cmd("LIST ACTIVE" + (f" {pattern}" if pattern else ""))
        data = await self._multiline()
        out = []
        for line in data.split(b"\r\n"):
            p = line.decode("utf-8", "replace").split()
            if len(p) >= 3:
                try:
                    out.append((p[0], int(p[2]), int(p[1]), p[3] if len(p) > 3 else "y"))
                except ValueError:
                    pass
        return out

    async def list_newsgroups(self, pattern=None):
        await self.cmd("LIST NEWSGROUPS" + (f" {pattern}" if pattern else ""))
        data = await self._multiline()
        out = []
        for line in data.split(b"\r\n"):
            p = line.decode("utf-8", "replace").split(None, 1)
            if p:
                out.append((p[0], p[1].strip() if len(p) > 1 else ""))
        return out

    async def post(self, raw: bytes):
        code, msg = await self._send_resp("POST")
        if code != 340:
            raise NNTPError(code, msg)
        lines = raw.replace(b"\r\n", b"\n").split(b"\n")
        payload = b"\r\n".join((b"." + l if l.startswith(b".") else l) for l in lines)
        self.writer.write(payload + b"\r\n.\r\n")
        await self.writer.drain()
        code, msg = await self._resp()
        if code != 240:
            raise NNTPError(code, msg)
        return msg

    async def quit(self):
        with contextlib.suppress(Exception):
            self.writer.write(b"QUIT\r\n")
            await asyncio.wait_for(self.writer.drain(), 3)
        self.close()

    def close(self):
        with contextlib.suppress(Exception):
            if self.writer:
                self.writer.close()
        self.writer = None


def _unstuff(data: bytes):
    if data.startswith(b".."):
        data = data[1:]
    return data.replace(b"\r\n..", b"\r\n.")


def parse_over_data(data: bytes):
    return [parse_over_line(l) for l in data.split(b"\r\n") if l]


def fmt_id(msgid):
    msgid = msgid.strip()
    return msgid if msgid.startswith("<") else f"<{msgid}>"


def decode_header_value(raw: str):
    if "=?" not in raw:
        return raw
    try:
        return str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:
        return raw


def parse_date(s):
    try:
        return int(email.utils.parsedate_to_datetime(s).timestamp())
    except Exception:
        return int(time.time())


def parse_over_line(line: bytes):
    f = line.decode("utf-8", "replace").split("\t")
    f += [""] * (8 - len(f))
    refs = f[5].split()
    def num(v):
        try:
            return int(v)
        except ValueError:
            return 0
    return {
        "num": num(f[0]), "subject": decode_header_value(f[1]), "poster": decode_header_value(f[2]),
        "date": parse_date(f[3]), "msgid": f[4], "parent": refs[-1] if refs else "",
        "bytes": num(f[6]), "lines": num(f[7]),
    }


class ServerPool:
    """Pool of connections to one server, capped at the configured connection count."""

    def __init__(self, cfg, max_conns=None):
        self.cfg = cfg
        self.max = max_conns or cfg.get("connections", 4)
        self.sem = asyncio.Semaphore(self.max)
        self.idle = []
        self.closed = False

    def _new(self):
        c = self.cfg
        return NNTPConnection(c["host"], c["port"], c.get("ssl", True), c.get("username", ""),
                              c.get("password", ""), verify_ssl=c.get("verify_ssl", True))

    @contextlib.asynccontextmanager
    async def connection(self):
        async with self.sem:
            conn = None
            while self.idle:
                conn = self.idle.pop()
                if time.time() - conn.last_used < 120 and conn.writer:
                    break
                await conn.quit()
                conn = None
            if conn is None:
                conn = await self._new().connect()
            ok = False
            try:
                yield conn
                ok = True
            except NNTPError as e:
                ok = e.code != 400  # 400 means the server may drop this connection
                raise
            finally:
                if ok and not self.closed:
                    self.idle.append(conn)
                else:
                    conn.close()

    async def run(self, fn, retries=2):
        """Run fn(conn) and retry on dropped connections."""
        for attempt in range(retries + 1):
            try:
                async with self.connection() as conn:
                    return await fn(conn)
            except CONN_ERRORS:
                if attempt >= retries:
                    raise
                await asyncio.sleep(1 + attempt)

    async def close(self):
        self.closed = True
        idle, self.idle = self.idle, []
        for c in idle:
            await c.quit()


class Pools:
    """One pool per configured server; rebuilt when settings change."""

    def __init__(self, settings):
        self.settings = settings
        self.pools = {}
        self.reader_pools = {}
        self._sig = self._signature()
        settings.on_change(self.reset)

    def _signature(self):
        return repr((self.settings.servers(), self.settings.get("headers").get("max_connections")))

    def reset(self):
        """Drop pools only when server settings changed (not e.g. for a speed-limit change)."""
        sig = self._signature()
        if sig == self._sig:
            return
        self._sig = sig
        old = list(self.pools.values()) + list(self.reader_pools.values())
        self.pools, self.reader_pools = {}, {}
        for p in old:
            try:
                asyncio.get_running_loop().create_task(p.close())
            except RuntimeError:
                pass

    def _reader_conns(self, s):
        return max(1, min(int(self.settings.get("headers").get("max_connections", 4)), s["connections"]))

    async def close_all(self):
        for p in list(self.pools.values()) + list(self.reader_pools.values()):
            await p.close()
        self.pools, self.reader_pools = {}, {}

    def download_pools(self):
        out = []
        srvs = [s for s in self.settings.servers() if s.get("host")]
        for i, s in enumerate(srvs):
            if s["id"] not in self.pools:
                # Leave room for the browse/header connections on the primary server.
                n = s["connections"] - (self._reader_conns(s) if i == 0 else 0)
                self.pools[s["id"]] = ServerPool(s, max(1, n))
            out.append(self.pools[s["id"]])
        return out

    def reader(self):
        """Separate small pool on the primary server for browsing, so downloads never block reading."""
        srvs = [s for s in self.settings.servers() if s.get("host")]
        if not srvs:
            raise NNTPError(503, "No news server configured. Add one under Settings.")
        s = srvs[0]
        if s["id"] not in self.reader_pools:
            self.reader_pools[s["id"]] = ServerPool(s, self._reader_conns(s))
        return self.reader_pools[s["id"]]
