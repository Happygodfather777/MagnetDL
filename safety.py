"""
File safety check for MagnetDL.

Looks at the list of files inside a download (before it's downloaded) and
flags anything that isn't a normal video, subtitle or small extra.

This is a name/type check, not an antivirus scan: it catches the usual
tricks used to pass malware off as movies (programs, scripts, shortcuts,
password-protected archives, fake "codec" videos, disguised names).
"""

import os

VIDEO = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".mts", ".webm",
         ".mpg", ".mpeg", ".vob", ".flv", ".ogv", ".3gp", ".divx", ".xvid"}
SAFE_EXTRA = {
    # subtitles
    ".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sup", ".smi",
    # audio tracks
    ".mka", ".aac", ".ac3", ".dts", ".eac3", ".flac", ".mp3", ".m4a", ".opus", ".ogg",
    # info / artwork / checksums
    ".nfo", ".txt", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".sfv", ".md5", ".sha1", ".sha256", ".nzb", ".xml", ".json",
}
DANGER = {
    ".exe", ".msi", ".msp", ".msix", ".appx", ".bat", ".cmd", ".com", ".scr", ".pif",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".psm1", ".psd1",
    ".lnk", ".hta", ".jar", ".dll", ".sys", ".cpl", ".reg", ".inf", ".scf",
    ".msc", ".gadget", ".application", ".appref-ms", ".chm", ".apk", ".dmg",
    ".pkg", ".app", ".sh", ".run", ".deb", ".rpm", ".py", ".pyw", ".vb", ".ws",
}
ARCHIVE = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".cab", ".arj",
           ".lzh", ".ace", ".zipx"}
DISK_IMAGE = {".iso", ".img", ".vhd", ".vhdx"}
FAKE_VIDEO = {".wmv", ".asf", ".wma"}  # classic "you need this codec" scams
LINKS_DOCS = {".url", ".website", ".html", ".htm", ".mht", ".pdf", ".doc", ".docx",
              ".docm", ".xls", ".xlsx", ".xlsm", ".rtf", ".svg"}

TRICK_CHARS = {"‮", "‭", "‎", "‏", "⁦", "⁧", "⁨"}
MIN_VIDEO_BYTES = 50 * 1024 * 1024  # smaller than this isn't a real movie/episode

OK, WARN, DANGER_LEVEL = "ok", "warn", "danger"


def _ext(name: str) -> str:
    return os.path.splitext(name.rstrip(" ."))[1].lower()


def check_file(path: str, size: int):
    """(level, reason) for one file inside a download."""
    # Windows drops trailing dots and spaces, so "Movie.exe ." is really "Movie.exe"
    name = os.path.basename(path.replace("\\", "/")).rstrip(" .")
    ext = _ext(name)
    stem_ext = _ext(os.path.splitext(name)[0])

    if any(c in name for c in TRICK_CHARS):
        return DANGER_LEVEL, "uses hidden characters to fake its file type"
    if ext in DANGER:
        if stem_ext in VIDEO or stem_ext in FAKE_VIDEO:
            return DANGER_LEVEL, f"a program disguised as a video ({stem_ext}{ext})"
        return DANGER_LEVEL, f"a program or script ({ext}) — videos never need one"
    if ext in VIDEO or ext in SAFE_EXTRA:
        if stem_ext in DANGER:
            return DANGER_LEVEL, "double file extension trick"
        return OK, ""
    if ext in ARCHIVE or ext[:3] in (".r0", ".r1", ".r2") or ext[1:].isdigit() and len(ext) == 4:
        return WARN, f"an archive ({ext}) — movies are rarely packed; often hides programs"
    if ext in DISK_IMAGE:
        return WARN, f"a disk image ({ext}) — can hide programs inside"
    if ext in FAKE_VIDEO:
        return WARN, (f"{ext} video — common format for fake videos that ask you "
                      "to download a 'codec' or 'player'")
    if ext in LINKS_DOCS:
        return WARN, f"a link or document ({ext}) — not needed for a video"
    if not ext:
        return WARN, "has no file extension"
    return WARN, f"unusual file type ({ext})"


class Report:
    def __init__(self, files):
        """files: [(path, size)]"""
        self.files = files
        self.items = []          # (index, path, size, level, reason)
        self.notes = []          # whole-download warnings
        for i, (path, size) in enumerate(files):
            level, reason = check_file(path, size)
            if level != OK:
                self.items.append((i, path, size, level, reason))

        videos = [(p, s) for p, s in files if _ext(p) in VIDEO]
        fakes = [(p, s) for p, s in files if _ext(p) in FAKE_VIDEO]
        if not videos and not fakes:
            self.notes.append("There's no video file in this download.")
        elif videos and max(s for _, s in videos) < MIN_VIDEO_BYTES:
            self.notes.append("The video is under 50 MB — too small to be a full "
                              "movie or episode, often a fake or a sample.")

    @property
    def level(self):
        if any(it[3] == DANGER_LEVEL for it in self.items):
            return DANGER_LEVEL
        if self.items or self.notes:
            return WARN
        return OK

    @property
    def flagged_indices(self):
        return [it[0] for it in self.items]

    @property
    def safe_video_count(self):
        return sum(1 for p, _ in self.files if _ext(p) in VIDEO and
                   check_file(p, 0)[0] == OK)

    def summary(self) -> str:
        n = len(self.items)
        bits = []
        if n:
            bits.append(f"{n} risky file{'s' if n != 1 else ''}")
        bits += self.notes
        return "; ".join(bits)


def check_folder(path: str) -> Report:
    """Report for a file or folder already on disk (used for loose files)."""
    files = []
    if os.path.isdir(path):
        for root, _, names in os.walk(path):
            for n in names:
                full = os.path.join(root, n)
                try:
                    files.append((os.path.relpath(full, path), os.path.getsize(full)))
                except OSError:
                    pass
    else:
        try:
            files.append((os.path.basename(path), os.path.getsize(path)))
        except OSError:
            pass
    return Report(files)
