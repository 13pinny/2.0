"""File-attachment storage for Kartis.

Files live on disk under ATTACH_DIR (default <repo>/attachments, override
with KARTIS_ATTACHMENTS_DIR). DB rows in `attachments` track metadata and
point at a path relative to ATTACH_DIR. The two-layer split lets a parent
ticket row be deleted (DB rows go away) while preserving the file on disk
so OneDrive sync still has it.
"""
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.utils import secure_filename

import db

MAX_BYTES = 25 * 1024 * 1024
ATTACH_DIR = Path(os.getenv("KARTIS_ATTACHMENTS_DIR") or (Path(__file__).parent / "attachments")).resolve()
ATTACH_DIR.mkdir(parents=True, exist_ok=True)


def _safe_owner_dir(owner_id):
    # owner_id comes from our own DB (e.g. "pending-abc123"), but still strip
    # anything that's not in the conservative allowlist before joining paths.
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", owner_id or "")
    return cleaned or "misc"


def save_upload(owner_type, owner_id, file_storage):
    """Persist an uploaded werkzeug FileStorage to disk + DB. Returns the
    inserted attachment row as a dict. Caller is responsible for size
    enforcement before reading the file (Flask MAX_CONTENT_LENGTH covers it
    at the request layer)."""
    original = file_storage.filename or "file"
    safe_name = secure_filename(original) or "file"
    att_id = "att-" + uuid.uuid4().hex[:12]
    sub = _safe_owner_dir(owner_id)
    dest_dir = ATTACH_DIR / sub
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{att_id}-{safe_name}"
    file_storage.save(dest)
    size = dest.stat().st_size
    rel = dest.relative_to(ATTACH_DIR).as_posix()
    row = {
        "id": att_id,
        "owner_type": owner_type,
        "owner_id": owner_id,
        "filename": original,
        "stored_path": rel,
        "size_bytes": size,
        "content_type": file_storage.mimetype or "",
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    db.insert_attachment(row)
    return row


def disk_path(att_row):
    """Resolve an attachment's on-disk absolute Path. Refuses anything that
    escapes ATTACH_DIR (defense in depth — stored_path is written by us, but
    we still check)."""
    rel = (att_row.get("stored_path") or "").lstrip("/\\")
    p = (ATTACH_DIR / rel).resolve()
    try:
        p.relative_to(ATTACH_DIR)
    except ValueError:
        raise ValueError(f"attachment path escapes ATTACH_DIR: {rel!r}")
    return p


def delete(att_id):
    row = db.get_attachment(att_id)
    if not row:
        return False
    try:
        p = disk_path(row)
        if p.exists():
            p.unlink()
    except (OSError, ValueError):
        # Disk delete failures shouldn't block the DB cleanup; the file is
        # already orphaned at that point.
        pass
    db.delete_attachment(att_id)
    return True
