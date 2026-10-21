"""Recursive folder scan; groups images by EXIF capture month."""
import calendar
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


def scan_folder(folder, db, progress=None, cancelled=lambda: False):
    """Walk folder recursively and record files in the DB. Returns count of new/changed files."""
    batch, count = [], 0
    for root, _dirs, names in os.walk(folder):
        for name in names:
            if cancelled():
                db.upsert_scanned(batch)
                return count
            ext = os.path.splitext(name)[1].lower()
            is_video = ext in VIDEO_EXT
            if ext not in IMAGE_EXT and not is_video:
                continue
            path = os.path.normpath(os.path.join(root, name))
            try:
                st = os.stat(path)
            except OSError:
                continue
            known = db.query("SELECT size, mtime FROM files WHERE path=?", (path,))
            if known and known[0]["size"] == st.st_size and known[0]["mtime"] == st.st_mtime:
                continue  # unchanged: keep recorded upload state
            taken = (filename_datetime(name) if is_video else exif_datetime(path)) \
                or datetime.fromtimestamp(st.st_mtime)
            key = f"{taken.year:04d}-{taken.month:02d}"
            batch.append((path, st.st_size, st.st_mtime, taken.isoformat(), key, album_title(key)))
            count += 1
            if len(batch) >= 200:
                db.upsert_scanned(batch)
                batch = []
            if progress and count % 25 == 0:
                progress(count)
    db.upsert_scanned(batch)
    if progress:
        progress(count)
    return count
