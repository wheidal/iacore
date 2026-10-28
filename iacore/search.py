"""Searching archive.org: the advanced search (any fields, at most 10,000 results for one query) and the
scrape API (identifiers only, paged by cursor, no ceiling). Pages are fetched with a pause between them.

`get` is a function from URL to bytes, such as iacore.http.http_get with the caller's User-Agent.
"""

import json
import time
import urllib.parse

from .http import ARCHIVE, FetchError

PAGE = 5000
SEARCH_CEILING = 10000  # archive.org's advanced search returns no more than this for one query
SCRAPE_COUNT = 10000  # the scrape API's largest page (its smallest is 100)


def advanced_search(query: str, fields: list[str], get, *, pause: float = 1.0, page_size: int = PAGE,
                    ceiling: int = SEARCH_CEILING, sort: str = "identifier asc", log=lambda line: None) -> list[dict]:
    """Every result of `query`, with `fields`. A query with more results than archive.org will return is an
    error rather than a silently short list: split it (by year, say), or use scrape_identifiers."""
    docs, page = [], 1
    while True:
        params = [("q", query), ("rows", str(page_size)), ("page", str(page)), ("output", "json"), ("sort[]", sort)]
        params += [("fl[]", f) for f in fields]
        data = parse_json(get(f"{ARCHIVE}/advancedsearch.php?" + urllib.parse.urlencode(params)))
        response = data.get("response") or {}
        total = response.get("numFound", 0)
        if total > ceiling:
            raise FetchError(f"archive.org's search found {total} results for one query, more than the "
                             f"{ceiling} it returns; the query needs splitting")
        batch = response.get("docs") or []
        docs += batch
        log(f"  {len(docs)} of {total}")
        if not batch or len(docs) >= total:
            return docs
        page += 1
        time.sleep(pause)


def scrape_identifiers(query: str, get, *, pause: float = 1.0, count: int = SCRAPE_COUNT) -> set[str]:
    """The identifiers of every item matching `query`, however many."""
    out, cursor = set(), None
    while True:
        params = {"q": query, "fields": "identifier", "count": str(count)}
        if cursor:
            params["cursor"] = cursor
        data = parse_json(get(f"{ARCHIVE}/services/search/v1/scrape?" + urllib.parse.urlencode(params)))
        out |= {i["identifier"] for i in data.get("items", []) if i.get("identifier")}
        cursor = data.get("cursor")
        if not cursor:
            return out
        time.sleep(pause)


def parse_json(data: bytes) -> dict:
    try:
        return json.loads(data)
    except ValueError as exc:
        raise FetchError(f"archive.org's search sent something unreadable ({exc})") from exc
