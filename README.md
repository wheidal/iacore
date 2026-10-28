# iacore

Polite, verified downloading from the [Internet Archive](https://archive.org). This is the engine shared by **deadfetch** (part of [DeadShowTagger](https://github.com/wheidal/DeadShowTagger)) and **iafetch**, a general Internet Archive downloader that's still in planning.

It's a small library with no dependencies beyond the Python standard library (3.11+). It doesn't decide *what* to download. It makes sure that whatever is downloaded is fetched considerately and arrives intact.

## The rules it keeps

These come from archive.org's [guidance for automated access](https://archive.org/developers/bots.html), its [terms of use](https://archive.org/about/terms), and its request to bulk users to ["start slowly, and ramp up"](https://blog.archive.org/2023/05/29/let-us-serve-you-but-dont-bring-us-down/).

- **It says who it is.** Every request carries a descriptive User-Agent. Callers pass their own tool name and version, and iacore's is the fallback.
- **It doesn't count as a view.** Downloads are requested with `cnt=0`, as archive.org's own `ia` tool does by default, so automated downloads don't inflate the counts people go by.
- **It goes at a set pace.** `Throttle` caps the bandwidth, and idle time isn't saved up to spend later as a burst. Search pages are fetched with a pause between them.
- **It backs off when asked.** A `429` or `503` waits as long as archive.org's `Retry-After` says (or 30 s per attempt). A server error (500/502/504) waits a minute, then two. A `404` isn't retried.
- **It fetches each byte once.** An interrupted download resumes from where it stopped. A resumed reply is appended only if its `Content-Range` continues the part file exactly, for a file of the size archive.org lists. Anything else (a wrong range, a `416`) throws the part away and starts over, rather than corrupting the file.
- **It checks everything.** Each file's MD5 is taken while it streams in, then compared, with its size, against what archive.org lists. A file that doesn't match is fetched again, up to three tries. Only a verified file gets its final name.
- **It doesn't page past the end.** Advanced search refuses a query with more than the 10,000 results archive.org will return, instead of quietly returning a short list. For more results, `scrape_identifiers` uses the scrape API's cursor.

The library doesn't touch restricted items (lending library, stream-only), but deciding what to request is the caller's job. Callers should skip `access-restricted-item:true` items and files marked `private`.

## Use

```python
import json
from functools import partial
from pathlib import Path

from iacore.http import download_url, http_get, http_open, metadata_url
from iacore.download import RemoteFile, Throttle, fetch_file, parse_rate
from iacore.search import scrape_identifiers

UA = "mytool/1.0 (+https://example.org/mytool; personal collection)"
get = partial(http_get, user_agent=UA)
opener = partial(http_open, user_agent=UA)

item = json.loads(get(metadata_url("AboutBan1935")))
for f in item["files"]:
    if f.get("source") == "original" and f["name"].endswith(".mpeg"):
        remote = RemoteFile(f["name"], int(f["size"]), f.get("md5"))
        fetch_file(download_url("AboutBan1935", f["name"]), remote, Path(f["name"]),
                   throttle=Throttle(parse_rate("4M")), opener=opener, log=print)

ids = scrape_identifiers("collection:prelinger AND year:1935", get)
```

## Modules

| Module | What's in it |
| --- | --- |
| `iacore.http` | `http_get` (whole replies, size-capped), `http_open` (streamed downloads, `Range`, `cnt=0`), `metadata_url`, `download_url`, `retry_after`, `FetchError` |
| `iacore.download` | `fetch_file` (resumable, throttled, MD5-verified), `Throttle`, `parse_rate`, `RemoteFile`, `check`, `DownloadError` |
| `iacore.search` | `advanced_search` (any fields, paged, the 10,000 ceiling enforced), `scrape_identifiers` (cursor-paged, no ceiling) |

## Developing

```sh
pip install -e .
python3 -m unittest discover -s tests
```

Tests never touch the network. They run against fakes of archive.org.

## License

MIT. See [LICENSE](LICENSE).
