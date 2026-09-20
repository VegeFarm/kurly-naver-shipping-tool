from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import settings


RUNTIME = Path(settings.runtime_dir)
RUNTIME.mkdir(parents=True, exist_ok=True)


def cleanup_old_jobs(max_age_hours: int = 24) -> None:
    cutoff = time.time() - max_age_hours * 3600
    for p in RUNTIME.iterdir():
        try:
            if p.is_dir() and p.stat().st_mtime < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def new_job_dir() -> tuple[str, Path]:
    cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    path = RUNTIME / job_id
    path.mkdir(parents=True, exist_ok=False)
    return job_id, path


def save_meta(job_dir: Path, meta: dict) -> None:
    meta = dict(meta)
    meta.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    (job_dir / "job.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_meta(job_id: str) -> tuple[Path, dict]:
    if not job_id or any(c not in "0123456789abcdef" for c in job_id.lower()):
        raise FileNotFoundError(job_id)
    path = RUNTIME / job_id
    meta_path = path / "job.json"
    if not meta_path.exists():
        raise FileNotFoundError(job_id)
    return path, json.loads(meta_path.read_text(encoding="utf-8"))
