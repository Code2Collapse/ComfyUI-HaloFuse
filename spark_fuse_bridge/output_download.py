"""0.3.x compatibility shim for the manual "Download outputs" routes (/spark_fuse/recent_jobs, /spark_fuse/download/*).

Outputs are now delivered automatically by ``delivery`` when a job ends. These functions keep their 0.3.x names and
response shapes for one release; they run the same unified delivery (manual recovery) and read its status, so no
second download mechanism exists. Files land in <output>/HaloFuse/<job_id>/ (no longer spark_fuse/<id8>/).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import delivery
from .job_store import HaloFuseError

# The 0.3.x folder. Outputs written there before the upgrade stay where they are and stay readable; nothing new is
# written into it.
OUTPUT_SUBFOLDER = "spark_fuse"

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
_INVALID_ID = "That is not a valid HaloFuse job id."
_LEGACY_STATUS = {"pending": "downloading", "downloading": "downloading", "complete": "done", "partial": "done",
                  "failed": "failed"}
_FAILED: dict[str, dict] = {}   # job id -> legacy state of a recovery refused before any delivery started


def valid_job_id(job_id: str) -> bool:
    return bool(_JOB_ID_RE.fullmatch(job_id or ""))


def start_download(job_id: str) -> dict:
    """Start (or report) delivery of one job's outputs. Idempotent while a delivery for the same job runs."""
    if not valid_job_id(job_id):
        raise ValueError(_INVALID_ID)
    _FAILED.pop(job_id, None)
    try:
        delivery.recover(job_id, background=True)
    except HaloFuseError as exc:
        _FAILED[job_id] = {"status": "failed", "files": [], "folder": None, "error": exc.message, "started": None,
                           "finished": None, "total": 0, "progress": None, "job_id": job_id}
        return dict(_FAILED[job_id])
    return get_download_state(job_id) or {"status": "downloading", "files": [], "folder": None, "error": None,
                                          "started": None, "finished": None, "total": 0, "progress": None,
                                          "job_id": job_id}


def get_download_state(job_id: str) -> dict | None:
    if job_id in _FAILED:
        return dict(_FAILED[job_id])
    status = delivery.status(job_id)
    if status is None:
        return None
    files = [{"name": o["filename"], "subfolder": o["subfolder"], "size": o["size"]}
             for o in delivery.outputs(job_id)]
    files.sort(key=lambda f: (f["name"].lower().endswith(".log"), f["subfolder"], f["name"]))
    legacy = _LEGACY_STATUS.get(status.get("state"), "downloading")
    errors = status.get("errors") or []
    error = errors[0]["message"] if errors else None
    if legacy == "done" and not files and error is None:
        error = "The job's output folder is empty."
    running = legacy == "downloading"
    return {"status": legacy, "files": [] if running else files,
            "folder": (status.get("folder") or {}).get("subfolder"), "error": error,
            "started": status.get("started_at"), "finished": status.get("finished_at"),
            "total": status.get("files_total") or 0,
            "progress": f"{status.get('files_done') or 0}/{status.get('files_total') or 0}" if running else None,
            "job_id": job_id}


def recent_jobs(limit: int = 20) -> list[dict]:
    """This installation's newest jobs (X12: own jobs only), in the 0.3.x shape."""
    out = []
    for j in delivery.recent_jobs(limit):
        ts = j.get("created_at")
        iso = datetime.fromtimestamp(ts, timezone.utc).isoformat() if isinstance(ts, (int, float)) else None
        out.append({"id": j["job_id"], "status": j.get("status"), "created_at": iso,
                    "instance_type": j.get("instance_type")})
    return out
