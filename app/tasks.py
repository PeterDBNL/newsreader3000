"""Tracks background tasks (group list refresh, header fetches) for the UI."""
import asyncio
import itertools
import logging
import time

log = logging.getLogger("tasks")


class Tasks:
    def __init__(self):
        self.items = {}
        self._ids = itertools.count(1)

    def start(self, kind, label, coro_fn):
        """coro_fn(task_dict) -> coroutine. Same kind+label won't run twice at once."""
        for t in self.items.values():
            if t["kind"] == kind and t["label"] == label and t["status"] == "running":
                return t
        tid = next(self._ids)
        t = {"id": tid, "kind": kind, "label": label, "status": "running", "progress": 0, "total": 0,
             "message": "", "started": time.time(), "finished": None}
        self.items[tid] = t

        async def runner():
            try:
                await coro_fn(t)
                t["status"] = "done"
            except asyncio.CancelledError:
                t["status"] = "cancelled"
            except Exception as e:
                log.exception("task %s failed", label)
                t["status"] = "error"
                t["message"] = str(e)
            t["finished"] = time.time()
        t["_task"] = asyncio.get_running_loop().create_task(runner())
        self._prune()
        return t

    def cancel(self, tid):
        t = self.items.get(tid)
        if t and t["status"] == "running":
            t["_task"].cancel()

    def _prune(self):
        done = [t for t in self.items.values() if t["status"] != "running"]
        for t in sorted(done, key=lambda t: t["finished"] or 0)[:-30]:
            self.items.pop(t["id"], None)

    def list(self):
        return [{k: v for k, v in t.items() if not k.startswith("_")}
                for t in sorted(self.items.values(), key=lambda t: -t["id"])]
