"""Thin Google Photos Library API client (upload bytes -> batchCreate into album)."""
import mimetypes
import os
import time
import urllib.parse

import requests

from .throttle import Throttle, ThrottledBody

BASE = "https://photoslibrary.googleapis.com/v1"
UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
BATCH_LIMIT = 50  # API maximum per batchCreate
RESUMABLE_THRESHOLD = 32 * 1024 * 1024
CHUNK_SIZE = 16 * 1024 * 1024  # rounded down to the server's chunk granularity


class ApiError(Exception):
    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


class PhotosApi:
    def __init__(self, auth):
        self.auth = auth
        self.session = requests.Session()
        self.throttle = Throttle()

    def _request(self, method, url, retries=6, **kw):
        delay = 2
        base_headers = kw.pop("headers", {})
        payload = kw.pop("data", None)
        for attempt in range(retries):
            headers = dict(base_headers)
            headers["Authorization"] = f"Bearer {self.auth.token()}"
            try:
                body = ThrottledBody(payload, self.throttle) if payload else payload
                r = self.session.request(method, url, headers=headers, timeout=(15, 300), data=body, **kw)
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise ApiError(f"Network error: {e}")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(float(r.headers.get("Retry-After", delay)))
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 401 and attempt < retries - 1:
                self.auth.invalidate()
                continue
            if not r.ok:
                raise ApiError(f"HTTP {r.status_code}: {r.text[:300]}", fatal=r.status_code in (401, 403))
            return r
        raise ApiError("Retries exhausted")

    def create_album(self, title):
        r = self._request("POST", f"{BASE}/albums", json={"album": {"title": title}})
        return r.json()["id"]

    def share_album(self, album_id):
        r = self._request("POST", f"{BASE}/albums/{album_id}:share",
                          json={"sharedAlbumOptions": {"isCollaborative": False, "isCommentable": True}})
        return r.json()["shareInfo"]["shareableUrl"]

    def upload_bytes(self, path, on_bytes=None):
        """Step 1: upload bytes, returns an upload token (valid ~1 day).
        Small files go in one raw request; large files (videos) use the resumable protocol in chunks."""
        name = os.path.basename(path)
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        size = os.path.getsize(path)
        if size > RESUMABLE_THRESHOLD:
            return self._upload_resumable(path, name, mime, size, on_bytes)
        with open(path, "rb") as f:
            data = f.read()
        r = self._request("POST", UPLOAD_URL, data=data, headers={
            "Content-Type": "application/octet-stream",
            "X-Goog-Upload-Content-Type": mime,
            "X-Goog-Upload-Protocol": "raw",
            "X-Goog-Upload-File-Name": urllib.parse.quote(name),
        })
        if on_bytes:
            on_bytes(size)
        return r.text.strip()

    def _upload_resumable(self, path, name, mime, size, on_bytes):
        r = self._request("POST", UPLOAD_URL, data=b"", headers={
            "Content-Length": "0",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Content-Type": mime,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Raw-Size": str(size),
            "X-Goog-Upload-File-Name": urllib.parse.quote(name),
        })
        url = r.headers["X-Goog-Upload-URL"]
        granularity = int(r.headers.get("X-Goog-Upload-Chunk-Granularity", 262144))
        chunk = max(granularity, CHUNK_SIZE // granularity * granularity)
        offset = 0
        with open(path, "rb") as f:
            while True:
                f.seek(offset)
                data = f.read(chunk)
                last = offset + len(data) >= size
                try:
                    r = self._request("POST", url, data=data, headers={
                        "X-Goog-Upload-Command": "upload, finalize" if last else "upload",
                        "X-Goog-Upload-Offset": str(offset),
                    })
                except ApiError as e:
                    if e.fatal:
                        raise
                    received = self._query_offset(url)
                    if received is None:
                        raise
                    if received == size:  # finalize had succeeded server-side; re-finalize returns the token
                        offset = received
                    elif received != offset:
                        if on_bytes and received > offset:
                            on_bytes(received - offset)
                        offset = received
                        continue
                    else:
                        raise
                    r = self._request("POST", url, data=b"", headers={
                        "X-Goog-Upload-Command": "finalize",
                        "X-Goog-Upload-Offset": str(size),
                    })
                    return r.text.strip()
                if on_bytes:
                    on_bytes(len(data))
                offset += len(data)
                if last:
                    return r.text.strip()

    def _query_offset(self, url):
        try:
            r = self._request("POST", url, data=b"", retries=3, headers={
                "Content-Length": "0", "X-Goog-Upload-Command": "query"})
            return int(r.headers["X-Goog-Upload-Size-Received"])
        except (ApiError, KeyError, ValueError):
            return None

    def batch_create(self, album_id, items):
        """Step 2: items = [(token, filename)]. Returns {token: (ok, media_id_or_error)}."""
        body = {"albumId": album_id, "newMediaItems": [
            {"description": "", "simpleMediaItem": {"uploadToken": t, "fileName": n}} for t, n in items]}
        r = self._request("POST", f"{BASE}/mediaItems:batchCreate", json=body)
        out = {}
        for res in r.json().get("newMediaItemResults", []):
            st = res.get("status", {})
            ok = "mediaItem" in res and not st.get("code")
            out[res.get("uploadToken")] = (ok, res["mediaItem"]["id"] if ok else st.get("message", "unknown error"))
        return out
