#!/usr/bin/env python3
"""
MagnetDL - a tiny, ad-free magnet/torrent downloader.

Engine: libtorrent (the same engine used by qBittorrent/Deluge)
GUI:    Tkinter (ships with Python on Windows/macOS; on Linux install python3-tk)

Extras: sorts finished downloads into a media library (Movies/Series/...)
        and asks Plex to scan the folder that changed.

No ads, no telemetry, no bundled extras. Everything stays on your machine.
"""

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import libtorrent as lt
except ImportError:
    print("libtorrent is missing. Install it with:  pip install libtorrent")
    sys.exit(1)

import library
import safety

APP_NAME = "MagnetDL"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".magnetdl")
RESUME_DIR = os.path.join(CONFIG_DIR, "resume")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
REVIEWED_FILE = os.path.join(CONFIG_DIR, "safety_decisions.json")
DEFAULT_SAVE = os.path.join(os.path.expanduser("~"), "Downloads")
if sys.platform.startswith("win"):
    DEFAULT_CINEMA, DEFAULT_LIBRARY = "D:\\Cinema", "F:\\"
else:
    DEFAULT_CINEMA, DEFAULT_LIBRARY = DEFAULT_SAVE, ""

STATE_NAMES = {
    lt.torrent_status.checking_files: "Checking",
    lt.torrent_status.downloading_metadata: "Fetching metadata",
    lt.torrent_status.downloading: "Downloading",
    lt.torrent_status.finished: "Finished",
    lt.torrent_status.seeding: "Seeding",
    lt.torrent_status.checking_resume_data: "Resuming",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def human_eta(seconds: float) -> str:
    if seconds <= 0 or seconds == float("inf"):
        return "∞"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 99:
        return ">99h"
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def load_settings() -> dict:
    defaults = {
        "save_path": DEFAULT_CINEMA if os.path.isdir(DEFAULT_CINEMA) else DEFAULT_SAVE,
        "download_limit_kbps": 0,  # 0 = unlimited
        "upload_limit_kbps": 0,
        "seed_after_finish": True,
        "listen_port": 6881,
        # library sorting
        "library_root": DEFAULT_LIBRARY,
        "auto_sort": True,
        # plex
        "plex_enabled": True,
        "plex_url": "http://127.0.0.1:32400",
        "plex_token": "",
    }
    stored = {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        pass
    # First run of this version: switch downloads to the Cinema folder if it exists
    if "library_root" not in stored and os.path.isdir(DEFAULT_CINEMA):
        stored["save_path"] = DEFAULT_CINEMA
    defaults.update(stored)
    return defaults


def save_settings(settings: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


# --------------------------------------------------------------------------- #
# One MagnetDL at a time
# --------------------------------------------------------------------------- #
# A second copy (e.g. started by clicking a magnet link) hands its magnet link
# or .torrent path to the running one through a small "inbox" folder and quits.
# Nothing goes over the network for this.
INBOX_DIR = os.path.join(CONFIG_DIR, "inbox")
LOCK_FILE = os.path.join(CONFIG_DIR, "running.lock")


def acquire_single_instance():
    """An open lock file if no other MagnetDL is running, else None.
    The lock is released automatically when the program exits."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    f = open(LOCK_FILE, "a+")
    try:
        if sys.platform.startswith("win"):
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def forward_to_running(args):
    """Pass magnet links / .torrent paths to the MagnetDL that's already open."""
    os.makedirs(INBOX_DIR, exist_ok=True)
    items = [a for a in args if a.strip()] or ["show"]
    for i, text in enumerate(items):
        if not text.lower().startswith("magnet:") and text != "show":
            text = os.path.abspath(text)
        base = os.path.join(INBOX_DIR, f"{time.time_ns()}-{os.getpid()}-{i}")
        with open(base + ".tmp", "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(base + ".tmp", base + ".txt")  # appears complete or not at all


def read_inbox():
    """Items handed over by other copies, oldest first (and removed from the inbox)."""
    out = []
    try:
        names = sorted(n for n in os.listdir(INBOX_DIR) if n.endswith(".txt"))
    except OSError:
        return out
    for n in names:
        path = os.path.join(INBOX_DIR, n)
        try:
            with open(path, "r", encoding="utf-8") as f:
                out.append(f.read().strip())
            os.remove(path)
        except OSError:
            pass
    return [x for x in out if x]


# --------------------------------------------------------------------------- #
# Windows: open magnet links and .torrent files with MagnetDL
# --------------------------------------------------------------------------- #
# Everything goes under HKEY_CURRENT_USER (this user only, no admin rights).
_CLASSES = r"Software\Classes"
_CAPS = r"Software\MagnetDL\Capabilities"


def handler_command() -> str:
    """The command Windows should run for a magnet link or .torrent file."""
    if getattr(sys, "frozen", False):  # the built MagnetDL.exe
        return f'"{sys.executable}" "%1"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw  # no console window
    return f'"{exe}" "{os.path.abspath(__file__)}" "%1"'


def register_handlers(winreg=None) -> None:
    """Offer MagnetDL for magnet: links and .torrent files (current user only)."""
    if winreg is None:
        import winreg
    cmd = handler_command()
    icon = f'"{sys.executable}",0'
    hk = winreg.HKEY_CURRENT_USER

    def put(path, values):
        with winreg.CreateKeyEx(hk, path, 0, winreg.KEY_WRITE) as k:
            for name, val in values.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)

    for prog, title, url in (("MagnetDL.magnet", "Magnet link", True),
                             ("MagnetDL.torrent", "Torrent file", False)):
        put(rf"{_CLASSES}\{prog}", {"": title, **({"URL Protocol": ""} if url else {})})
        put(rf"{_CLASSES}\{prog}\DefaultIcon", {"": icon})
        put(rf"{_CLASSES}\{prog}\shell\open\command", {"": cmd})
    # The magnet: protocol itself (browsers look here)
    put(rf"{_CLASSES}\magnet", {"": "URL:Magnet link", "URL Protocol": ""})
    put(rf"{_CLASSES}\magnet\DefaultIcon", {"": icon})
    put(rf"{_CLASSES}\magnet\shell\open\command", {"": cmd})
    # Add to "Open with" for .torrent without taking over other programs
    put(rf"{_CLASSES}\.torrent\OpenWithProgids", {"MagnetDL.torrent": ""})
    # So MagnetDL appears in Settings > Apps > Default apps
    put(_CAPS, {"ApplicationName": APP_NAME,
                "ApplicationDescription": "Ad-free magnet link and torrent downloader"})
    put(_CAPS + r"\URLAssociations", {"magnet": "MagnetDL.magnet"})
    put(_CAPS + r"\FileAssociations", {".torrent": "MagnetDL.torrent"})
    put(r"Software\RegisteredApplications", {APP_NAME: _CAPS})
    _notify_assoc_changed()


def _delete_tree(winreg, hk, path):
    try:
        with winreg.OpenKey(hk, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(winreg, hk, path + "\\" + sub)
        winreg.DeleteKey(hk, path)
    except FileNotFoundError:
        pass


def _read_default(winreg, hk, path):
    try:
        with winreg.OpenKey(hk, path) as k:
            return winreg.QueryValueEx(k, "")[0]
    except OSError:
        return None


def unregister_handlers(winreg=None) -> None:
    """Undo register_handlers (leaves other programs' settings alone)."""
    if winreg is None:
        import winreg
    hk = winreg.HKEY_CURRENT_USER
    cmd = _read_default(winreg, hk, rf"{_CLASSES}\magnet\shell\open\command") or ""
    if "magnetdl" in cmd.lower():
        _delete_tree(winreg, hk, rf"{_CLASSES}\magnet")
    for prog in ("MagnetDL.magnet", "MagnetDL.torrent"):
        _delete_tree(winreg, hk, rf"{_CLASSES}\{prog}")
    try:
        with winreg.OpenKey(hk, rf"{_CLASSES}\.torrent\OpenWithProgids", 0,
                            winreg.KEY_WRITE) as k:
            winreg.DeleteValue(k, "MagnetDL.torrent")
    except OSError:
        pass
    try:
        with winreg.OpenKey(hk, r"Software\RegisteredApplications", 0, winreg.KEY_WRITE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except OSError:
        pass
    _delete_tree(winreg, hk, r"Software\MagnetDL")
    _notify_assoc_changed()


def handlers_registered(winreg=None) -> bool:
    if winreg is None:
        import winreg
    return _read_default(winreg, winreg.HKEY_CURRENT_USER,
                         rf"{_CLASSES}\MagnetDL.magnet\shell\open\command") is not None


def update_handlers_if_moved(winreg=None) -> bool:
    """If MagnetDL.exe was moved since registering, point Windows at the new place."""
    if winreg is None:
        import winreg
    current = _read_default(winreg, winreg.HKEY_CURRENT_USER,
                            rf"{_CLASSES}\MagnetDL.magnet\shell\open\command")
    if current is not None and current != handler_command():
        register_handlers(winreg)
        return True
    return False


def _notify_assoc_changed():
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
    except Exception:
        pass


class DuplicateTorrent(ValueError):
    pass


def _same_torrent(a, b) -> bool:
    """True if two lt.info_hash_t values share a v1 or v2 hash."""
    return bool((a.has_v1() and b.has_v1() and a.v1 == b.v1) or
                (a.has_v2() and b.has_v2() and a.v2 == b.v2))


def same_folder(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Torrent engine (no GUI code in here, so it can be reused or tested headless)
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, settings: dict):
        self.settings = settings
        port = int(settings.get("listen_port", 6881))
        self.session = lt.session({
            "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "user_agent": f"{APP_NAME}/1.2 libtorrent/{lt.__version__}",
            "alert_mask": lt.alert.category_t.error_notification
            | lt.alert.category_t.status_notification
            | lt.alert.category_t.storage_notification,
        })
        self.apply_limits()
        self.handles: dict[str, lt.torrent_handle] = {}
        self.lock = threading.Lock()
        self.finished: list[str] = []          # keys that just completed
        self.moving: dict[str, str] = {}       # key -> destination folder
        self.moved: list[tuple[str, str]] = []  # (name, new path) after a move
        self.reports = {}                       # key -> safety.Report awaiting a decision
        self.needs_review: list[str] = []       # keys the app should ask about
        self.reviewed = self._load_reviewed()   # key -> decision
        self.pending_saves = {}                 # key -> resume saves not yet written
        os.makedirs(RESUME_DIR, exist_ok=True)

    # -- limits ------------------------------------------------------------- #
    def apply_limits(self):
        self.session.apply_settings({
            "download_rate_limit": int(self.settings["download_limit_kbps"]) * 1024,
            "upload_rate_limit": int(self.settings["upload_limit_kbps"]) * 1024,
        })

    # -- adding ------------------------------------------------------------- #
    def add_magnet(self, uri: str, save_path: str) -> str:
        params = lt.parse_magnet_uri(uri.strip())
        params.save_path = save_path
        # Fetch the file list but download nothing until the safety check has run
        params.flags |= lt.torrent_flags.upload_mode
        return self._add(params)

    def add_torrent_file(self, path: str, save_path: str) -> str:
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(path)
        params.save_path = save_path
        # File list is known up front: start paused so nothing downloads before the check
        params.flags |= lt.torrent_flags.paused
        params.flags &= ~lt.torrent_flags.auto_managed
        key = self._add(params)
        if self.inspect(key) == safety.OK:
            self.resume(key)
        return key

    def _add(self, params, key=None) -> str:
        ih = params.ti.info_hashes() if params.ti is not None else params.info_hashes
        for k, h in list(self.handles.items()):
            if h.is_valid() and _same_torrent(h.info_hashes(), ih):
                raise DuplicateTorrent("This torrent is already in the list.")
        h = self.session.add_torrent(params)
        # The key is fixed at add time. Don't recompute it from the handle later:
        # a hybrid (v1+v2) torrent added from a v1 magnet gets a different
        # "best" hash once its file list arrives.
        key = key or str(h.info_hashes().get_best())
        with self.lock:
            self.handles[key] = h
        return key

    def key_of(self, handle):
        """Our key for a libtorrent handle (from an alert), or None if removed."""
        with self.lock:
            for k, h in self.handles.items():
                if h == handle:
                    return k
        return None

    # -- safety check ------------------------------------------------------- #
    def file_list(self, key):
        h = self.handles.get(key)
        ti = h.torrent_file() if h and h.is_valid() else None
        if ti is None:
            return None
        fs = ti.files()
        out = []
        for i in range(fs.num_files()):
            if fs.file_flags(i) & lt.file_storage.flag_pad_file:
                out.append(("", 0, i, True))  # padding, not a real file
            else:
                out.append((fs.file_path(i), fs.file_size(i), i, False))
        return out

    def _report(self, key):
        """Safety report for a torrent's file list, or None if it isn't known yet."""
        files = self.file_list(key)
        if files is None:
            return None
        real = [(p, s, i) for p, s, i, pad in files if not pad]
        report = safety.Report([(p, s) for p, s, _ in real])
        report.lt_index = [i for _, _, i in real]
        return report

    def inspect(self, key) -> str:
        """Check a download's files. Risky ones are skipped and the download
        is paused until the user decides. Returns the safety level."""
        h = self.handles[key]
        if key in self.reviewed:
            if self.reviewed[key] not in ("all", "all-risky"):
                # Re-apply "skip risky files" every time, whatever the saved
                # resume data says (it can be older than the decision).
                report = self._report(key)
                for idx in (report.flagged_indices if report else ()):
                    h.file_priority(report.lt_index[idx], 0)
            h.unset_flags(lt.torrent_flags.upload_mode)
            return safety.OK
        report = self._report(key)
        if report is None:
            return safety.OK  # no file list yet; checked when metadata arrives
        if report.level == safety.OK:
            self.reviewed[key] = "ok"
            self._save_reviewed()
            h.unset_flags(lt.torrent_flags.upload_mode)
            return safety.OK
        h.unset_flags(lt.torrent_flags.auto_managed)
        h.pause()
        for idx in report.flagged_indices:
            h.file_priority(report.lt_index[idx], 0)
        self.reports[key] = report
        if key not in self.needs_review:
            self.needs_review.append(key)
        return report.level

    def decide(self, key, choice):
        """choice: 'videos' (skip risky files), 'all', or 'remove'."""
        h = self.handles.get(key)
        report = self.reports.pop(key, None)
        if choice == "remove":
            self.remove(key, delete_files=True)
            self.reviewed.pop(key, None)
        elif h and h.is_valid():
            if choice == "all" and report:
                for idx in report.flagged_indices:
                    h.file_priority(report.lt_index[idx], 4)
            # Approving any flagged file (program, archive, disk image...) means
            # this download must never be sorted into the library automatically.
            self.reviewed[key] = "all-risky" if (choice == "all" and report
                                                 and report.items) else choice
            h.unset_flags(lt.torrent_flags.upload_mode)
            self.resume(key)
            self.request_save(key)  # store the new file choices straight away
        self._save_reviewed()

    def request_save(self, key):
        """Ask libtorrent for fresh resume data (written when the alert arrives)."""
        h = self.handles.get(key)
        if h and h.is_valid():
            h.save_resume_data(lt.torrent_handle.save_info_dict)
            self.pending_saves[key] = self.pending_saves.get(key, 0) + 1

    def _save_done(self, key):
        n = self.pending_saves.get(key, 0) - 1
        if n > 0:
            self.pending_saves[key] = n
        else:
            self.pending_saves.pop(key, None)

    def _load_reviewed(self):
        try:
            with open(REVIEWED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_reviewed(self):
        try:
            with open(REVIEWED_FILE, "w", encoding="utf-8") as f:
                json.dump(self.reviewed, f)
        except OSError:
            pass

    # -- control ------------------------------------------------------------ #
    def pause(self, key):
        h = self.handles.get(key)
        if h:
            h.unset_flags(lt.torrent_flags.auto_managed)
            h.pause()

    def resume(self, key):
        h = self.handles.get(key)
        if h:
            h.set_flags(lt.torrent_flags.auto_managed)
            h.resume()

    def remove(self, key, delete_files=False):
        with self.lock:
            h = self.handles.pop(key, None)
        if h:
            flags = lt.session.delete_files if delete_files else 0
            self.session.remove_torrent(h, flags)
        rf = os.path.join(RESUME_DIR, key + ".fastresume")
        if os.path.exists(rf):
            os.remove(rf)
        self.reports.pop(key, None)
        self.pending_saves.pop(key, None)
        if key in self.needs_review:
            self.needs_review.remove(key)
        if self.reviewed.pop(key, None) is not None:
            self._save_reviewed()

    def info(self, key):
        """(name, save_path) of a torrent, or (None, None)."""
        h = self.handles.get(key)
        if not h or not h.is_valid():
            return None, None
        s = h.status()
        return s.name, s.save_path

    def disk_report(self, key):
        """Safety report for the files of this torrent that are on disk now."""
        name, save = self.info(key)
        if not name:
            return safety.Report([])
        return safety.check_folder(os.path.join(save, name))

    def save_path_of(self, key):
        return self.info(key)[1]

    def is_complete(self, key):
        h = self.handles.get(key)
        return bool(h and h.is_valid() and h.status().is_finished)

    def move_to_library(self, key, dest_dir) -> str:
        """Stop seeding and move a finished torrent's files into dest_dir.
        Returns '' when started, or a reason it couldn't."""
        h = self.handles.get(key)
        if not h or not h.is_valid():
            return "it's no longer in the list"
        st = h.status()
        name = st.name
        if os.path.exists(os.path.join(dest_dir, name)):
            # Name clash: take it out of the session; the app then moves it as "name (2)"
            self.remove(key)
            return "clash:" + os.path.join(st.save_path, name)
        os.makedirs(dest_dir, exist_ok=True)
        h.unset_flags(lt.torrent_flags.auto_managed)
        h.pause()  # stop uploading right away
        self.moving[key] = dest_dir
        h.move_storage(dest_dir)
        return ""

    # -- status ------------------------------------------------------------- #
    def snapshot(self):
        rows = []
        with self.lock:
            items = list(self.handles.items())
        for key, h in items:
            if not h.is_valid():
                continue
            s = h.status()
            paused = bool(s.flags & lt.torrent_flags.paused)
            state = "Paused" if paused else STATE_NAMES.get(s.state, str(s.state))
            if key in self.moving:
                state = "Moving to library…"
            elif key in self.reports:
                state = ("⛔ Blocked: risky files" if self.reports[key].level == safety.DANGER_LEVEL
                         else "⚠ Check before downloading")
            if s.errc.value():
                state = "Error: " + s.errc.message()
            remaining = max(s.total_wanted - s.total_wanted_done, 0)
            eta = remaining / s.download_rate if s.download_rate > 0 else float("inf")
            rows.append({
                "key": key,
                "name": s.name or "(fetching metadata…)",
                "size": s.total_wanted,
                "progress": s.progress,
                "state": state,
                "down": s.download_rate,
                "up": s.upload_rate,
                "peers": s.num_peers,
                "seeds": s.num_seeds,
                "eta": 0 if s.is_seeding or s.is_finished else eta,
                "finished": s.is_finished,
            })
        return rows

    def handle_alerts(self):
        """Process engine events; returns list of human messages for the log."""
        msgs = []
        for a in self.session.pop_alerts():
            key = self.key_of(a.handle) if isinstance(a, lt.torrent_alert) else None
            if isinstance(a, lt.torrent_alert) and key is None:
                continue  # torrent was removed meanwhile
            if isinstance(a, lt.save_resume_data_alert):
                self._write_resume(a, key)
                self._save_done(key)
            elif isinstance(a, lt.save_resume_data_failed_alert):
                self._save_done(key)
            elif isinstance(a, lt.torrent_finished_alert):
                msgs.append(f"Finished: {a.handle.status().name}")
                self.request_save(key)
                if key not in self.moving:
                    self.finished.append(key)
                if not self.settings.get("seed_after_finish", True):
                    a.handle.unset_flags(lt.torrent_flags.auto_managed)
                    a.handle.pause()
            elif isinstance(a, lt.metadata_received_alert):
                name = a.handle.status().name
                msgs.append(f"Got metadata: {name}")
                if self.inspect(key) != safety.OK:
                    msgs.append(f"Safety check paused {name}: {self.reports[key].summary()}")
                self.request_save(key)
            elif isinstance(a, lt.storage_moved_alert):
                dest = self.moving.pop(key, None)
                if dest is not None:
                    name = a.handle.status().name
                    self.remove(key)  # files stay; seeding stops
                    self.moved.append((name, os.path.join(dest, name)))
                    msgs.append(f"Moved to library: {name}  →  {dest}")
            elif isinstance(a, lt.storage_moved_failed_alert):
                self.moving.pop(key, None)
                msgs.append(f"Couldn't move {a.handle.status().name}: {a.message()}")
            elif a.category() & lt.alert.category_t.error_notification:
                msgs.append(a.message())
        return msgs

    # -- persistence -------------------------------------------------------- #
    def _write_resume(self, alert, key=None):
        key = key or self.key_of(alert.handle)
        if key is None:
            return  # already removed
        data = lt.write_resume_data_buf(alert.params)
        with open(os.path.join(RESUME_DIR, key + ".fastresume"), "wb") as f:
            f.write(data)

    def load_saved(self):
        count = 0
        for fn in os.listdir(RESUME_DIR):
            if not fn.endswith(".fastresume"):
                continue
            try:
                with open(os.path.join(RESUME_DIR, fn), "rb") as f:
                    params = lt.read_resume_data(f.read())
                # Keep the key it was saved under, so safety decisions still match
                key = self._add(params, key=fn[:-len(".fastresume")])
                self.inspect(key)  # asks again if you closed the app before deciding
                count += 1
            except DuplicateTorrent:  # a second copy of a torrent already restored
                try:
                    os.remove(os.path.join(RESUME_DIR, fn))
                except OSError:
                    pass
            except Exception as e:  # corrupt resume file: skip it
                print("Could not restore", fn, e)
        return count

    def shutdown(self, timeout=5.0):
        self.session.pause()
        with self.lock:
            keys = [k for k, h in self.handles.items() if h.is_valid()]
        for k in keys:
            self.request_save(k)
        # Wait for every requested save, including ones asked for earlier, so the
        # newest state (file choices, paused or not) is what's on disk.
        deadline = time.time() + timeout
        while self.pending_saves and time.time() < deadline:
            self.session.wait_for_alert(500)
            for a in self.session.pop_alerts():
                if isinstance(a, (lt.save_resume_data_alert, lt.save_resume_data_failed_alert)):
                    key = self.key_of(a.handle)
                    if key is None:
                        continue
                    if isinstance(a, lt.save_resume_data_alert):
                        self._write_resume(a, key)
                    self._save_done(key)


# --------------------------------------------------------------------------- #
# Sort window
# --------------------------------------------------------------------------- #
class SortWindow(tk.Toplevel):
    """Lists finished downloads and loose files in the download folder,
    with a destination picker for each."""

    def __init__(self, app: "App", focus_keys=()):
        super().__init__(app)
        self.app = app
        self.title("Sort to library")
        self.geometry("900x460")
        self.minsize(640, 300)
        self.rows = []

        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        self.info = ttk.Label(top, text="")
        self.info.pack(side="left")
        ttk.Button(top, text="Refresh list", command=self.populate).pack(side="right")

        holder = ttk.Frame(self, padding=(10, 0, 10, 0))
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, highlightthickness=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.inner.columnconfigure(1, weight=1)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        ttk.Label(bottom, foreground="gray",
                  text="Ticked items move to the folder shown, stop seeding, "
                       "and Plex is asked to scan them.").pack(side="left")
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Move ticked", command=self.move).pack(side="right", padx=6)

        self.populate()

    def populate(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self.rows = []
        lib = self.app.library()
        if not lib.connected:
            self.info.configure(text=f"Library drive {lib.root} isn't connected. "
                                     "Plug it in and click Refresh list.")
            return

        items = []  # (kind, key_or_path, name)
        torrent_names = set()
        for r in self.app.engine.snapshot():
            name, _ = self.app.engine.info(r["key"])
            if name:
                torrent_names.add(name)
            if r["finished"] and r["key"] not in self.app.engine.moving:
                items.append(("torrent", r["key"], r["name"]))
        folder = self.app.settings["save_path"]
        for name in library.loose_items(folder, exclude_names=torrent_names):
            items.append(("file", os.path.join(folder, name), name))

        if not items:
            self.info.configure(text="Nothing to sort. Finished downloads and media files "
                                     f"in {folder} will show up here.")
            return
        self.info.configure(text=f"{len(items)} item(s) to sort. Library: {lib.root}")

        ttk.Label(self.inner, text="Move", foreground="gray").grid(row=0, column=0, sticky="w")
        ttk.Label(self.inner, text="Item", foreground="gray").grid(row=0, column=1, sticky="w")
        ttk.Label(self.inner, text="Destination", foreground="gray").grid(row=0, column=2, sticky="w")

        for i, (kind, ref, name) in enumerate(items, start=1):
            dests = lib.all_destinations(name)
            label, path, _ = lib.guess(name)
            if label and label not in [d[0] for d in dests]:
                dests.insert(0, (label, path))
            choices = dict(dests)
            risk = self._risk(kind, ref)
            tick = tk.BooleanVar(value=bool(label) and not risk)
            dest = tk.StringVar(value=label or "")
            ttk.Checkbutton(self.inner, variable=tick).grid(row=i, column=0, padx=(0, 6), pady=2)
            tag = "" if kind == "torrent" else "   (file in download folder)"
            cell = ttk.Frame(self.inner)
            cell.grid(row=i, column=1, sticky="w", pady=2)
            ttk.Label(cell, text=name + tag).pack(anchor="w")
            if risk:
                ttk.Label(cell, text="⚠ " + risk, foreground="#b42318",
                          wraplength=420).pack(anchor="w")
            box = ttk.Combobox(self.inner, textvariable=dest, values=list(choices),
                               state="readonly", width=48)
            box.grid(row=i, column=2, sticky="e", pady=2)
            if not label:
                box.set("Choose a folder…")
            box.bind("<<ComboboxSelected>>", lambda e, t=tick: t.set(True))
            self.rows.append((kind, ref, name, tick, dest, choices, risk))

    def _risk(self, kind, ref) -> str:
        """Short warning text if the item contains risky files, else ''."""
        if kind == "torrent":
            report = self.app.engine.disk_report(ref)
            if not report.items and self.app.engine.reviewed.get(ref) != "all-risky":
                return ""
        else:
            report = safety.check_folder(ref)
        if report.level == safety.OK:
            return ""
        names = [os.path.basename(p) for _, p, _, lvl, _ in report.items][:3]
        more = f" and {len(report.items) - 3} more" if len(report.items) > 3 else ""
        detail = f"Contains {', '.join(names)}{more}." if names else ""
        return " ".join(x for x in (detail, " ".join(report.notes)) if x)

    def move(self):
        count = 0
        for kind, ref, name, tick, dest, choices, risk in self.rows:
            if not tick.get():
                continue
            target = choices.get(dest.get())
            if not target:
                messagebox.showwarning("Sort to library",
                                       f"Choose a folder for:\n{name}", parent=self)
                return
            if risk and not messagebox.askyesno(
                    "Risky files",
                    f"{name}\n\n{risk}\n\nMove it into your library anyway? "
                    "Don't open any program files it contains.", icon="warning", parent=self):
                continue
            self.app.send_to_library(kind, ref, name, target)
            count += 1
        if count:
            self.app.log(f"Moving {count} item(s) to the library…")
            self.after(800, self.populate)


# --------------------------------------------------------------------------- #
# Safety review window
# --------------------------------------------------------------------------- #
class ReviewDialog(tk.Toplevel):
    """Shown when a download contains risky files. The download stays paused
    (with the risky files skipped) until a choice is made."""

    def __init__(self, app: "App", key: str):
        super().__init__(app)
        self.app, self.key = app, key
        report = app.engine.reports[key]
        name, _ = app.engine.info(key)
        danger = report.level == safety.DANGER_LEVEL
        self.title("Safety check")
        self.geometry("760x440")
        self.minsize(560, 320)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.later)

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        color = "#b42318" if danger else "#b54708"
        head = ("⛔ This download contains files that could harm your PC"
                if danger else "⚠ This download doesn't look like a normal video")
        ttk.Label(frm, text=head, foreground=color,
                  font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        ttk.Label(frm, text=name or "", foreground="gray").pack(anchor="w", pady=(2, 8))
        ttk.Label(frm, wraplength=720, text=(
            "It's paused and nothing risky has been downloaded. Movies and TV shows only "
            "need video and subtitle files; programs, scripts and archives in a movie "
            "download are a common way to spread malware.")).pack(anchor="w")

        for note in report.notes:
            ttk.Label(frm, text="•  " + note, wraplength=720).pack(anchor="w", pady=(6, 0))

        if report.items:
            box = ttk.Frame(frm)
            box.pack(fill="both", expand=True, pady=(10, 0))
            tree = ttk.Treeview(box, columns=("file", "size", "why"), show="headings", height=6)
            for col, text, w in (("file", "File", 280), ("size", "Size", 80), ("why", "Why", 340)):
                tree.heading(col, text=text)
                tree.column(col, width=w, anchor="e" if col == "size" else "w",
                            stretch=(col != "size"))
            tree.tag_configure("danger", foreground="#b42318")
            for _, path, size, level, reason in report.items:
                tree.insert("", "end", values=(path, human_size(size), reason),
                            tags=("danger",) if level == safety.DANGER_LEVEL else ())
            sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=sb.set)
            tree.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Remove download", command=lambda: self.choose("remove")
                   ).pack(side="right")
        if report.items and report.safe_video_count:
            ttk.Button(btns, text="Download videos only (recommended)",
                       command=lambda: self.choose("videos")).pack(side="right", padx=6)
        ttk.Button(btns, text="Download everything" if report.items else "Download anyway",
                   command=self.everything).pack(side="left")
        ttk.Button(btns, text="Decide later", command=self.later).pack(side="left", padx=6)
        self.lift()
        self.focus_force()

    def everything(self):
        report = self.app.engine.reports.get(self.key)
        if report and report.level == safety.DANGER_LEVEL:
            if not messagebox.askyesno(
                    "Download risky files?",
                    "This includes program or script files. Only continue if you know "
                    "exactly what they are, and never open them to 'play' the video.\n\n"
                    "Download everything anyway?", icon="warning", parent=self):
                return
        self.choose("all")

    def choose(self, choice):
        name, _ = self.app.engine.info(self.key)
        self.app.engine.decide(self.key, choice)
        msg = {"remove": "Removed (safety check)",
               "videos": "Downloading videos only, risky files skipped",
               "all": "Downloading everything (you approved risky files)"}[choice]
        self.app.log(f"{msg}: {name}")
        self.destroy()

    def later(self):
        self.app.log("Left paused for review. Select it and click Resume to decide.")
        self.destroy()


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    COLUMNS = ("name", "size", "progress", "state", "down", "up", "peers", "eta")
    HEADINGS = {
        "name": ("Name", 320, "w"),
        "size": ("Size", 80, "e"),
        "progress": ("Done", 70, "e"),
        "state": ("Status", 140, "w"),
        "down": ("Down", 90, "e"),
        "up": ("Up", 90, "e"),
        "peers": ("Peers (seeds)", 100, "center"),
        "eta": ("ETA", 80, "e"),
    }

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1080x580")
        self.minsize(780, 400)

        self.settings = load_settings()
        save_settings(self.settings)
        self.engine = Engine(self.settings)
        self.events = queue.Queue()     # messages from worker threads
        self.plex_paths = set()         # folders waiting for a Plex scan
        self.plex_due = 0.0
        self.waiting_for_drive = []     # finished keys to sort once the drive is back
        self.sort_window = None
        self.review_window = None

        self._build_ui()
        restored = self.engine.load_saved()
        if restored:
            self.log(f"Restored {restored} torrent(s) from last session.")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(500, self.refresh)
        self.after(10_000, self.check_drive)

        for arg in sys.argv[1:]:
            self._add_any(arg)

        # Keep "open magnet links with MagnetDL" working if MagnetDL.exe was moved
        if sys.platform.startswith("win") and getattr(sys, "frozen", False):
            try:
                if update_handlers_if_moved():
                    self.log("Updated the magnet link setting to this copy of MagnetDL.")
            except Exception:
                pass

    def library(self):
        return library.Library(self.settings.get("library_root", ""))

    # -- layout ------------------------------------------------------------- #
    def _build_ui(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names() and sys.platform.startswith("linux"):
            style.theme_use("clam")
        style.configure("Treeview", rowheight=24)

        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Magnet link:").pack(side="left")
        self.magnet_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.magnet_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda e: self.add_magnet())
        ttk.Button(top, text="Paste", command=self.paste).pack(side="left")
        ttk.Button(top, text="Add", command=self.add_magnet).pack(side="left", padx=(4, 0))
        ttk.Button(top, text="Open .torrent…", command=self.add_file).pack(side="left", padx=(4, 0))

        row = ttk.Frame(self, padding=(10, 0, 10, 6))
        row.pack(fill="x")
        ttk.Label(row, text="Save to:").pack(side="left")
        self.save_var = tk.StringVar(value=self.settings["save_path"])
        ttk.Entry(row, textvariable=self.save_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Browse…", command=self.pick_folder).pack(side="left")

        bar = ttk.Frame(self, padding=(10, 0, 10, 6))
        bar.pack(fill="x")
        for text, cmd in (
            ("Resume", self.resume_sel),
            ("Pause", self.pause_sel),
            ("Remove", lambda: self.remove_sel(False)),
            ("Remove + delete files", lambda: self.remove_sel(True)),
            ("Open folder", self.open_folder),
            ("Sort to library…", self.open_sort),
            ("Settings", self.open_settings),
        ):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 4))

        mid = ttk.Frame(self, padding=(10, 0, 10, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=self.COLUMNS, show="headings", selectmode="extended")
        for col in self.COLUMNS:
            text, width, anchor = self.HEADINGS[col]
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "name"))
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.open_folder())
        self.tree.bind("<Delete>", lambda e: self.remove_sel(False))

        self.logbox = tk.Text(self, height=5, state="disabled", relief="flat",
                              font=("TkFixedFont", 9))
        self.logbox.pack(fill="x", padx=10, pady=(6, 0))
        self.status = ttk.Label(self, anchor="w", padding=(10, 4))
        self.status.pack(fill="x")

    # -- basic actions ------------------------------------------------------ #
    def log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _save_path(self):
        path = self.save_var.get().strip() or DEFAULT_SAVE
        os.makedirs(path, exist_ok=True)
        if path != self.settings["save_path"]:
            self.settings["save_path"] = path
            save_settings(self.settings)
        return path

    def paste(self):
        try:
            self.magnet_var.set(self.clipboard_get().strip())
        except tk.TclError:
            pass

    def _add_any(self, text):
        text = text.strip()
        try:
            if text.lower().startswith("magnet:"):
                self.engine.add_magnet(text, self._save_path())
                self.log("Added magnet link.")
            elif text.lower().endswith(".torrent") and os.path.isfile(text):
                self.engine.add_torrent_file(text, self._save_path())
                self.log(f"Added {os.path.basename(text)}")
            else:
                messagebox.showwarning(APP_NAME, "That doesn't look like a magnet link.")
                return False
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not add torrent:\n{e}")
            return False
        return True

    def add_magnet(self):
        if self._add_any(self.magnet_var.get()):
            self.magnet_var.set("")

    def add_file(self):
        for path in filedialog.askopenfilenames(
                title="Open torrent file", filetypes=[("Torrent files", "*.torrent")]):
            self._add_any(path)

    def pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.save_var.get() or DEFAULT_SAVE)
        if d:
            self.save_var.set(d)
            self._save_path()

    def _selected(self):
        return list(self.tree.selection())

    def pause_sel(self):
        for k in self._selected():
            self.engine.pause(k)

    def resume_sel(self):
        for k in self._selected():
            if k in self.engine.reports:      # flagged: ask again instead of resuming
                if k not in self.engine.needs_review:
                    self.engine.needs_review.append(k)
            else:
                self.engine.resume(k)

    def remove_sel(self, delete_files):
        keys = self._selected()
        if not keys:
            return
        what = "remove the selected torrent(s) AND delete their files" if delete_files \
            else "remove the selected torrent(s) (files are kept)"
        if not messagebox.askyesno(APP_NAME, f"Are you sure you want to {what}?"):
            return
        for k in keys:
            self.engine.remove(k, delete_files)
            if self.tree.exists(k):
                self.tree.delete(k)

    def open_folder(self):
        keys = self._selected()
        path = self.engine.save_path_of(keys[0]) if keys else self._save_path()
        if not path:
            return
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}" >/dev/null 2>&1 &')

    # -- library sorting ---------------------------------------------------- #
    def open_sort(self):
        if self.sort_window and self.sort_window.winfo_exists():
            self.sort_window.populate()
            self.sort_window.lift()
        else:
            self.sort_window = SortWindow(self)

    def send_to_library(self, kind, ref, name, dest_dir):
        """kind 'torrent' (ref = key) or 'file' (ref = full path)."""
        if kind == "torrent":
            why = self.engine.move_to_library(ref, dest_dir)
            if why.startswith("clash:"):
                # Same name already in the library: move under "name (2)"
                self._move_file_async(why[len("clash:"):], dest_dir)
            elif why:
                self.log(f"Couldn't move {name}: {why}")
        else:
            self._move_file_async(ref, dest_dir)

    def _move_file_async(self, src, dest_dir):
        def work():
            for attempt in range(6):  # files can stay locked for a moment
                try:
                    new = library.move_item(src, dest_dir)
                    self.events.put(("moved", os.path.basename(src), new))
                    return
                except PermissionError:
                    time.sleep(2)
                except Exception as e:
                    self.events.put(("log", f"Couldn't move {os.path.basename(src)}: {e}"))
                    return
            self.events.put(("log", f"Couldn't move {os.path.basename(src)}: "
                                    "it's in use by another program."))
        threading.Thread(target=work, daemon=True).start()

    def auto_sort(self, key):
        """Called when a download finishes."""
        if not self.settings.get("auto_sort", True):
            return
        lib = self.library()
        name, _ = self.engine.info(key)
        if not name:
            return
        if not lib.connected:
            if key not in self.waiting_for_drive:
                self.waiting_for_drive.append(key)
            self.log(f"{lib.root} isn't connected. {name} will be sorted when it's plugged in.")
            return
        label, path, confident = lib.guess(name)
        # Second check on what actually landed on disk, so nothing risky is
        # ever moved into the library without a person looking at it.
        if self.engine.reviewed.get(key) == "all-risky" or self.engine.disk_report(key).items:
            self.log(f"{name} contains files you approved as risky, so it isn't sorted "
                     "automatically. Check it in the Sort window.")
            self.open_sort()
        elif confident:
            self.log(f"Sorting {name}  →  {label}")
            self.send_to_library("torrent", key, name, path)
        else:
            self.log(f"{name} needs a folder. Choose one in the Sort window.")
            self.open_sort()

    def check_drive(self):
        if self.waiting_for_drive and self.library().connected:
            keys, self.waiting_for_drive = self.waiting_for_drive, []
            self.log("Library drive connected. Sorting finished downloads…")
            for k in keys:
                if self.engine.is_complete(k):
                    self.auto_sort(k)
        self.after(10_000, self.check_drive)

    def _queue_plex(self, path):
        if self.settings.get("plex_enabled", True):
            self.plex_paths.add(os.path.dirname(path))
            self.plex_due = time.time() + 5  # wait for more moves, then scan once

    def _run_plex(self):
        paths, self.plex_paths = sorted(self.plex_paths), set()
        plex = library.Plex(self.settings.get("plex_url"), self.settings.get("plex_token"))

        def work():
            for p in paths:
                try:
                    self.events.put(("log", plex.refresh_for(p)))
                except Exception as e:
                    self.events.put(("log", f"Plex refresh failed: {e}. "
                                            "Check the Plex settings."))
                    return
        threading.Thread(target=work, daemon=True).start()

    # -- settings ----------------------------------------------------------- #
    def open_settings(self):
        win = tk.Toplevel(self)
        win.title("Settings")
        win.transient(self)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=14)
        frm.pack()
        frm.columnconfigure(1, weight=1)

        dl = tk.IntVar(value=self.settings["download_limit_kbps"])
        ul = tk.IntVar(value=self.settings["upload_limit_kbps"])
        port = tk.IntVar(value=self.settings["listen_port"])
        seed = tk.BooleanVar(value=self.settings["seed_after_finish"])
        lib_root = tk.StringVar(value=self.settings["library_root"])
        auto = tk.BooleanVar(value=self.settings["auto_sort"])
        plex_on = tk.BooleanVar(value=self.settings["plex_enabled"])
        plex_url = tk.StringVar(value=self.settings["plex_url"])
        plex_token = tk.StringVar(value=self.settings["plex_token"])

        r = 0
        ttk.Label(frm, text="Downloads", font=("TkDefaultFont", 10, "bold")).grid(
            row=r, column=0, sticky="w"); r += 1
        ttk.Label(frm, text="Download limit (KB/s, 0 = unlimited)").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(frm, from_=0, to=10_000_000, textvariable=dl, width=12).grid(
            row=r, column=1, sticky="w", pady=2); r += 1
        ttk.Label(frm, text="Upload limit (KB/s, 0 = unlimited)").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(frm, from_=0, to=10_000_000, textvariable=ul, width=12).grid(
            row=r, column=1, sticky="w", pady=2); r += 1
        ttk.Label(frm, text="Listening port (restart to apply)").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(frm, from_=1024, to=65535, textvariable=port, width=12).grid(
            row=r, column=1, sticky="w", pady=2); r += 1
        ttk.Checkbutton(frm, text="Keep seeding after download finishes (until sorted)",
                        variable=seed).grid(row=r, column=0, columnspan=3, sticky="w"); r += 1

        ttk.Separator(frm).grid(row=r, column=0, columnspan=3, sticky="ew", pady=10); r += 1
        ttk.Label(frm, text="Library", font=("TkDefaultFont", 10, "bold")).grid(
            row=r, column=0, sticky="w"); r += 1
        ttk.Label(frm, text="Library drive or folder").grid(row=r, column=0, sticky="w")
        ttk.Entry(frm, textvariable=lib_root, width=32).grid(row=r, column=1, sticky="ew", pady=2)
        ttk.Button(frm, text="Browse…", command=lambda: lib_root.set(
            filedialog.askdirectory(parent=win, initialdir=lib_root.get()) or lib_root.get())
        ).grid(row=r, column=2, padx=(4, 0)); r += 1
        ttk.Checkbutton(frm, text="Sort downloads into the library when they finish",
                        variable=auto).grid(row=r, column=0, columnspan=3, sticky="w"); r += 1

        ttk.Separator(frm).grid(row=r, column=0, columnspan=3, sticky="ew", pady=10); r += 1
        ttk.Label(frm, text="Plex", font=("TkDefaultFont", 10, "bold")).grid(
            row=r, column=0, sticky="w"); r += 1
        ttk.Checkbutton(frm, text="Ask Plex to scan new files after moving them",
                        variable=plex_on).grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        ttk.Label(frm, text="Plex address").grid(row=r, column=0, sticky="w")
        ttk.Entry(frm, textvariable=plex_url, width=32).grid(row=r, column=1, sticky="ew", pady=2); r += 1
        ttk.Label(frm, text="Plex token").grid(row=r, column=0, sticky="w")
        tok = ttk.Entry(frm, textvariable=plex_token, show="•", width=32)
        tok.grid(row=r, column=1, sticky="ew", pady=2)
        show = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Show", variable=show,
                        command=lambda: tok.configure(show="" if show.get() else "•")
                        ).grid(row=r, column=2, padx=(4, 0)); r += 1
        plex_msg = ttk.Label(frm, text="", foreground="gray", wraplength=380)

        def test_plex():
            plex_msg.configure(text="Connecting…")
            win.update_idletasks()
            try:
                secs = library.Plex(plex_url.get(), plex_token.get()).sections()
                names = ", ".join(t for _, t, _ in secs) or "no libraries"
                plex_msg.configure(text=f"Connected. Libraries: {names}")
            except Exception as e:
                plex_msg.configure(text=f"Couldn't connect: {e}")

        ttk.Button(frm, text="Test connection", command=test_plex).grid(
            row=r, column=1, sticky="w", pady=(4, 0)); r += 1
        plex_msg.grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0)); r += 1

        if sys.platform.startswith("win"):
            ttk.Separator(frm).grid(row=r, column=0, columnspan=3, sticky="ew", pady=10); r += 1
            ttk.Label(frm, text="Magnet links", font=("TkDefaultFont", 10, "bold")).grid(
                row=r, column=0, sticky="w"); r += 1
            link_msg = ttk.Label(frm, text="", foreground="gray", wraplength=380)

            def show_link_state():
                try:
                    on = handlers_registered()
                except Exception:
                    on = False
                link_msg.configure(text=(
                    "MagnetDL is offered for magnet links and .torrent files."
                    if on else "Clicking a magnet link in your browser doesn't use MagnetDL yet."))
                return on

            def use_magnetdl():
                try:
                    register_handlers()
                except Exception as e:
                    messagebox.showerror(APP_NAME, f"Couldn't set this up:\n{e}", parent=win)
                    return
                show_link_state()
                messagebox.showinfo(
                    APP_NAME,
                    "Done. Windows will now open its Default apps page for MagnetDL.\n\n"
                    "If it shows another app next to MAGNET or .torrent, click it and "
                    "choose MagnetDL.\n\nThe first time you click a magnet link, your "
                    "browser may ask to open MagnetDL: tick 'Always allow' and click Open.",
                    parent=win)
                try:
                    os.startfile("ms-settings:defaultapps?registeredAppUser=" + APP_NAME)  # noqa
                except Exception:
                    pass

            def stop_using():
                try:
                    unregister_handlers()
                except Exception as e:
                    messagebox.showerror(APP_NAME, f"Couldn't undo this:\n{e}", parent=win)
                show_link_state()

            ttk.Button(frm, text="Open magnet links with MagnetDL", command=use_magnetdl).grid(
                row=r, column=0, sticky="w", pady=2)
            ttk.Button(frm, text="Stop", command=stop_using).grid(
                row=r, column=1, sticky="w", padx=(4, 0), pady=2); r += 1
            link_msg.grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
            show_link_state()

        def apply():
            try:
                self.settings.update(
                    download_limit_kbps=max(0, dl.get()),
                    upload_limit_kbps=max(0, ul.get()),
                    listen_port=port.get(),
                    seed_after_finish=seed.get(),
                    library_root=lib_root.get().strip(),
                    auto_sort=auto.get(),
                    plex_enabled=plex_on.get(),
                    plex_url=plex_url.get().strip(),
                    plex_token=plex_token.get().strip(),
                )
            except tk.TclError:
                messagebox.showerror(APP_NAME, "Please enter whole numbers.", parent=win)
                return
            save_settings(self.settings)
            self.engine.apply_limits()
            win.destroy()

        ttk.Button(frm, text="Save", command=apply).grid(row=r, column=2, sticky="e", pady=(12, 0))

    # -- periodic refresh --------------------------------------------------- #
    def refresh(self):
        # Never let one unexpected error stop the window from updating
        try:
            self._check_inbox()
            self._refresh_once()
        except Exception as e:
            msg = f"Unexpected error (MagnetDL keeps running): {type(e).__name__}: {e}"
            if msg != getattr(self, "_last_error", None):
                self._last_error = msg
                self.log(msg)
        self.after(1000, self.refresh)

    def _check_inbox(self):
        """Magnet links / .torrent files handed over by a second copy of MagnetDL."""
        items = read_inbox()
        if not items:
            return
        if self.state() == "iconic":
            self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        for text in items:
            if text != "show":
                self._add_any(text)

    def _refresh_once(self):
        for msg in self.engine.handle_alerts():
            self.log(msg)

        # downloads that just finished
        while self.engine.finished:
            self.auto_sort(self.engine.finished.pop(0))

        # downloads the safety check flagged: ask about one at a time
        if self.engine.needs_review and not (self.review_window
                                             and self.review_window.winfo_exists()):
            key = self.engine.needs_review.pop(0)
            if key in self.engine.reports:
                self.review_window = ReviewDialog(self, key)

        # torrent moves completed by the engine
        while self.engine.moved:
            name, new = self.engine.moved.pop(0)
            self._queue_plex(new)

        # results from worker threads
        while True:
            try:
                kind, *data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "moved":
                name, new = data
                self.log(f"Moved to library: {name}  →  {os.path.dirname(new)}")
                self._queue_plex(new)
            else:
                self.log(data[0])

        if self.plex_paths and time.time() >= self.plex_due:
            self._run_plex()

        rows = self.engine.snapshot()
        total_down = total_up = 0
        seen = set()
        for r in rows:
            seen.add(r["key"])
            total_down += r["down"]
            total_up += r["up"]
            values = (
                r["name"],
                human_size(r["size"]) if r["size"] else "—",
                f"{r['progress'] * 100:.1f}%",
                r["state"],
                human_size(r["down"]) + "/s",
                human_size(r["up"]) + "/s",
                f"{r['peers']} ({r['seeds']})",
                "—" if r["finished"] else human_eta(r["eta"]),
            )
            if self.tree.exists(r["key"]):
                self.tree.item(r["key"], values=values)
            else:
                self.tree.insert("", "end", iid=r["key"], values=values)
        for iid in self.tree.get_children():
            if iid not in seen:
                self.tree.delete(iid)

        dht = self.engine.session.status().dht_nodes
        lib = self.library()
        drive = f"Library {lib.root}: {'connected' if lib.connected else 'not connected'}" \
            if lib.root else "Library: not set"
        self.status.configure(
            text=f"↓ {human_size(total_down)}/s    ↑ {human_size(total_up)}/s    "
                 f"Torrents: {len(rows)}    DHT nodes: {dht}    {drive}")

    def on_close(self):
        if self.engine.moving:
            if not messagebox.askyesno(APP_NAME, "Files are still being moved to the library. "
                                                 "Close anyway? The move finishes next time."):
                return
        self.status.configure(text="Saving state…")
        self.update_idletasks()
        self.engine.shutdown()
        self.destroy()


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    lock = acquire_single_instance()
    if lock is None:
        # Already running: hand over the magnet link (or just bring it to the front)
        forward_to_running(sys.argv[1:])
        return
    App().mainloop()  # anything left in the inbox is added on the first refresh


if __name__ == "__main__":
    main()
