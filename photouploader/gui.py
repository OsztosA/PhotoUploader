"""Tkinter GUI for the Google Photos uploader."""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections import deque
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from .api import PhotosApi
from .auth import Auth
from .db import Database
from .engine import UploadEngine
from .scanner import scan_folder

APP_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ICON_PATH = os.path.join(APP_DIR, "images", "icon.jpg")

if getattr(sys, "frozen", False):
    DATA_DIR = os.path.join(os.path.dirname(sys.executable), "data")
else:
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def fmt_time(sec):
    if sec is None:
        return "--"
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Google Photos Uploader")
        with Image.open(ICON_PATH) as icon:
            self._window_icon = ImageTk.PhotoImage(icon.copy())
        self.iconphoto(True, self._window_icon)
        self.geometry("980x680")
        os.makedirs(DATA_DIR, exist_ok=True)
        self.db = Database(os.path.join(DATA_DIR, "uploads.db"))
        self.auth = Auth(os.path.join(DATA_DIR, "client_secret.json"), os.path.join(DATA_DIR, "token.json"))
        self.api = PhotosApi(self.auth)
        self.events = queue.Queue()
        self.engine = None
        self.uploading_albums = set()
        self.committing_albums = set()
        self.scanning = False
        self.rescanning = False
        self.log_path = os.path.join(DATA_DIR, "app.log")
        self.settings_path = os.path.join(DATA_DIR, "settings.json")
        self._build()
        self._load_log()
        self.refresh_albums()
        self.update_auth_state()
        self.log_msg("Application started.")
        self.after(500, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.LabelFrame(self, text="1. Google account (OAuth2)")
        top.pack(fill="x", **pad)
        self.auth_lbl = ttk.Label(top, text="")
        self.auth_lbl.pack(side="left", padx=8, pady=6)
        self.sign_btn = ttk.Button(top, text="Sign in", command=self.sign_in)
        self.sign_btn.pack(side="right", padx=8)
        self.out_btn = ttk.Button(top, text="Sign out", command=self.sign_out)
        self.out_btn.pack(side="right")

        fol = ttk.LabelFrame(self, text="2. Folder (scanned recursively)")
        fol.pack(fill="x", **pad)
        self.folder = tk.StringVar(value=self._load_settings().get("folder", ""))
        self.folder.trace_add("write", lambda *_: self._save_setting("folder", lambda: self.folder.get().strip()))
        ttk.Entry(fol, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=8, pady=6)
        ttk.Button(fol, text="Browse...", command=self.browse).pack(side="left")
        self.scan_btn = ttk.Button(fol, text="Scan", command=self.scan)
        self.scan_btn.pack(side="left", padx=8)
        self.rescan_btn = ttk.Button(fol, text="Rescan unprocessed", command=lambda: self.scan(rescan=True))
        self.rescan_btn.pack(side="left", padx=(0, 8))

        alb = ttk.LabelFrame(self, text="3. Albums (one per month, by EXIF date) - select albums to upload, none = all")
        alb.pack(fill="both", expand=True, **pad)
        cols = ("files", "done", "failed", "size", "left", "shared")
        self.tree = ttk.Treeview(alb, columns=cols, show="tree headings", selectmode="extended", height=8)
        self.tree.tag_configure("uploading", background="#fff2cc", foreground="#7a4b00")
        self.tree.tag_configure("committing", background="#d9eaf7", foreground="#164a6e")
        self.tree.heading("#0", text="Album")
        self.tree.column("#0", width=160)
        for c, t, w in (("files", "Files", 70), ("done", "Uploaded", 80), ("failed", "Failed", 70),
                        ("size", "Size", 90), ("left", "Remaining", 90), ("shared", "Share link", 300)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c == "shared" else "e")
        sb = ttk.Scrollbar(alb, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self._open_share)

        ctl = ttk.LabelFrame(self, text="4. Upload")
        ctl.pack(fill="x", **pad)
        ttk.Label(ctl, text="Parallel uploads:").pack(side="left", padx=(8, 2))
        self.workers = tk.IntVar(value=self._load_settings().get("workers", 4))
        self.workers.trace_add("write", lambda *_: self._save_setting("workers", self.workers.get))
        ttk.Spinbox(ctl, from_=1, to=16, width=4, textvariable=self.workers).pack(side="left")
        ttk.Label(ctl, text="Max speed (Mbps, 0 = unlimited):").pack(side="left", padx=(12, 2))
        self.max_mbps = tk.StringVar(value=self._load_settings().get("max_mbps", "0"))
        self._apply_speed_limit()
        self.max_mbps.trace_add("write", self._apply_speed_limit)
        ttk.Spinbox(ctl, from_=0, to=10000, increment=1, width=6, textvariable=self.max_mbps).pack(side="left")
        self.share = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Create share link for new albums", variable=self.share).pack(side="left", padx=12)
        ttk.Button(ctl, text="Export share links", command=self.export_links).pack(side="right", padx=8)
        self.stop_btn = ttk.Button(ctl, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.pause_btn = ttk.Button(ctl, text="Pause", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="right", padx=4)
        self.start_btn = ttk.Button(ctl, text="Start / Resume", command=self.start)
        self.start_btn.pack(side="right")

        bottom = ttk.Frame(self)
        bottom.pack(fill="both", expand=True, **pad)

        status = ttk.LabelFrame(bottom, text="Status")
        status.pack(fill="x")
        log_panel = ttk.LabelFrame(bottom, text=r"Detailed app log (saved to data\app.log)")
        log_panel.pack(fill="both", expand=True, pady=(4, 0))

        self.bar = ttk.Progressbar(status, maximum=100)
        self.bar.pack(fill="x", padx=8, pady=6)
        self.stat_lbl = ttk.Label(status, text="Idle")
        self.stat_lbl.pack(anchor="w", padx=8)
        self.threads_frame = ttk.Frame(status)
        self.threads_frame.pack(fill="x", padx=8, pady=(0, 4))
        self.thread_rows = []
        self.slot_of = {}
        ttk.Style().configure("Small.Horizontal.TProgressbar", thickness=6)
        self.small_font = ("TkDefaultFont", 7)

        self.log = tk.Text(log_panel, height=8, state="disabled", wrap="word")
        log_scroll = ttk.Scrollbar(log_panel, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        log_scroll.pack(side="right", fill="y", padx=(0, 8), pady=6)

    def _load_settings(self):
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _apply_speed_limit(self, *_):
        try:
            value = float(self.max_mbps.get())
        except ValueError:
            return  # keep the previous limit while the field holds invalid text
        self.api.throttle.mbps = value
        settings = self._load_settings()
        settings["max_mbps"] = self.max_mbps.get().strip()
        self._write_settings(settings)

    def _write_settings(self, settings):
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)

    def _save_setting(self, key, getter):
        try:
            value = getter()
        except tk.TclError:
            return  # field holds invalid text while typing
        settings = self._load_settings()
        settings[key] = value
        self._write_settings(settings)

    def log_msg(self, msg):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {msg}"
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _load_log(self):
        if not os.path.isfile(self.log_path):
            return
        with open(self.log_path, "r", encoding="utf-8") as f:
            lines = deque(f, maxlen=1000)
        self.log.configure(state="normal")
        self.log.insert("end", "".join(lines))
        self.log.see("end")
        self.log.configure(state="disabled")

    def update_auth_state(self):
        if not self.auth.has_client_secret():
            text = f"Missing OAuth client file: {self.auth.client_secret_path} (see README)"
        elif self.auth.is_signed_in():
            text = "Signed in"
        else:
            text = "Not signed in"
        self.auth_lbl.config(text=text)
        self.sign_btn.config(state="normal" if self.auth.has_client_secret() else "disabled")
        self.out_btn.config(state="normal" if self.auth.is_signed_in() else "disabled")

    def sign_in(self):
        self.log_msg("Opening browser for Google sign-in...")

        def work():
            try:
                self.auth.sign_in()
                self.events.put({"kind": "log", "message": "Signed in."})
            except Exception as e:  # noqa: BLE001
                self.events.put({"kind": "log", "message": f"Sign-in failed: {e}"})
            self.events.put({"kind": "auth"})
        threading.Thread(target=work, daemon=True).start()

    def sign_out(self):
        self.auth.sign_out()
        self.update_auth_state()

    def browse(self):
        d = filedialog.askdirectory()
        if d:
            self.folder.set(d)
            self.scan()

    def scan(self, rescan=False):
        folder = self.folder.get().strip()
        if not os.path.isdir(folder) or self.scanning:
            return
        self.scanning = True
        self.rescanning = rescan
        self.scan_btn.config(state="disabled")
        self.rescan_btn.config(state="disabled")
        self.log_msg(f"{'Rescanning unprocessed albums in' if rescan else 'Scanning'} {folder} ...")

        def work():
            n = scan_folder(folder, self.db, rescan=rescan, progress=lambda stage, done, total: self.events.put(
                {"kind": "scanning", "stage": stage, "done": done, "total": total}))
            self.events.put({"kind": "scanned", "count": n})
        threading.Thread(target=work, daemon=True).start()

    def refresh_albums(self):
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for r in self.db.album_summary():
            done, failed = r["done"] or 0, r["failed"] or 0
            self.tree.insert("", "end", iid=r["key"], text=r["title"], values=(
                r["total"], done, failed, fmt_size(r["bytes"]), r["total"] - done, r["share_url"] or ""),
                tags=("committing",) if r["key"] in self.committing_albums else
                     ("uploading",) if r["key"] in self.uploading_albums else ())
        keep = [k for k in sel if self.tree.exists(k)]
        if keep:
            self.tree.selection_set(keep)

    def _open_share(self, _e):
        item = self.tree.focus()
        if item:
            url = self.tree.set(item, "shared")
            if url:
                webbrowser.open(url)

    def export_links(self):
        rows = [r for r in self.db.album_summary() if r["share_url"]]
        if not rows:
            messagebox.showinfo("Share links", "No shared albums yet.")
            return
        text = "\n".join(f"{r['title']}\t{r['share_url']}" for r in rows)
        path = os.path.join(DATA_DIR, "shared_albums.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("Share links", f"{len(rows)} links copied to clipboard and saved to\n{path}\n\n"
                            "Send them to the people you want to share with.")

    def start(self):
        if self.engine and self.engine.active:
            if not self.engine._running.is_set():
                self.toggle_pause()
            return
        if not self.auth.is_signed_in():
            messagebox.showwarning("Sign in", "Please sign in to Google first.")
            return
        selected = set(self.tree.selection())
        rows = [r for r in self.db.album_summary()
                if (not selected or r["key"] in selected) and r["total"] - (r["done"] or 0) > 0]
        rows.sort(key=lambda r: (not bool(r["done"]), r["key"]))
        keys = [r["key"] for r in rows]
        if not keys:
            if selected:
                messagebox.showinfo("Nothing to do", f"The {len(selected)} selected album(s) have no pending files. "
                                    "Deselect them (Ctrl+click) to upload all albums.")
            else:
                messagebox.showinfo("Nothing to do", "No pending files. Browse a folder and scan first.")
            return
        marks = ",".join("?" * len(keys))
        failed = self.db.query(
            f"SELECT COUNT(*) n FROM files WHERE album_key IN ({marks}) AND status='failed'", keys)[0]["n"]
        retry_failed_first = False
        if failed:
            retry_failed_first = messagebox.askyesno(
                "Retry failed uploads",
                f"There are {failed} failed upload(s). Retry them first?\n\n"
                "Choosing No skips failed uploads for this run.")
        self.engine = UploadEngine(self.db, self.api, self.events.put, workers=self.workers.get(),
                                   share=self.share.get())
        self.engine.start(keys, retry_failed_first=retry_failed_first)
        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="normal", text="Pause")
        self.stop_btn.config(state="normal")
        self.log_msg(f"Upload started: {len(keys)} album(s)" + (" (selected only)." if selected else "."))

    def toggle_pause(self):
        if not self.engine or not self.engine.active:
            return
        if self.engine._running.is_set():
            self.engine.pause()
            self.pause_btn.config(text="Resume")
            self.log_msg("Pausing (in-flight files finish first)...")
        else:
            self.engine.resume()
            self.pause_btn.config(text="Pause")
            self.log_msg("Resumed.")

    def stop(self):
        if self.engine:
            self.engine.stop()
            self.log_msg("Stopping...")

    def _tick(self):
        refresh = False
        try:
            while True:
                ev = self.events.get_nowait()
                k = ev["kind"]
                if k in ("log", "info"):
                    self.log_msg(ev["message"])
                elif k == "auth":
                    self.update_auth_state()
                elif k == "scanning":
                    label = "Rescanning" if self.rescanning else "Scanning"
                    if ev["stage"] == "hashing":
                        self.stat_lbl.config(text=f"{label}: hashing {ev['done']}/{ev['total']} files for duplicates")
                        self.bar.config(mode="determinate")
                        self.bar["value"] = 100 * ev["done"] / ev["total"] if ev["total"] else 100
                    else:
                        self.stat_lbl.config(text=f"{label}: {ev['done']} files checked")
                        self.bar.config(mode="indeterminate")
                        self.bar.step(5)
                elif k == "scanned":
                    self.scanning = False
                    self.bar.config(mode="determinate")
                    self.bar["value"] = 0
                    self.scan_btn.config(state="normal")
                    self.rescan_btn.config(state="normal")
                    dups = self.db.query("SELECT COUNT(*) n FROM files WHERE status='duplicate'")[0]["n"]
                    self.log_msg(f"Scan finished: {ev['count']} new/changed files, {dups} duplicates skipped.")
                    self.stat_lbl.config(text="Idle")
                    refresh = True
                elif k == "album":
                    self.uploading_albums.add(ev["key"])
                    self.log_msg(f"Album: {ev['title']}")
                    refresh = True
                elif k == "album_done":
                    self.uploading_albums.discard(ev["key"])
                    refresh = True
                elif k == "commit_start":
                    self.committing_albums.add(ev["key"])
                    refresh = True
                elif k == "commit_done":
                    self.committing_albums.discard(ev["key"])
                    refresh = True
                elif k == "shared":
                    self.log_msg(f"Share link created for {ev['title']}")
                    refresh = True
                elif k == "progress":
                    refresh = True
                elif k == "error":
                    self.log_msg(f"ERROR: {ev['message']}")
                    self._finished()
                    refresh = True
                elif k == "finished":
                    self.log_msg("Stopped - progress saved, press Start to resume." if ev["stopped"]
                                 else "All done.")
                    self._finished()
                    refresh = True
        except queue.Empty:
            pass
        if self.engine and self.engine.active and self.engine.stats:
            s = self.engine.stats
            self.bar["value"] = 100 * s.bytes_done / s.total_bytes if s.total_bytes else 100
            cooldown = self.engine.cooldown_remaining
            if not self.engine._running.is_set():
                state = "PAUSED"
            elif cooldown > 0:
                state = f"Cooling down ({max(1, int(cooldown))}s)"
            else:
                state = "Uploading"
            self.stat_lbl.config(text=(
                f"{state}: {s.files_done}/{s.total_files} files ({s.files_failed} failed) | "
                f"{fmt_size(s.bytes_done)} / {fmt_size(s.total_bytes)} | "
                f"{fmt_size(s.speed())}/s | ETA {fmt_time(s.eta())} | elapsed {fmt_time(time.time() - s.start)}"))
            self._update_thread_rows(self.engine.file_progress())
        else:
            self._update_thread_rows([])
        if refresh:
            self.refresh_albums()
        self.after(500, self._tick)

    def _update_thread_rows(self, files):
        # fixed number of slots (2 x parallel uploads) so the window doesn't resize
        count = 2 * (self.engine.workers if self.engine else self.workers.get())
        if len(self.thread_rows) != count:
            for slot in self.thread_rows:
                slot[0].destroy()
            self.thread_rows = []
            self.slot_of = {}
            for _ in range(count):
                row = ttk.Frame(self.threads_frame)
                row.pack(fill="x")
                lbl = ttk.Label(row, text=" ", font=self.small_font)
                lbl.pack(side="left")
                spd = ttk.Label(row, text="", font=self.small_font, width=12, anchor="e")
                spd.pack(side="right")
                bar = ttk.Progressbar(row, maximum=100, style="Small.Horizontal.TProgressbar", length=200)
                bar.pack(side="right", padx=6)
                self.thread_rows.append((row, lbl, bar, spd))
        current = {fp.path: fp for fp in files}
        self.slot_of = {p: i for p, i in self.slot_of.items() if p in current}
        used = set(self.slot_of.values())
        free = (i for i in range(count) if i not in used)
        for path in current:
            if path not in self.slot_of:
                i = next(free, None)
                if i is not None:
                    self.slot_of[path] = i
        by_slot = {i: current[p] for p, i in self.slot_of.items()}
        for i, (_row, lbl, bar, spd) in enumerate(self.thread_rows):
            fp = by_slot.get(i)
            if fp is None:
                lbl.config(text=" ")
                bar["value"] = 0
                spd.config(text="")
            else:
                lbl.config(text=f"[{fp.album}] {os.path.basename(fp.path)}")
                bar["value"] = 100 * fp.done / fp.size if fp.size else 0
                spd.config(text=f"{fmt_size(fp.speed())}/s")

    def _finished(self):
        self.uploading_albums.clear()
        self.committing_albums.clear()
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled", text="Pause")
        self.stop_btn.config(state="disabled")

    def _close(self):
        if self.engine and self.engine.active:
            self.engine.stop()
        self.destroy()


def main():
    App().mainloop()
