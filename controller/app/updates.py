"""Versie-informatie en zelf-update van de controller (via git en update.sh)."""
from __future__ import annotations

import shutil
import subprocess

from .config import DATA, ROOT, get_state

UPDATE_LOG = DATA / "update.log"
UNIT = "otlab-update"


def _git(*args, timeout=30) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                          timeout=timeout, check=True).stdout.strip()


def otsim_tree() -> str | None:
    """Git-hash van de otsim-map in de huidige code."""
    try:
        return _git("rev-parse", "HEAD:otsim")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def version() -> dict:
    try:
        commit, date, subject = _git("log", "-1", "--format=%h%x09%cI%x09%s").split("\t", 2)
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return {"git": False}
    deployed = get_state().get("otsim_tree")
    current = otsim_tree()
    return {
        "git": True, "commit": commit, "date": date, "subject": subject, "branch": branch,
        # simulators in de containers lopen achter op de code van de controller
        "otsim_outdated": bool(get_state().get("deployed") and deployed and current and deployed != current),
        "updating": running(),
    }


def check() -> dict:
    """Haal de remote op en vergelijk met de lokale versie."""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    _git("fetch", "--quiet", "origin", branch, timeout=60)
    log = _git("log", "--format=%h%x09%cI%x09%s", "HEAD..FETCH_HEAD")
    commits = [dict(zip(("commit", "date", "subject"), line.split("\t", 2))) for line in log.splitlines() if line]
    dirty = [line for line in _git("status", "--porcelain", "--untracked-files=no").splitlines() if line]
    otsim_changed = bool(commits) and bool(_git("diff", "--name-only", "HEAD", "FETCH_HEAD", "--", "otsim"))
    return {"branch": branch, "behind": len(commits), "commits": commits, "dirty": dirty,
            "otsim_changed": otsim_changed}


def running() -> bool:
    if not shutil.which("systemctl"):
        return False
    r = subprocess.run(["systemctl", "is-active", "--quiet", UNIT])
    return r.returncode == 0


def start():
    """Draai update.sh als losse systemd-unit: het herstarten van otlab.service
    zou een gewoon child-proces (en daarmee de update) afbreken."""
    if running():
        raise RuntimeError("Er loopt al een update.")
    if not shutil.which("systemd-run"):
        raise RuntimeError("systemd-run ontbreekt; draai update.sh handmatig in de controller.")
    UPDATE_LOG.write_text("")
    subprocess.run(["systemctl", "reset-failed", UNIT], capture_output=True)
    subprocess.run(["systemd-run", f"--unit={UNIT}", "--collect", "--quiet",
                    "/bin/bash", "-c", f"{ROOT}/update.sh >> {UPDATE_LOG} 2>&1"], check=True)


def log_lines(offset: int = 0) -> dict:
    lines = UPDATE_LOG.read_text().splitlines() if UPDATE_LOG.exists() else []
    return {"lines": lines[offset:], "offset": len(lines), "running": running()}
