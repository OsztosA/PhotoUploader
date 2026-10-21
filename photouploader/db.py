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
    status TEXT NOT NULL DEFAULT 'new',   -- new | uploaded | done | failed
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
                else:
                    self._c.execute(
                        "UPDATE files SET size=?,mtime=?,taken=?,album_key=?,status='new',"
                        "upload_token=NULL,media_item_id=NULL,error=NULL WHERE path=?",
                        (size, mtime, taken, key, path))
            self._c.commit()

    def album_summary(self):
        return self.query(
            "SELECT a.key, a.title, a.album_id, a.share_url, COUNT(f.path) total, "
            "SUM(f.status='done') done, SUM(f.status='failed') failed, "
            "COALESCE(SUM(f.size),0) bytes, "
            "COALESCE(SUM(CASE WHEN f.status!='done' THEN f.size END),0) pending_bytes "
            "FROM albums a JOIN files f ON f.album_key=a.key GROUP BY a.key ORDER BY a.key")

    def set_album(self, key, album_id):
        self.execute("UPDATE albums SET album_id=? WHERE key=?", (album_id, key))

    def set_share(self, key, url):
        self.execute("UPDATE albums SET share_url=? WHERE key=?", (url, key))

    def pending_files(self, key):
        return self.query(
            "SELECT * FROM files WHERE album_key=? AND status IN ('new','uploaded','failed') "
            "ORDER BY taken, path", (key,))

    def mark(self, path, status, token=None, media_id=None, error=None):
        self.execute(
            "UPDATE files SET status=?, upload_token=?, media_item_id=COALESCE(?,media_item_id), "
            "error=?, updated=? WHERE path=?", (status, token, media_id, error, time.time(), path))
