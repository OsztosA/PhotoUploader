"""Upload engine: share a worker pool across albums and batchCreate (<=50) per album.
Pausable / resumable; all state lives in the DB so a restart continues where it stopped."""
import csv
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .api import ApiError, BATCH_LIMIT

TOKEN_MAX_AGE = 20 * 3600  # upload tokens expire after ~1 day
FAILURE_WINDOW = 10
FAILURE_LIMIT = 5
FAILURE_COOLDOWN_SECONDS = 60
FINISHED_LINGER = 2  # seconds a completed file stays in the progress list


class Stats:
    def __init__(self, total_files, total_bytes):
        self.total_files, self.total_bytes = total_files, total_bytes
        self.files_done = self.files_failed = self.bytes_done = 0
        self.start = time.time()
        self._samples = deque()
        self._lock = threading.Lock()

    def add_bytes(self, n):
        with self._lock:
            self.bytes_done += n
            now = time.time()
            self._samples.append((now, self.bytes_done))
            while self._samples and now - self._samples[0][0] > 15:
                self._samples.popleft()

    def add_files_done(self):
        with self._lock:
            self.files_done += 1

    def add_files_failed(self):
        with self._lock:
            self.files_failed += 1

    def speed(self):
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
            return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def eta(self):
        s = self.speed()
        return (self.total_bytes - self.bytes_done) / s if s > 0 else None


class FileProgress:
    def __init__(self, path, size, album=""):
        self.path, self.size, self.done = path, size, 0
        self.album = album
        self.finished = 0
        self.start = time.time()
        self._samples = deque([(self.start, 0)])

    def add(self, n):
        self.done += n
        now = time.time()
        self._samples.append((now, self.done))
        while len(self._samples) > 2 and now - self._samples[0][0] > 5:
            self._samples.popleft()

    def speed(self):
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        if t1 <= t0:
            return 0.0
        # decay to zero if no data has arrived for a while
        return (b1 - b0) / (max(time.time(), t1) - t0)


