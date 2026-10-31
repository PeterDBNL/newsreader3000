"""Settings storage. Settings live in <config>/settings.json and can be
pre-seeded from environment variables on first start (handy for Unraid)."""
import copy
import json
import os
import threading
import uuid

CONFIG_DIR = os.environ.get("NR_CONFIG_DIR", "/config")
DOWNLOAD_DIR = os.environ.get("NR_DOWNLOAD_DIR", "/downloads")
MASK = "********"

# Popular binary/text groups subscribed on first start so search works out of the box.
DEFAULT_GROUPS = [
    "alt.binaries.boneless",
    "alt.binaries.misc",
    "alt.binaries.multimedia",
    "alt.binaries.teevee",
    "alt.binaries.tv",
    "alt.binaries.hdtv",
    "alt.binaries.hdtv.x264",
    "alt.binaries.x264",
    "alt.binaries.movies",
    "alt.binaries.movies.divx",
    "alt.binaries.dvd",
    "alt.binaries.mp3",
    "alt.binaries.sounds.mp3",
    "alt.binaries.sounds.lossless",
    "alt.binaries.e-book",
    "alt.binaries.ebook",
    "alt.binaries.cores",
    "alt.binaries.mom",
    "alt.binaries.frogs",
    "alt.binaries.pictures",
]

DEFAULTS = {
    "servers": [],
    "ui": {"username": "", "password": ""},
    "downloads": {
        "complete_dir": os.path.join(DOWNLOAD_DIR, "complete"),
        "incomplete_dir": os.path.join(DOWNLOAD_DIR, "incomplete"),
        "watch_dir": os.path.join(DOWNLOAD_DIR, "watch"),
        "auto_extract": True,
        "nested_extract": True,
        "delete_archives": True,
        "par2_repair": True,
        "always_verify": False,
        "delete_par2": True,
        "speed_limit_kbps": 0,
        "max_retries": 3,
        "categories": ["movies", "tv", "music", "books", "software", "other"],
        "default_category": "",
    },
    "headers": {
        "initial_fetch": 20000,
        "batch_size": 5000,
        "max_connections": 4,
        "auto_update_minutes": 0,
        "retention_days": 0,
        "fetch_descriptions": False,
    },
    "posting": {"from_name": "", "from_email": "", "organization": ""},
    "default_groups": DEFAULT_GROUPS,
}

SERVER_DEFAULTS = {
    "name": "Server",
    "host": "",
    "port": 563,
    "ssl": True,
    "verify_ssl": True,
    "username": "",
    "password": "",
    "connections": 10,
    "priority": 0,
    "enabled": True,
}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def normalize_server(s, old=None):
    srv = _merge(SERVER_DEFAULTS, s)
    srv["id"] = srv.get("id") or uuid.uuid4().hex[:8]
    if srv.get("password") == MASK:
        srv["password"] = (old or {}).get("password", "")
    srv["port"] = int(srv["port"])
    srv["connections"] = max(1, int(srv["connections"]))
    srv["priority"] = int(srv["priority"])
    return srv


class Settings:
    def __init__(self, config_dir=None):
        self.config_dir = config_dir or CONFIG_DIR
        self.path = os.path.join(self.config_dir, "settings.json")
        self._lock = threading.Lock()
        self._listeners = []
        os.makedirs(self.config_dir, exist_ok=True)
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = _merge(DEFAULTS, json.load(f))
            self.first_run = False
        else:
            self.data = copy.deepcopy(DEFAULTS)
            self.first_run = True
            self._seed_from_env()
            self.save()

    def _seed_from_env(self):
        host = os.environ.get("NR_SERVER_HOST")
        if host:
            self.data["servers"].append(normalize_server({
                "name": os.environ.get("NR_SERVER_NAME", host),
                "host": host,
                "port": os.environ.get("NR_SERVER_PORT", 563),
                "ssl": os.environ.get("NR_SERVER_SSL", "true").lower() in ("1", "true", "yes"),
                "username": os.environ.get("NR_SERVER_USERNAME", ""),
                "password": os.environ.get("NR_SERVER_PASSWORD", ""),
                "connections": os.environ.get("NR_SERVER_CONNECTIONS", 10),
            }))

    @property
    def ui_credentials(self):
        user = os.environ.get("NR_UI_USERNAME") or self.data["ui"].get("username", "")
        pw = os.environ.get("NR_UI_PASSWORD") or self.data["ui"].get("password", "")
        return user, pw

    def get(self, section):
        return self.data[section]

    def servers(self, enabled_only=True):
        srvs = [s for s in self.data["servers"] if s.get("enabled", True) or not enabled_only]
        return sorted(srvs, key=lambda s: s.get("priority", 0))

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)

    def public(self):
        """Settings with passwords masked, safe to send to the browser."""
        d = copy.deepcopy(self.data)
        for s in d["servers"]:
            if s.get("password"):
                s["password"] = MASK
        if d["ui"].get("password"):
            d["ui"]["password"] = MASK
        return d

    def update(self, new):
        old_servers = {s["id"]: s for s in self.data["servers"]}
        if "servers" in new:
            new["servers"] = [normalize_server(s, old_servers.get(s.get("id"))) for s in new["servers"]]
        if new.get("ui", {}).get("password") == MASK:
            new["ui"]["password"] = self.data["ui"].get("password", "")
        for k, v in new.items():
            if k not in DEFAULTS:
                continue
            if isinstance(DEFAULTS[k], dict):
                self.data[k] = _merge(self.data[k], v)
            else:
                self.data[k] = v
        self.save()
        for cb in self._listeners:
            cb()

    def on_change(self, cb):
        self._listeners.append(cb)
