import json
import unittest
import urllib.parse
from unittest import mock

from iacore import search
from iacore.http import FetchError
from iacore.search import advanced_search, parse_json, scrape_identifiers

DOCS = [{"identifier": f"item{n:02d}", "title": f"Item {n}"} for n in range(7)]


class Archive:
    """archive.org's two search APIs over DOCS, keeping the URLs asked for."""

    def __init__(self, docs=DOCS, scrape_page=3):
        self.docs, self.scrape_page, self.urls = docs, scrape_page, []

    def __call__(self, url):
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/advancedsearch.php":
            page, rows = int(q["page"][0]), int(q["rows"][0])
            fields = q["fl[]"]
            docs = [{k: d[k] for k in fields} for d in self.docs[(page - 1) * rows: page * rows]]
            return json.dumps({"response": {"numFound": len(self.docs), "docs": docs}}).encode()
        if parsed.path == "/services/search/v1/scrape":
            at = int(q.get("cursor", ["0"])[0])
            items = [{"identifier": d["identifier"]} for d in self.docs[at: at + self.scrape_page]]
            reply = {"items": items, "count": len(items), "total": len(self.docs)}
            if at + self.scrape_page < len(self.docs):
                reply["cursor"] = str(at + self.scrape_page)
            return json.dumps(reply).encode()
        raise AssertionError(url)


class SearchTest(unittest.TestCase):
    def setUp(self):
        sleep = mock.patch.object(search.time, "sleep", lambda s: self.slept.append(s))
        self.slept = []
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_advanced_search_pages_with_a_pause_between(self):
        archive = Archive()
        docs = advanced_search("collection:x", ["identifier", "title"], archive, page_size=3, pause=1.5)
        self.assertEqual(docs, DOCS)
        self.assertEqual(len(archive.urls), 3)
        self.assertEqual(self.slept, [1.5, 1.5])

    def test_too_many_results_is_an_error_not_a_short_list(self):
        with self.assertRaises(FetchError) as caught:
            advanced_search("collection:x", ["identifier"], Archive(), ceiling=5)
        self.assertIn("needs splitting", str(caught.exception))

    def test_scrape_follows_the_cursor(self):
        archive = Archive(scrape_page=3)
        self.assertEqual(scrape_identifiers("collection:x", archive, pause=1.0), {d["identifier"] for d in DOCS})
        self.assertEqual(len(archive.urls), 3)
        self.assertNotIn("cursor", archive.urls[0])
        self.assertEqual(self.slept, [1.0, 1.0])

    def test_unreadable_reply(self):
        with self.assertRaises(FetchError):
            parse_json(b"<html>busy</html>")


if __name__ == "__main__":
    unittest.main()
