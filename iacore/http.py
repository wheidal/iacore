"""Asking archive.org for things: small replies read whole, downloads streamed, and the URLs for both.

Every request carries a User-Agent naming the tool, as archive.org asks of automated clients. Callers
pass their own (e.g. "deadfetch/0.6.1 (+https://github.com/...; personal collection)"); iacore's is the
fallback.
"""

import urllib.error
import urllib.parse
import urllib.request

from . import __version__

ARCHIVE = "https://archive.org"
USER_AGENT = f"iacore/{__version__} (+https://github.com/wheidal/iacore)"
MAX_GET_BYTES = 5 * 1024 * 1024


class FetchError(Exception):
    pass


def http_get(url: str, limit: int = MAX_GET_BYTES, timeout: int = 30, user_agent: str = USER_AGENT) -> bytes:
    """The whole reply, for metadata and search results. More than `limit` bytes is an error."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"{url}: {getattr(exc, 'reason', exc)}") from exc
    if len(data) > limit:
        raise FetchError(f"{url}: larger than {limit} bytes")
    return data


def http_open(url: str, start: int = 0, timeout: int = 60, user_agent: str = USER_AGENT):
    """A download as an open response, from byte `start` on (a resume) if that's not 0. Not counted as a view."""
    headers = {"User-Agent": user_agent}
    if start:
        headers["Range"] = f"bytes={start}-"
    return urllib.request.urlopen(urllib.request.Request(not_counted(url), headers=headers), timeout=timeout)


def not_counted(url: str) -> str:
    """The URL with cnt=0, which tells archive.org not to count the download as a view, as its own `ia`
    tool does: an automated download shouldn't inflate the download counts people go by."""
    return url + ("&" if "?" in url else "?") + "cnt=0"


def metadata_url(identifier: str) -> str:
    return f"{ARCHIVE}/metadata/{urllib.parse.quote(identifier)}"


def download_url(identifier: str, name: str) -> str:
    """A file in an item; `name` is its path within the item, as archive.org lists it."""
    return f"{ARCHIVE}/download/{urllib.parse.quote(identifier)}/{urllib.parse.quote(name)}"


def retry_after(exc, default: float) -> float:
    """The seconds archive.org asked us to wait (its Retry-After header), or `default`."""
    try:
        return float(exc.headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return default
