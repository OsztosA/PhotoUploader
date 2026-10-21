import csv, os, tempfile, time
from PIL import Image
from photouploader.db import Database
from photouploader.scanner import scan_folder
from photouploader.engine import ApiError, UploadEngine


class FakeApi:
    def __init__(self):
        self.albums, self.n = {}, 0
    def create_album(self, t):
        self.albums[t] = []; return "id-" + t
    def share_album(self, i): return "https://photos.google.com/share/" + i
    def upload_bytes(self, p, on_bytes=None):
        time.sleep(0.05)
        if on_bytes:
            on_bytes(os.path.getsize(p))
        return "tok-" + p
    def batch_create(self, aid, items):
        assert len(items) <= 50
        return {t: (True, "m" + t) for t, _ in items}


def test_flow():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "sub"))
    for i in range(120):
        im = Image.new("RGB", (4, 4))
        ex = Image.Exif()
        ex[306] = "2018:01:05 10:00:00" if i % 2 else "2019:03:09 10:00:00"
        im.save(os.path.join(d, "sub" if i % 3 else "", f"{i}.jpg"), exif=ex)
    db = Database(os.path.join(d, "t.db"))
    assert scan_folder(d, db) == 120
    summ = {r["title"]: r["total"] for r in db.album_summary()}
    assert summ == {"2018. January": 60, "2019. March": 60}, summ
    assert scan_folder(d, db) == 0

    events = []
    api = FakeApi()
    eng = UploadEngine(db, api, events.append, workers=4)
    keys = [r["key"] for r in db.album_summary()]
    eng.start(keys)
    time.sleep(0.4); eng.pause(); time.sleep(0.5)
    mid = eng.stats.files_done
    time.sleep(0.5)
    assert eng.stats.files_done == mid, "must not progress while paused"
    eng.stop(); eng._thread.join(5)
    done = db.query("SELECT COUNT(*) c FROM files WHERE status='done'")[0]["c"]
    assert 0 < done < 120, done
    # "next morning": new engine resumes
    eng2 = UploadEngine(db, api, events.append, workers=4)
    eng2.start(keys); eng2._thread.join(30)
    assert db.query("SELECT COUNT(*) c FROM files WHERE status='done'")[0]["c"] == 120
    assert all(r["share_url"] for r in db.album_summary())
    assert not [e for e in events if e["kind"] == "error"], events
    with open(os.path.join(d, "uploaded_files.csv"), newline="", encoding="utf-8") as f:
        uploaded = list(csv.DictReader(f))
    assert len(uploaded) == 120, len(uploaded)
    assert all(row["media_item_id"] and row["album_id"] and row["source_path"] for row in uploaded)
    assert {row["album_title"] for row in uploaded} == {"2018. January", "2019. March"}
    print("OK", done)


def test_upload_failure_is_logged():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "failed.jpg")
    with open(path, "wb") as f:
        f.write(b"photo")
    db = Database(os.path.join(d, "t.db"))
    db.upsert_scanned([(path, 5, time.time(), "2018-01-05", "2018-01", "2018. January")])

    class FailingApi(FakeApi):
        def upload_bytes(self, p, on_bytes=None):
            raise ApiError("network unavailable")

    events = []
    eng = UploadEngine(db, FailingApi(), events.append)
    eng._prepare(["2018-01"])
    eng._running.set()
    assert eng._upload_one(db.pending_files("2018-01")[0]) is None
    assert db.query("SELECT status FROM files WHERE path=?", (path,))[0]["status"] == "failed"
    assert any(
        e["kind"] == "log" and path in e["message"] and "network unavailable" in e["message"]
        for e in events
    ), events


test_flow()
test_upload_failure_is_logged()


def test_throttle_limits_speed():
    from photouploader.throttle import Throttle, ThrottledBody
    t = Throttle(8)  # 8 Mbps = 1 MB/s
    body = ThrottledBody(b"x" * 500_000, t)
    assert len(body) == 500_000
    start, total = time.monotonic(), 0
    while True:
        chunk = body.read(8192)
        if not chunk:
            break
        total += len(chunk)
    elapsed = time.monotonic() - start
    assert total == 500_000 and 0.4 < elapsed < 0.8, elapsed
    t.mbps = 0
    start = time.monotonic()
    body = ThrottledBody(b"x" * 500_000, t)
    while body.read(8192):
        pass
    assert time.monotonic() - start < 0.2


test_throttle_limits_speed()
