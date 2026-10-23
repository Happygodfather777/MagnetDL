"""
Library sorting and Plex refresh for MagnetDL.

Knows the layout of the media drive:
    <library>\\Movies\\<Language>\\<Genre>\\
    <library>\\Series\\<Show>\\
    <library>\\Cartoon\\
and works out where a finished download should go.
No GUI code here, so it can be tested on its own.
"""

import difflib
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".ts", ".webm",
             ".mpg", ".mpeg", ".flv", ".srt", ".sub", ".ass", ".vtt"}
PARTIAL_EXT = {".part", ".parts", ".!qb", ".!ut", ".crdownload", ".tmp"}

# Things that mark a TV release and where the show name ends
_SEASON_TOKEN = re.compile(
    r"""(?ix)
    (?:^|[\s._\-(\[])
    (?:
        S\d{1,2}(?:\s*[-–]\s*S?\d{1,2})?(?:E\d{1,3})?     # S01, S01E02, S01-S05
      | \d{1,2}x\d{2,3}                                   # 1x05
      | Seasons?[\s._]*\d+                                # Season 1, Seasons 1
      | Complete[\s._]Series
      # bare "COMPLETE" only when a quality tag, year or the end follows
      # ("Dark.COMPLETE.1080p"), not in titles like "The Complete Guide"
      | (?:The[\s._])?Complete(?=[\s._\-]*(?:$|[(\[]|(?:19|20)\d\d\b|\d{3,4}p\b
                                            |web|bluray|brrip|hdtv|dvdrip|x26[45]|hevc))
    )
    (?=$|[\s._\-)\]])
    """)
_YEAR = re.compile(r"[\(\[]?\b(19|20)\d{2}\b[\)\]]?")
_HINDI = re.compile(r"(?i)(?:^|[^a-z])(hindi|hin|bollywood)(?:[^a-z]|$)")
_JUNK = re.compile(r"(?i)\b(1080p|720p|480p|2160p|4k|bluray|web[- .]?dl|webrip|hdrip|x26[45]|"
                   r"hevc|10bit|aac|dual[ .]audio)\b.*$")


# --------------------------------------------------------------------------- #
# Name helpers
# --------------------------------------------------------------------------- #
def _tidy(text: str) -> str:
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s*[-–]\s*$", "", text)
    return re.sub(r"\s+", " ", text).strip(" -([")


def is_series(name: str) -> bool:
    return bool(_SEASON_TOKEN.search(name))


def show_name(name: str) -> str:
    """'Breaking.Bad.S01-S05.1080p' -> 'Breaking Bad'"""
    base = os.path.splitext(name)[0] if os.path.splitext(name)[1].lower() in VIDEO_EXT else name
    m = _SEASON_TOKEN.search(base)
    head = base[:m.start()] if m else _JUNK.sub("", base)
    head = _YEAR.sub("", head)
    return _tidy(head) or _tidy(base)


def _key(name: str) -> str:
    """Comparable form of a show name: lowercase words, no year/punctuation."""
    s = show_name(name).lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_hindi(name: str) -> bool:
    return bool(_HINDI.search(name))


# --------------------------------------------------------------------------- #
# Destinations
# --------------------------------------------------------------------------- #
class Library:
    def __init__(self, root: str):
        self.root = root

    @property
    def connected(self) -> bool:
        return os.path.isdir(self.root)

    def _dirs(self, *parts):
        path = os.path.join(self.root, *parts)
        try:
            return sorted(d for d in os.listdir(path)
                          if not d.startswith(".") and os.path.isdir(os.path.join(path, d)))
        except OSError:
            return []

    def movie_destinations(self):
        """[(label, path)] for every Movies\\<Language>\\<Genre> folder."""
        out = []
        for lang in self._dirs("Movies"):
            genres = self._dirs("Movies", lang)
            if genres:
                out += [(f"Movies › {lang} › {g}", os.path.join(self.root, "Movies", lang, g))
                        for g in genres]
            else:
                out.append((f"Movies › {lang}", os.path.join(self.root, "Movies", lang)))
        return out

    def series_folders(self):
        return self._dirs("Series")

    def other_destinations(self):
        return [(d, os.path.join(self.root, d)) for d in self._dirs()
                if d.lower() not in ("movies", "series", "back up", "backup")]

    def match_show(self, name: str, fuzzy=False):
        """Existing Series folder whose show name is exactly this release's, or None.
        With fuzzy=True also returns a near match a person should confirm,
        e.g. typos like Severence/Severance or "The Office" / "The Office US".
        A name that merely starts with another ("Dark Matter" vs "Dark") is a
        different show and isn't matched."""
        k = _key(name)
        if not k:
            return None
        near, near_score = None, 0.0
        for folder in self.series_folders():
            fk = _key(folder)
            if not fk:
                continue
            if fk == k:
                return folder
            if fuzzy:
                score = difflib.SequenceMatcher(None, fk, k).ratio()
                if score > near_score:
                    near, near_score = folder, score
        if fuzzy and near_score >= 0.85:
            return near
        return None

    def all_destinations(self, name: str):
        """Every choice for the Sort window, best guess first is not implied."""
        series = []
        new = show_name(name)
        if new and not self.match_show(name):
            series.append((f"Series › {new}  (new folder)", os.path.join(self.root, "Series", new)))
        series += [(f"Series › {s}", os.path.join(self.root, "Series", s))
                   for s in self.series_folders()]
        return self.movie_destinations() + series + self.other_destinations()

    def guess(self, name: str):
        """(label, path, confident) or (None, None, False) if a person must pick."""
        if is_series(name):
            folder = self.match_show(name)
            if folder:
                return f"Series › {folder}", os.path.join(self.root, "Series", folder), True
            folder = self.match_show(name, fuzzy=True)
            if folder:  # probably the same show, but let a person confirm
                return f"Series › {folder}", os.path.join(self.root, "Series", folder), False
            new = show_name(name)
            return (f"Series › {new}  (new folder)",
                    os.path.join(self.root, "Series", new), False)
        # Movie: language is guessable, genre is not
        lang = "Hindi" if is_hindi(name) else "English"
        options = [d for d in self.movie_destinations() if d[0].startswith(f"Movies › {lang}")]
        if len(options) == 1:  # language folder without genre subfolders
            return options[0][0], options[0][1], False
        return None, None, False


