"""Eenvoudige achtergrondjobs met een live log voor de web UI."""
from __future__ import annotations

import threading
import time
import traceback
import uuid

_jobs: dict[str, dict] = {}
_active_lock = threading.Lock()


def start(title: str, fn, *args, exclusive=True) -> dict:
    """Start fn(log, *args) in een thread. exclusive: maar één lab-wijzigende job tegelijk."""
    if exclusive and not _active_lock.acquire(blocking=False):
        raise RuntimeError("Er draait al een deploy/destroy-job.")
    job = {"id": uuid.uuid4().hex[:8], "title": title, "status": "running",
           "started": time.time(), "lines": []}
    _jobs[job["id"]] = job

    def log(msg: str):
        for line in str(msg).rstrip().splitlines() or [""]:
            job["lines"].append(f"{time.strftime('%H:%M:%S')} {line}")

    def runner():
        try:
            fn(log, *args)
            job["status"] = "done"
            log("✔ klaar")
        except Exception as e:  # noqa: BLE001 - alles naar de UI-log
            job["status"] = "failed"
            log(f"✖ {e}")
            log(traceback.format_exc())
        finally:
            job["ended"] = time.time()
            if exclusive:
                _active_lock.release()

    threading.Thread(target=runner, daemon=True).start()
    return job


def get(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def recent(n=10) -> list[dict]:
    jobs = sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)[:n]
    return [{k: v for k, v in j.items() if k != "lines"} for j in jobs]
