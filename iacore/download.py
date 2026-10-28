"""Downloading one file from archive.org, politely and verifiably.

The file streams into a .part file under a bandwidth cap, with its MD5 taken as it arrives. It's checked
against the size and MD5 archive.org lists, retried if it doesn't match, and resumed if the connection
drops: a resumed reply is appended only if it continues the part file exactly. When archive.org asks us
to slow down (429/503) we wait as long as it says. Only a verified file gets its final name.
"""

import hashlib
import http.client
import re
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from .http import http_open, retry_after

CHUNK = 256 * 1024
RETRIES = 3


class DownloadError(Exception):
    pass


@dataclass
class RemoteFile:
    name: str  # path within the item, posix
    size: int | None
    md5: str | None


class Quiet:
    """Stands in for a progress display when nobody is watching: every call does nothing."""

    def __getattr__(self, name):
        return lambda *args, **kw: None


def parse_rate(text: str | None) -> float | None:
    """ "4M" -> 4194304 bytes/s; "500K", "2.5M", "1G" likewise; "none", "0" or empty -> no cap."""
    if text is None or str(text).strip().lower() in ("", "none", "0", "off", "unlimited"):
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmg]?)i?b?(?:/s)?\s*", str(text).lower())
    if not m:
        raise ValueError(f"{text!r} isn't a rate: use e.g. 4M, 500K or none")
    return float(m.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2)]


class Throttle:
    """Keeps the rate at or under `rate` bytes per second (None: no cap). Time spent idle (the pause between
    files, waiting on the server) isn't saved up to spend later as a burst: at most a second's worth is."""

    CREDIT = 1.0

    def __init__(self, rate: float | None, clock=time.monotonic, sleep=time.sleep):
        self.rate, self.clock, self.sleep = rate, clock, sleep
        self.start, self.sent = clock(), 0

    def __call__(self, n: int) -> None:
        if not self.rate:
            return
        now = self.clock()
        if (now - self.start) - self.sent / self.rate > self.CREDIT:  # been idle: start a fresh window
            self.start, self.sent = now - self.CREDIT, 0
        self.sent += n
        ahead = self.sent / self.rate - (now - self.start)
        if ahead > 0:
            self.sleep(ahead)


class _Restart(Exception):
    """The part file can't be resumed: it's thrown away and the file fetched from the start."""


def fetch_file(url: str, f: RemoteFile, dest: Path, part: Path | None = None, *, throttle=None, opener=http_open,
               sleep=time.sleep, log=lambda line: None, progress=None, retries: int = RETRIES) -> None:
    """Download `url` (archive.org's file `f`) to `dest`, via `part` (default: dest + ".part"), which an
    earlier, interrupted run may have left half done. Raises DownloadError if it can't be got right."""
    part = part or dest.with_name(dest.name + ".part")
    throttle = throttle or Throttle(None)
    progress = progress or Quiet()
    problem = ""
    for attempt in range(1, retries + 1):
        start = part.stat().st_size if part.exists() else 0
        if f.size is not None and start > f.size:
            part.unlink()
            start = 0
        digest = None  # the MD5 is taken as the file streams in, so it's never read back from the disk
        try:
            if f.size is None or start < f.size:
                h = md5_of(part) if start and f.md5 else hashlib.md5()  # resuming: what's there, read once
                with opener(url, start) as response:
                    if start and getattr(response, "status", 206) != 206:
                        start, h = 0, hashlib.md5()  # the server ignored the range: start over
                    elif start and (wrong := wrong_range(response, start, f.size)):
                        raise _Restart(wrong)
                    progress.at(start)
                    with open(part, "ab" if start else "wb") as out:
                        while chunk := response.read(CHUNK):
                            out.write(chunk)
                            h.update(chunk)
                            progress.add(len(chunk))
                            throttle(len(chunk))
                digest = h.hexdigest()
                got = part.stat().st_size
                if f.size is not None and got < f.size:  # http.client ends a cut-off body without an error
                    raise ConnectionError(f"the connection closed after {got} of {f.size} bytes")
        except _Restart as exc:
            part.unlink(missing_ok=True)
            if attempt < retries:
                log(f"    {f.name}: {exc}; starting it again from the beginning")
                continue
            raise DownloadError(f"{f.name}: {exc}") from exc
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and start:  # the part can't be continued (the file changed, or it was already whole)
                part.unlink(missing_ok=True)
                if attempt < retries:
                    log(f"    {f.name}: archive.org won't resume it (HTTP 416); starting it again from the beginning")
                    continue
            if exc.code in (429, 503) and attempt < retries:
                wait = retry_after(exc, 30 * attempt)
                log(f"    archive.org asked us to slow down (HTTP {exc.code}); waiting {wait:.0f} s")
                sleep(wait)
                continue
            if exc.code in (500, 502, 504) and attempt < retries:  # a passing fault on archive.org's side
                log(f"    {f.name}: archive.org had a server error (HTTP {exc.code}); trying again in {60 * attempt} s")
                sleep(60 * attempt)
                continue
            raise DownloadError(f"{f.name}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:  # kept: resumed
            if attempt < retries:
                log(f"    {f.name}: {getattr(exc, 'reason', exc)}; resuming")
                sleep(5 * attempt)
                continue
            raise DownloadError(f"{f.name}: {getattr(exc, 'reason', exc)}") from exc
        problem = check(part, f, digest)
        if not problem:
            part.replace(dest)
            return
        log(f"    {f.name}: {problem}; downloading it again")
        part.unlink(missing_ok=True)
    raise DownloadError(f"{f.name}: still wrong after {retries} tries ({problem})")


def check(path: Path, f: RemoteFile, digest: str | None = None) -> str:
    """Size and MD5 against archive.org's: why they don't match, or "". digest is the MD5 taken while
    downloading, if there is one."""
    if f.size is not None and path.stat().st_size != f.size:
        return f"size {path.stat().st_size}, expected {f.size}"
    if f.md5 and (digest or md5_of(path).hexdigest()) != f.md5.lower():
        return "MD5 doesn't match archive.org's"
    return ""


def wrong_range(response, start: int, size: int | None) -> str:
    """Why a 206 doesn't continue the part file from `start`, or "" if it does. Appending a range that
    starts elsewhere, or belongs to a file of another size, would corrupt the part file."""
    headers = getattr(response, "headers", None)
    m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", (headers.get("Content-Range") or "").strip()) if headers else None
    if not m:
        return "archive.org's resumed reply didn't say which bytes it holds"
    if int(m.group(1)) != start:
        return f"archive.org resumed at byte {m.group(1)}, not {start}"
    if size is not None and m.group(3) != "*" and int(m.group(3)) != size:
        return f"archive.org now has it as {m.group(3)} bytes, not the {size} it lists"
    return ""


def md5_of(path: Path):
    """The file's MD5, as a hashlib object (so a resumed download can go on adding to it)."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while block := fh.read(1024 * 1024):
            h.update(block)
    return h