class UploadEngine:
    def __init__(self, db, api, on_event, workers=4, share=True):
        self.db, self.api, self.on_event = db, api, on_event
        self.workers, self.share = workers, share
        self.upload_log_path = os.path.join(
            os.path.dirname(os.path.abspath(db.path)), "uploaded_files.csv")
        self.error_log_path = os.path.join(
            os.path.dirname(os.path.abspath(db.path)), "upload_errors.log")
        self._running = threading.Event()   # set = not paused
        self._stop = threading.Event()
        self._thread = None
        self.stats = None
        self._files_lock = threading.Lock()
        self._file_progress = {}
        self._share_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._commit_lock = threading.Lock()
        self._failure_lock = threading.Lock()
        self._failure_history = deque(maxlen=FAILURE_WINDOW)
        self._cooldown_until = 0.0

    def file_progress(self):
        now = time.time()
        with self._files_lock:
            # finished files linger briefly so fast (small) uploads are visible
            for p in [p for p, fp in self._file_progress.items()
                      if fp.finished and now - fp.finished > FINISHED_LINGER]:
                del self._file_progress[p]
            return list(self._file_progress.values())

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, album_keys, retry_failed_first=None):
        self._stop.clear()
        self._running.set()
        with self._failure_lock:
            self._failure_history.clear()
            self._cooldown_until = 0.0
        self._thread = threading.Thread(
            target=self._run, args=(list(album_keys), retry_failed_first), daemon=True)
        self._thread.start()

    def pause(self):
        self._running.clear()
        self._emit("paused")

    def resume(self):
        self._running.set()
        self._emit("running")

    def stop(self):
        self._stop.set()
        self._running.set()

    def _emit(self, kind, **kw):
        if kind in ("log", "error"):
            message = str(kw.get("message", "")).replace("\r", "\\r").replace("\n", "\\n")
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            try:
                with self._log_lock:
                    with open(self.error_log_path, "a", encoding="utf-8") as log:
                        log.write(f"{timestamp} ERROR {message}\n")
            except OSError as e:
                self.on_event({
                    "kind": "log",
                    "message": f"Could not write upload error log {self.error_log_path}: {e}",
                })
        self.on_event({"kind": kind, **kw})

    def _wait_if_paused(self):
        while not self._stop.is_set():
            if not self._running.wait(timeout=0.25):
                continue
            remaining = self.cooldown_remaining
            if remaining <= 0:
                return True
            self._stop.wait(min(remaining, 0.25))
        return False

    @property
    def cooldown_remaining(self):
        with self._failure_lock:
            return max(0.0, self._cooldown_until - time.monotonic())

    def _record_file_result(self, succeeded):
        with self._failure_lock:
            self._failure_history.append(not succeeded)
            if len(self._failure_history) < FAILURE_LIMIT:
                return
            if sum(self._failure_history) < FAILURE_LIMIT:
                return
            now = time.monotonic()
            if self._cooldown_until > now:
                return
            self._cooldown_until = now + FAILURE_COOLDOWN_SECONDS
        self._emit(
            "info",
            message=f"Upload failure rate is high "
                    f"({FAILURE_LIMIT} or more of the last {FAILURE_WINDOW} files failed); "
                    f"pausing for {FAILURE_COOLDOWN_SECONDS} seconds.",
        )

    def _run(self, keys, retry_failed_first=None):
        try:
            self._prepare(keys, retry_failed_first)
            first_error = None
            with ThreadPoolExecutor(max_workers=self.workers) as upload_pool:
                with ThreadPoolExecutor(max_workers=self.workers) as album_pool:
                    if retry_failed_first is True:
                        modes = ("failed", "pending")
                    elif retry_failed_first is False:
                        modes = ("pending",)
                    else:
                        modes = (None,)
                    for mode in modes:
                        if self._stop.is_set():
                            break
                        futures = [album_pool.submit(self._do_album, key, upload_pool, mode) for key in keys]
                        for future in as_completed(futures):
                            try:
                                future.result()
                            except Exception as e:  # noqa: BLE001
                                self._stop.set()
                                if first_error is None:
                                    first_error = e
            if first_error is not None:
                raise first_error
            self._emit("finished", stopped=self._stop.is_set())
        except ApiError as e:
            self._emit("error", message=str(e))
        except Exception as e:  # noqa: BLE001
            self._emit("error", message=f"{type(e).__name__}: {e}")

    def _prepare(self, keys, retry_failed_first=None):
        marks = ",".join("?" * len(keys))
        status_clause = "status IN ('new','uploaded')" if retry_failed_first is False else "status NOT IN ('done','duplicate')"
        row = self.db.query(
            f"SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM files "
            f"WHERE album_key IN ({marks}) AND {status_clause}", keys)[0]
        self.stats = Stats(row["n"], row["b"])

    def _do_album(self, key, pool, status=None):
        if self._stop.is_set():
            return
        title = self.db.query("SELECT title FROM albums WHERE key=?", (key,))[0]["title"]
        pending = self.db.pending_files(key, status)
        if not pending:
            return
        self._emit("album", title=title)
        album = self.db.query("SELECT album_id, share_url FROM albums WHERE key=?", (key,))[0]
        album_id = album["album_id"]
        if not album_id:
            album_id = self.api.create_album(title)
            self.db.set_album(key, album_id)
        with self._share_lock:
            if self.share and not album["share_url"]:
                try:
                    self.db.set_share(key, self.api.share_album(album_id))
                    self._emit("shared", title=title)
                except ApiError as e:
                    # sharing scope was discontinued by Google; keep uploading without a link
                    self._emit("log", message=f"Share link creation failed for {title}: {e}")
                    self.share = False

        for i in range(0, len(pending), BATCH_LIMIT):
            if not self._wait_if_paused():
                return
            chunk = pending[i:i + BATCH_LIMIT]
            ready = []
            todo = []
            for f in chunk:
                fresh = f["status"] == "uploaded" and f["upload_token"] and \
                    time.time() - (f["updated"] or 0) < TOKEN_MAX_AGE
                if fresh:
                    ready.append(f)
                    self.stats.add_bytes(f["size"])
                else:
                    todo.append(f)
            for f, token in zip(todo, pool.map(lambda f: self._upload_one(f, title), todo)):
                if token:
                    ready.append({**dict(f), "upload_token": token})
            if ready:
                self._commit(album_id, title, ready)
            self._emit("progress")
            if self._stop.is_set():
                return

    def _upload_one(self, f, album=""):
        if not self._wait_if_paused():
            return None
        path = f["path"]
        fp = FileProgress(path, f["size"], album)

        def on_bytes(n):
            fp.add(n)
            self.stats.add_bytes(n)

        try:
            if not os.path.isfile(path):
                raise ApiError("file missing")
            with self._files_lock:
                self._file_progress[path] = fp
            token = self.api.upload_bytes(path, on_bytes=on_bytes)
        except (ApiError, OSError) as e:
            if isinstance(e, ApiError) and e.fatal:
                self._stop.set()
            self.db.mark(path, "failed", error=str(e))
            self.stats.add_files_failed()
            self._record_file_result(False)
            self._emit("log", message=f"Upload failed for {path}: {type(e).__name__}: {e}")
            return None
        finally:
            fp.finished = time.time()
        self.db.mark(path, "uploaded", token=token)
        return token

    def _commit(self, album_id, album_title, files):
        with self._commit_lock:
            self._commit_locked(album_id, album_title, files)

    def _commit_locked(self, album_id, album_title, files):
        by_token = {f["upload_token"]: f for f in files}
        items = [(f["upload_token"], os.path.basename(f["path"])) for f in files]
        try:
            results = self.api.batch_create(album_id, items)
        except ApiError as e:
            if e.fatal:
                self._stop.set()
            self._emit("log", message=f"Batch create failed for {len(files)} file(s): {e}")
            for f in files:  # keep the upload token, retried on next run
                self.db.mark(f["path"], "uploaded", token=f["upload_token"], error=str(e))
                self._record_file_result(False)
            return
        committed = 0
        for token, f in by_token.items():
            ok, info = results.get(token, (False, "no result"))
            if ok:
                committed += 1
                self._record_file_result(True)
                self._log_uploaded_file(f, album_title, album_id, info)
                self.db.mark(f["path"], "done", media_id=info)
                self.stats.add_files_done()
            else:
                self._record_file_result(False)
                self.db.mark(f["path"], "failed", error=info)  # token unusable -> re-upload next time
                self.stats.add_files_failed()
                self._emit("log", message=f"Adding {f['path']} to album failed: {info}")
        self._emit(
            "info",
            message=f"Batch create completed for {album_title}: "
                    f"{committed}/{len(files)} file(s) added.",
        )

    def _log_uploaded_file(self, file, album_title, album_id, media_item_id):
        fields = ("uploaded_at", "source_path", "album_title", "album_id", "media_item_id")
        with self._log_lock:
            needs_header = not os.path.exists(self.upload_log_path) or os.path.getsize(self.upload_log_path) == 0
            with open(self.upload_log_path, "a", newline="", encoding="utf-8") as log:
                writer = csv.DictWriter(log, fieldnames=fields)
                if needs_header:
                    writer.writeheader()
                writer.writerow({
                    "uploaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "source_path": file["path"],
                    "album_title": album_title,
                    "album_id": album_id,
                    "media_item_id": media_item_id,
                })
