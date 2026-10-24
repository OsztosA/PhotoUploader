"""SQLite record of every scanned / uploaded file and every album. Survives restarts."""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    key TEXT PRIMARY KEY,            -- 'YYYY-MM'
    title TEXT NOT NULL,
    album_id TEXT,
    share_url TEXT
);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    taken TEXT NOT NULL,             -- ISO datetime from EXIF (or file mtime)
    album_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',   -- new | uploaded | done | failed | duplicate
    upload_token TEXT,
    media_item_id TEXT,
    error TEXT,
    updated REAL
);
CREATE INDEX IF NOT EXISTS idx_files_album ON files(album_key, status);
"""


class Database:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        with self._lock:
            self._c.executescript(SCHEMA)
            cols = {r["name"] for r in self._c.execute("PRAGMA table_info(files)")}
            if "hash" not in cols:
                self._c.execute("ALTER TABLE files ADD COLUMN hash TEXT")
            self._c.execute("CREATE INDEX IF NOT EXISTS idx_files_size ON files(size)")
            self._c.commit()

    def execute(self, sql, params=()):
        with self._lock:
            cur = self._c.execute(sql, params)
            self._c.commit()
            return cur

    def query(self, sql, params=()):
        with self._lock:
            return self._c.execute(sql, params).fetchall()

    def upsert_scanned(self, rows):
        """rows: (path, size, mtime, taken, album_key, title). Changed files are reset to 'new'."""
        with self._lock:
            for path, size, mtime, taken, key, title in rows:
                self._c.execute("INSERT OR IGNORE INTO albums(key, title) VALUES (?,?)", (key, title))
                old = self._c.execute("SELECT size, mtime FROM files WHERE path=?", (path,)).fetchone()
                if old is None:
                    self._c.execute(
                        "INSERT INTO files(path,size,mtime,taken,album_key) VALUES (?,?,?,?,?)",
                        (path, size, mtime, taken, key))
                elif old["size"] == size and old["mtime"] == mtime:
                    # rescan of an unchanged file: keep upload state, re-evaluate duplicates
                    self._c.execute(
                        "UPDATE files SET taken=?,album_key=?,"
                        "status=CASE WHEN status='duplicate' THEN 'new' ELSE status END,"
                        "error=CASE WHEN status='duplicate' THEN NULL ELSE error END WHERE path=?",
                        (taken, key, path))
                else:
                    self._c.execute(
                        "UPDATE files SET size=?,mtime=?,taken=?,album_key=?,status='new',hash=NULL,"
                        "upload_token=NULL,media_item_id=NULL,error=NULL WHERE path=?",
                        (size, mtime, taken, key, path))
            self._c.commit()

    def unprocessed_keys(self):
        """Albums that still have files which are neither done nor duplicates."""
        return {r["album_key"] for r in self.query(
            "SELECT DISTINCT album_key FROM files WHERE status NOT IN ('done','duplicate')")}

    def delete_file(self, path):
        self.execute("DELETE FROM files WHERE path=?", (path,))

    def album_summary(self):
        return self.query(
            "SELECT a.key, a.title, a.album_id, a.share_url, COUNT(f.path) total, "
            "SUM(f.status='done') done, SUM(f.status='failed') failed, "
            "COALESCE(SUM(f.size),0) bytes, "
            "COALESCE(SUM(CASE WHEN f.status!='done' THEN f.size END),0) pending_bytes "
            "FROM albums a JOIN files f ON f.album_key=a.key AND f.status!='duplicate' GROUP BY a.key ORDER BY a.key")

    def set_album(self, key, album_id):
        self.execute("UPDATE albums SET album_id=? WHERE key=?", (album_id, key))

    def set_share(self, key, url):
        self.execute("UPDATE albums SET share_url=? WHERE key=?", (url, key))

    def pending_files(self, key, status=None):
        if status == "failed":
            return self.query(
                "SELECT * FROM files WHERE album_key=? AND status='failed' ORDER BY taken, path", (key,))
        if status == "pending":
            return self.query(
                "SELECT * FROM files WHERE album_key=? AND status IN ('new','uploaded') "
                "ORDER BY taken, path", (key,))
        return self.query(
            "SELECT * FROM files WHERE album_key=? AND status IN ('new','uploaded','failed') "
            "ORDER BY taken, path", (key,))

    def mark(self, path, status, token=None, media_id=None, error=None):
        self.execute(
            "UPDATE files SET status=?, upload_token=?, media_item_id=COALESCE(?,media_item_id), "
            "error=?, updated=? WHERE path=?", (status, token, media_id, error, time.time(), path))
