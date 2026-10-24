"""Recursive folder scan; groups images by EXIF capture month."""
import calendar
import hashlib
import os
import re
from datetime import datetime

from PIL import Image

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif"}


VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".mpg", ".mpeg", ".wmv", ".mts", ".m2ts", ".webm"}
FILENAME_DT = re.compile(r"^\s*(\d{4})\.(\d{2})\.(\d{2}) (\d{2})\.(\d{2})\.(\d{2})")


def filename_datetime(name):
    """Videos have no EXIF: filenames start with 'YYYY.mm.dd HH.MM.SS'."""
    m = FILENAME_DT.match(name)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()))
    except ValueError:
        return None


def album_title(key):
    year, month = key.split("-")
    return f"{year}. {calendar.month_name[int(month)]}"


def exif_datetime(path):
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            sub = exif.get_ifd(0x8769)
            value = sub.get(36867) or sub.get(36868) or exif.get(306)
    except Exception:
        return None
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    try:
        dt = datetime.strptime(str(value).strip()[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    return dt if dt.year > 1970 else None


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mark_duplicates(db, cancelled=lambda: False, progress=None):
    """Files with equal size AND equal SHA-256 are duplicates; only the first copy is kept.

    Already uploaded files are preferred as the original. Returns the number of duplicates marked.
    """
    sizes = [r["size"] for r in db.query("SELECT size FROM files GROUP BY size HAVING COUNT(*)>1")]
    marked = checked = 0
    total = db.query("SELECT COUNT(*) n FROM files WHERE size IN "
                     "(SELECT size FROM files GROUP BY size HAVING COUNT(*)>1)")[0]["n"]
    for size in sizes:
        group = db.query("SELECT path, status, hash FROM files WHERE size=? ORDER BY path", (size,))
        by_hash = {}
        for r in group:
            if cancelled():
                return marked
            checked += 1
            if progress and (checked % 10 == 0 or checked == total):
                progress("hashing", checked, total)
            h = r["hash"]
            if not h:
                try:
                    h = file_hash(r["path"])
                except OSError:
                    continue
                db.execute("UPDATE files SET hash=? WHERE path=?", (h, r["path"]))
            by_hash.setdefault(h, []).append(r)
        for items in by_hash.values():
            if len(items) < 2:
                continue
            items.sort(key=lambda r: (r["status"] != "done", r["status"] == "duplicate"))
            original = items[0]["path"]
            for r in items[1:]:
                if r["status"] in ("new", "uploaded", "failed", "duplicate"):
                    db.execute("UPDATE files SET status='duplicate', upload_token=NULL, error=? WHERE path=?",
                               (f"Duplicate of {original}", r["path"]))
                    marked += 1
    return marked


def scan_folder(folder, db, progress=None, cancelled=lambda: False, rescan=False):
    """Walk folder recursively and record files in the DB. Returns count of new/changed files.

    With rescan=True, every file of an album that still has unprocessed files is re-evaluated
    (album assignment, duplicates) and DB entries of vanished files in those albums are dropped.
    """
    batch, count, checked = [], 0, 0
    reopen = db.unprocessed_keys() if rescan else set()
    seen = set()
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]  # skip hidden folders
        for name in names:
            if cancelled():
                db.upsert_scanned(batch)
                return count
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            is_video = ext in VIDEO_EXT
            if ext not in IMAGE_EXT and not is_video:
                continue
            path = os.path.normpath(os.path.join(root, name))
            try:
                st = os.stat(path)
            except OSError:
                continue
            seen.add(path)
            checked += 1
            if progress and checked % 25 == 0:
                progress("scanning", checked, None)
            known = db.query("SELECT size, mtime, album_key FROM files WHERE path=?", (path,))
            if known and known[0]["size"] == st.st_size and known[0]["mtime"] == st.st_mtime \
                    and known[0]["album_key"] not in reopen:
                continue  # unchanged: keep recorded upload state
            taken = (filename_datetime(name) if is_video else exif_datetime(path)) \
                or datetime.fromtimestamp(st.st_mtime)
            key = f"{taken.year:04d}-{taken.month:02d}"
            batch.append((path, st.st_size, st.st_mtime, taken.isoformat(), key, album_title(key)))
            count += 1
            if len(batch) >= 200:
                db.upsert_scanned(batch)
                batch = []
    db.upsert_scanned(batch)
    if rescan:
        for key in reopen:
            for r in db.query("SELECT path FROM files WHERE album_key=? AND status!='done'", (key,)):
                if r["path"] not in seen and not os.path.exists(r["path"]):
                    db.delete_file(r["path"])
    mark_duplicates(db, cancelled, progress)
    if progress:
        progress("done", checked, checked)
    return count
