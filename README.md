# Photo Uploader (Google Photos)

Tkinter GUI that uploads local photos to Google Photos, one album per month by EXIF date
(e.g. `2018. January`), resumable across sessions.

## Setup
1. `python -m venv .venv` and `.venv\Scripts\pip install -r requirements.txt`
2. In Google Cloud Console: create a project, enable **Photos Library API**, configure the OAuth consent
   screen (add yourself as test user), create an **OAuth client ID of type Desktop app**, download the JSON
   and save it as `data\client_secret.json`.
3. Run `.venv\Scripts\python main.py`, click **Sign in**.

## Build (Windows)
1. Install dependencies with `.venv\Scripts\pip install -r requirements-build.txt`.
2. Run `build.bat` from the repository root (or `powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1`).
3. The standalone app is created as `dist\PhotoUploader.exe`. On first run it creates `dist\data`;
   copy `client_secret.json` there before signing in. Keep the `data` folder beside the executable
   to preserve sign-in and upload progress between runs.

## Usage
Browse a folder (scanned recursively) -> albums and file counts appear -> select albums (or none = all) ->
**Start / Resume**. **Pause** lets in-flight files finish; **Stop** or closing the app keeps all progress in
`data\uploads.db`; next time just scan/Start again and only the remaining files are uploaded.
**Max speed (Mbps)** caps the total upload bandwidth (megabit/s, shared by all parallel uploads, `0` = unlimited);
it can be changed live while uploading and is remembered in `data\settings.json`.
Each successfully added photo is also recorded in `data\uploaded_files.csv`, including its Google Photos
media item ID and album ID, so the uploaded items can be identified for later removal.
The detailed application log is shown in the bottom-right panel and saved across sessions to `data\app.log`.

## How it works
Google Photos two-step upload: (1) POST raw bytes to `/v1/uploads` (parallel workers) -> upload token;
(2) `mediaItems:batchCreate` with `albumId`, 50 items per call. Albums are created by the app via the API
(required for the app to add to / share them). Upload tokens are stored, so an interrupted batch is
finished without re-uploading (tokens older than 20 h are re-uploaded).

## Sharing
New albums get a share link via `albums.share` (toggle in the GUI). The API cannot invite people by e-mail,
so use **Export share links** (clipboard + `data\shared_albums.txt`) and send the links; double-click an
album row to open its link.

## Notes
- Since 2025 the API only lets an app see/modify what it uploaded; duplicates are not detected across
  apps, but the local DB prevents re-uploading the same unchanged file.
- Images (jpg, png, gif, bmp, tif, webp, heic) and videos (mp4, mov, m4v, avi, mkv, 3gp, mpg, wmv, mts, webm).
  Image date comes from EXIF; video date from the filename prefix `YYYY.mm.dd HH.MM.SS`.
  Files without a usable date use file modified time.
- Files over 32 MB (large videos) are uploaded in 16 MB chunks via the resumable upload protocol.
- Test: `.venv\Scripts\python tests_e2e.py` (fake API).
