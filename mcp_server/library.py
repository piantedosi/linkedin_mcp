"""Job posts owned by the LinkedIn MCP.

jobs_extracted keeps every post the server holds. jobs_interesting keeps the
posts moved there because they match what the user wants. jobs_minhash.jsonl
is the duplicate catalog for both folders.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from browser import (
    REPO_ROOT,
    display_path,
    job_id_from_url,
    posting_stem,
    render_posting,
    unique_stem,
)
from job_minhash import NGRAM, NUM_PERM, THRESHOLD, jaccard, signature

MCP_DIR = Path(__file__).resolve().parent
EXTRACTED = MCP_DIR / "jobs_extracted"
INTERESTING = MCP_DIR / "jobs_interesting"
INDEX_PATH = MCP_DIR / "jobs_minhash.jsonl"

_lock = threading.Lock()


def refresh_catalog() -> None:
    """Rebuild jobs_minhash.jsonl from the files in both library folders."""
    with _lock:
        _write(_reconcile())


def known_job_ids() -> set[str]:
    with _lock:
        return {job_id for record in _load() if (job_id := _job_id(record))}


def assess(link: str, description: str) -> dict:
    """Compare an open posting with the MinHash catalog."""
    text = render_posting(link, description)
    with _lock:
        report = _match(_load(), text, job_id_from_url(link))
    report["saved"] = False
    return report


def extract_posting(title: str, company: str, link: str, description: str) -> dict:
    """Save a new posting under jobs_extracted and add its signature."""
    text = render_posting(link, description)
    job_id = job_id_from_url(link)
    with _lock:
        records = _load()
        report = _match(records, text, job_id)
        if not report["is_new"]:
            report["saved"] = False
            return report
        EXTRACTED.mkdir(parents=True, exist_ok=True)
        used = {path.stem for path in EXTRACTED.glob("*.txt")}
        stem = unique_stem(posting_stem(company, title), used)
        path = EXTRACTED / f"{stem}.txt"
        path.write_text(text, encoding="utf-8")
        records.append(_record(path, text))
        _write(records)
        report["saved"] = True
        report["path"] = display_path(path)
        return report


def show_job(job: str) -> str:
    key = _query_key(job)
    with _lock:
        found = _find(key, EXTRACTED) or _find(key, INTERESTING)
    if found is None:
        raise RuntimeError(f"No extracted job matches {job.strip()!r}.")
    path, place = found
    note = (
        "This post was moved to jobs_interesting."
        if place == "interesting"
        else "This post is in jobs_extracted."
    )
    text = path.read_text(encoding="utf-8")
    return f"Path: {display_path(path)}\n{note}\n\n{text}"


def move_job(job: str) -> str:
    key = _query_key(job)
    with _lock:
        found = _find(key, EXTRACTED)
        if found is None:
            if _find(key, INTERESTING) is not None:
                raise RuntimeError(
                    "That post is not in jobs_extracted. It is already in jobs_interesting."
                )
            raise RuntimeError(f"No job in jobs_extracted matches {job.strip()!r}.")
        src, _place = found
        INTERESTING.mkdir(parents=True, exist_ok=True)
        dest = INTERESTING / src.name
        if dest.exists():
            used = {path.stem for path in INTERESTING.glob("*.txt")}
            dest = INTERESTING / f"{unique_stem(src.stem, used)}.txt"
        src_display = display_path(src)
        dest_display = display_path(dest)
        src.rename(dest)
        records = _load()
        text = dest.read_text(encoding="utf-8")
        updated = _record(dest, text)
        replaced = False
        for index, record in enumerate(records):
            if record.get("path") == src_display:
                records[index] = updated
                replaced = True
                break
        if not replaced:
            records.append(updated)
        _write(records)
    return f"Moved {src_display} to {dest_display}"


def _reconcile() -> list[dict]:
    """Index the files in both library folders and drop catalog lines whose file is gone."""
    EXTRACTED.mkdir(parents=True, exist_ok=True)
    INTERESTING.mkdir(parents=True, exist_ok=True)
    existing = {record.get("path"): record for record in _load()}
    kept: list[dict] = []
    for folder in (EXTRACTED, INTERESTING):
        for path in sorted(folder.glob("*.txt")):
            if not path.is_file() or "cover_letter" in path.stem.lower():
                continue
            rel = display_path(path)
            try:
                text = path.read_text(encoding="utf-8")
                mtime_ns = path.stat().st_mtime_ns
            except (OSError, UnicodeError):
                continue
            current = existing.get(rel)
            if _fresh(current, mtime_ns):
                kept.append(current)
            else:
                kept.append(_record(path, text, mtime_ns))
    return kept


def _fresh(record: dict | None, mtime_ns: int) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("mtime_ns") != mtime_ns:
        return False
    if record.get("ngram") != NGRAM or record.get("num_perm") != NUM_PERM:
        return False
    return _valid_signature(record.get("signature"))


def _match(records: list[dict], text: str, job_id: str | None) -> dict:
    if job_id:
        for record in records:
            if _job_id(record) == job_id:
                return {
                    "is_new": False,
                    "score": 1.0,
                    "threshold": THRESHOLD,
                    "path": record.get("path") or "",
                    "reason": "job_id",
                }
    query = signature(text)
    best_score = 0.0
    best_path = ""
    for record in records:
        values = record.get("signature")
        if not _valid_signature(values):
            continue
        score = jaccard(query, values)
        if score > best_score:
            best_score = score
            best_path = record.get("path") or ""
    return {
        "is_new": best_score < THRESHOLD,
        "score": round(best_score, 3),
        "threshold": THRESHOLD,
        "path": best_path,
        "reason": "minhash",
    }


def _record(path: Path, text: str, mtime_ns: int | None = None) -> dict:
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    record = {
        "path": display_path(path),
        "ngram": NGRAM,
        "num_perm": NUM_PERM,
        "signature": signature(text),
        "mtime_ns": path.stat().st_mtime_ns if mtime_ns is None else mtime_ns,
    }
    job_id = job_id_from_url(first)
    if job_id:
        record["job_id"] = job_id
    return record


def _find(key: str, folder: Path) -> tuple[Path, str] | None:
    place = "interesting" if folder == INTERESTING else "extracted"
    prefix = display_path(folder) + "/"
    for record in _load():
        path_text = record.get("path")
        if not isinstance(path_text, str) or not path_text.startswith(prefix):
            continue
        path = _safe_path(path_text)
        if path is None or not path.is_file():
            continue
        if _matches(path, _job_id(record), key):
            return path, place
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.txt")):
        if not path.is_file():
            continue
        job_id = None
        try:
            first = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            first = []
        if first:
            job_id = job_id_from_url(first[0].strip())
        if _matches(path, job_id, key):
            return path, place
    return None


def _matches(path: Path, job_id: str | None, key: str) -> bool:
    if key.isdigit():
        return job_id == key
    return path.stem == key


def _safe_path(path_text: str) -> Path | None:
    path = (REPO_ROOT / path_text).resolve()
    if _inside(EXTRACTED, path) or _inside(INTERESTING, path):
        return path
    return None


def _inside(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _job_id(record: dict) -> str | None:
    job_id = record.get("job_id")
    if isinstance(job_id, str) and job_id.isdigit():
        return job_id
    return None


def _valid_signature(values: object) -> bool:
    return (
        isinstance(values, list)
        and len(values) == NUM_PERM
        and all(isinstance(item, str) and len(item) == 16 for item in values)
    )


def _load() -> list[dict]:
    if not INDEX_PATH.is_file():
        return []
    try:
        raw = INDEX_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    by_path: dict[str, dict] = {}
    order: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        path_text = record.get("path")
        if not isinstance(path_text, str) or not path_text:
            continue
        if path_text not in by_path:
            order.append(path_text)
        by_path[path_text] = record
    return [by_path[path_text] for path_text in order]


def _write(records: list[dict]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    temporary = INDEX_PATH.with_name(INDEX_PATH.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, INDEX_PATH)


def _query_key(job: str) -> str:
    raw = job.strip()
    if not raw or any(part in raw for part in ("\x00",)):
        raise RuntimeError("job must be a filename stem or a numeric job id")
    name = Path(raw).name
    if name.endswith(".txt"):
        name = name[:-4]
    if not name or name in {".", ".."}:
        raise RuntimeError("job must be a filename stem or a numeric job id")
    return name
