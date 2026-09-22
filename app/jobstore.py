"""Private, atomic job records for the single-user local application."""
from dataclasses import asdict, dataclass, field, fields
import json
import os
from pathlib import Path
import re
import shutil
import time

TTL = 3600
ACTIVE = {"queued", "processing"}
ID_PATTERN = re.compile(r"[0-9a-f]{32}")


@dataclass
class Job:
    id: str
    url: str
    title: str
    height: int | None
    created: float = field(default_factory=time.time)
    queued_at: float = field(default_factory=time.time)
    status: str = "queued"
    message: str = "Sıraya alındı…"
    percent: int | None = None
    size: int | None = None
    code: str | None = None
    retryable: bool = False
    expires_at: float | None = None
    route: str = 'direct'
    compatibility: str = 'fast'
    cost_weight: int = 1
    strategy: str | None = None

    def public(self):
        result = asdict(self)
        result.pop("url")
        return result


class JobStore:
    def __init__(self, root: Path):
        self.root = root

    def save(self, job: Job):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.root / f"{job.id}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(asdict(job), stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.root / f"{job.id}.json")

    def remove(self, key):
        shutil.rmtree(self.root / key, ignore_errors=True)
        (self.root / f"{key}.json").unlink(missing_ok=True)
        (self.root / f"{key}.tmp").unlink(missing_ok=True)

    def load(self):
        jobs = {}
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        known = {f.name for f in fields(Job)}
        for record in self.root.glob("*.json"):
            if not ID_PATTERN.fullmatch(record.stem):
                continue
            try:
                data = json.loads(record.read_text())
                job = Job(**{k: v for k, v in data.items() if k in known})
                if job.id != record.stem or job.status not in ACTIVE | {"paused", "complete", "error", "cancelled"}:
                    raise ValueError("Invalid job record")
                if job.status in ACTIVE:
                    job.status, job.message = "paused", "Sunucu yeniden başlatıldı. İndirmeye devam edebilirsin."
                    job.code, job.retryable, job.expires_at = "interrupted", True, time.time() + TTL
                elif job.expires_at is None:
                    job.expires_at = job.created + TTL
                if job.expires_at is not None and job.expires_at <= time.time():
                    self.remove(job.id)
                    continue
                if job.status == "complete" and not (self.root / job.id / "video.mp4").is_file():
                    job.status, job.message = "error", "Hazırlanan dosya bulunamadı. Yeniden hazırlayabilirsin."
                    job.code, job.retryable = "file_missing", True
                jobs[job.id] = job
                self.save(job)
            except (ValueError, TypeError, KeyError):
                self.remove(record.stem)
        # Clean only our UUID-named orphan directories and incomplete metadata files.
        for path in self.root.iterdir():
            if path.is_dir() and ID_PATTERN.fullmatch(path.name) and path.name not in jobs:
                shutil.rmtree(path, ignore_errors=True)
            elif path.suffix == ".tmp" and ID_PATTERN.fullmatch(path.stem):
                path.unlink(missing_ok=True)
        return jobs
