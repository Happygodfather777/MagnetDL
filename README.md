# MagnetDL

A small, ad-free magnet link / torrent downloader that runs natively on your computer.
No ads, no telemetry, no bundled offers. Built on **libtorrent** (the engine behind qBittorrent and Deluge) with a plain Tkinter window.

## Features
- Paste a magnet link (or open a `.torrent` file) and download
- Live progress, speed, peers/seeds and ETA
- Pause, resume, remove (with or without deleting files)
- Choose download folder; double-click a torrent to open its folder
- Download/upload speed limits, optional seeding after finish
- DHT, local peer discovery, UPnP/NAT-PMP port mapping
- Remembers your torrents between restarts (stored in `~/.magnetdl`)
- Only one copy runs at a time: opening a magnet link while MagnetDL is open adds it
  to the window that's already open

## Open magnet links with MagnetDL (Windows)
1. Open MagnetDL and click **Settings**.
2. Under **Magnet links**, click **Open magnet links with MagnetDL**.
3. Windows opens *Default apps* for MagnetDL. If MAGNET or .torrent shows another app,
   click it and choose **MagnetDL**.
4. The first time you click a magnet link, your browser asks to open MagnetDL:
   tick **Always allow** and click **Open**.

Click **Stop** in the same place to undo it. This only changes settings for your
Windows user. If you move MagnetDL.exe, it updates the setting the next time it starts.

## Sorting into your library + Plex
- Downloads go to `D:\Cinema` by default.
- When a download finishes, MagnetDL stops seeding it and moves it to the library drive (`F:\`):
  - TV episodes/seasons go into the matching show folder in `F:\Series` automatically,
    but only when the show name matches exactly. Near matches (e.g. "Severance" vs your
    "Severence" folder) and new shows wait for you in the Sort window.
  - Movies (and anything it isn't sure about) open the **Sort to library** window, where you pick
    the folder, e.g. `Movies › English › Thriller`. The choices come from the folders on the drive,
    so new genre folders appear automatically.
- **Sort to library…** also lists loose video files/folders already sitting in `D:\Cinema`.
- If `F:\` isn't plugged in, finished downloads wait and are sorted once it's connected.
- After moving, Plex is asked to scan just the folder that changed.
- Set it all up in **Settings**: library drive, auto-sort on/off, Plex address and token.
  The token is saved in `%USERPROFILE%\.magnetdl\settings.json` on this PC only.

## Safety check
Before anything is downloaded, MagnetDL checks the file list:
- **Blocked:** programs and scripts (.exe, .msi, .bat, .scr, .lnk, .js, .ps1…), fake names
  like `Movie.mp4.exe`, and hidden-character tricks.
- **Warned:** archives (.zip, .rar, .7z), disk images (.iso), .wmv/.asf "codec" scams,
  links and documents (.url, .html, .pdf), unusual file types, no video at all,
  or a video under 50 MB.
- Flagged downloads stay paused, with the risky files skipped, until you choose
  "Download videos only", "Remove download" or "Download everything".
- Magnet links download nothing until their file list has been checked.
- The Sort window also marks risky items in your download folder and leaves them unticked.
- Before anything is moved into your library automatically, the files on disk are checked
  again. Downloads where you approved risky files (programs, archives, disk images) are
  never sorted automatically.
This checks file names and types. It's not a virus scanner, so keep Windows Security turned on.

## Install & run

You need Python 3.9 – 3.13 (libtorrent has no Windows packages for 3.14 yet).

**Windows**
1. Install Python from python.org (tick "Add Python to PATH").
2. Open a terminal in this folder and run:
   ```
   pip install libtorrent
   python magnetdl.py
   ```
   Or just double-click `run_windows.bat`.

**macOS**
```
pip3 install libtorrent
python3 magnetdl.py
```

**Linux (Debian/Ubuntu)**
```
sudo apt install python3-tk
pip install libtorrent        # or: sudo apt install python3-libtorrent
python3 magnetdl.py
```

## Optional: make a standalone .exe / app

**Windows, one click:** double-click `build_windows.bat`. It installs what's needed,
builds the app, and leaves `MagnetDL.exe` in this folder.

**Manually (any OS):**
```
pip install pyinstaller
pyinstaller --onefile --windowed --name MagnetDL magnetdl.py
```
The program appears in `dist/`. You can also pass a magnet link as an argument:
`MagnetDL.exe "magnet:?xt=urn:btih:..."` — so you can register it as your system's magnet-link handler.

## Note
Only download content you have the right to download.

## Privacy
MagnetDL makes no network connections except BitTorrent traffic and, if you turn it on,
your own Plex server. Your settings (including the Plex token) are stored only on your PC in
`%USERPROFILE%\.magnetdl\settings.json` and are never part of this repository.

## Legal use
MagnetDL is a general-purpose BitTorrent client. It does not search for or index content.
Only download files you have the right to download, such as public-domain films from the
Internet Archive, Linux ISOs or your own files.

## Licence
MIT — see [LICENSE](LICENSE).