# --------------------------------------------------------------------------- #
# Loose files in the download folder
# --------------------------------------------------------------------------- #
def loose_items(folder: str, exclude_names=()):
    """Top-level files/folders in the download folder that look like media."""
    out = []
    try:
        entries = os.listdir(folder)
    except OSError:
        return out
    skip = {n.lower() for n in exclude_names}
    for name in sorted(entries, key=str.lower):
        if name.startswith(".") or name.lower() in skip:
            continue
        full = os.path.join(folder, name)
        ext = os.path.splitext(name)[1].lower()
        if os.path.isdir(full) or (ext in VIDEO_EXT):
            if ext in PARTIAL_EXT:
                continue
            out.append(name)
    return out


def free_name(dest_dir: str, name: str) -> str:
    """name, or 'name (2)' etc. if something with that name is already there."""
    if not os.path.exists(os.path.join(dest_dir, name)):
        return name
    stem, ext = (os.path.splitext(name) if os.path.splitext(name)[1].lower() in VIDEO_EXT
                 else (name, ""))
    n = 2
    while os.path.exists(os.path.join(dest_dir, f"{stem} ({n}){ext}")):
        n += 1
    return f"{stem} ({n}){ext}"


def move_item(src: str, dest_dir: str) -> str:
    """Move a file/folder into dest_dir (creating it). Returns the new path."""
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, free_name(dest_dir, os.path.basename(src)))
    shutil.move(src, target)
    return target


# --------------------------------------------------------------------------- #
# Plex
# --------------------------------------------------------------------------- #
class Plex:
    def __init__(self, url: str, token: str = ""):
        self.url = (url or "http://127.0.0.1:32400").rstrip("/")
        # Tolerate pasting more than the token (e.g. "...?X-Plex-Token=abc&foo" or quotes)
        t = (token or "").strip().strip('"\'')
        if "x-plex-token=" in t.lower():
            t = t[t.lower().index("x-plex-token=") + len("x-plex-token="):]
        self.token = t.split("&")[0].strip()

    # Talk to Plex directly: never route the local server through a system proxy/VPN
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _get(self, path: str, timeout=10) -> bytes:
        req = urllib.request.Request(self.url + path, headers={"Accept": "application/xml"})
        if self.token:
            req.add_header("X-Plex-Token", self.token)
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"Can't reach Plex at {self.url}. Check that Plex Media Server "
                               f"is running and the address is right ({getattr(e, 'reason', e)}).") from None

    def sections(self):
        """[(key, title, [folder paths])]"""
        try:
            data = self._get("/library/sections")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("Plex rejected the token. Paste just the token "
                                   "(the text after X-Plex-Token=) in Settings and click Save, "
                                   "or allow 127.0.0.1 without auth in Plex's Network settings."
                                   if self.token else
                                   "Plex needs a token. Add it in MagnetDL's Settings.") from None
            raise
        root = ET.fromstring(data)
        out = []
        for d in root.iter("Directory"):
            paths = [loc.get("path", "") for loc in d.iter("Location")]
            out.append((d.get("key"), d.get("title"), paths))
        return out

    def refresh_for(self, path: str) -> str:
        """Scan only the library (and folder) that contains `path`."""
        norm = os.path.normcase(os.path.normpath(path))
        hits = []
        for key, title, locs in self.sections():
            for loc in locs:
                ln = os.path.normcase(os.path.normpath(loc))
                if norm == ln or norm.startswith(ln.rstrip("\\/") + os.sep):
                    hits.append((key, title))
                    break
        if not hits:
            self._get("/library/sections/all/refresh")
            return "Plex: refreshing all libraries"
        for key, _ in hits:
            q = urllib.parse.urlencode({"path": path})
            self._get(f"/library/sections/{key}/refresh?{q}")
        return "Plex: scanning " + ", ".join(t for _, t in hits)
