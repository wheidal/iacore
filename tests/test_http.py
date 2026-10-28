import io
import unittest
import urllib.error
from unittest import mock

from iacore import http
from iacore.http import FetchError, download_url, http_get, http_open, metadata_url, not_counted, retry_after


class Sent:
    """Stands in for urllib.request.urlopen, keeping the requests it was given."""

    def __init__(self, body=b"{}", error=None):
        self.body, self.error, self.requests = body, error, []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(self.body)


class HttpTest(unittest.TestCase):
    def test_downloads_are_not_counted_as_views_and_resume_by_range(self):
        sent = Sent()
        with mock.patch.object(http.urllib.request, "urlopen", sent):
            http_open("https://archive.org/download/x/a.flac", 100, user_agent="tool/1")
            http_open("https://archive.org/download/x/a.flac")
        first, second = sent.requests
        self.assertEqual(first.full_url, "https://archive.org/download/x/a.flac?cnt=0")
        self.assertEqual(first.get_header("Range"), "bytes=100-")
        self.assertEqual(first.get_header("User-agent"), "tool/1")
        self.assertIsNone(second.get_header("Range"))
        self.assertTrue(second.get_header("User-agent").startswith("iacore/"))

    def test_not_counted_keeps_a_query(self):
        self.assertEqual(not_counted("https://a/b?x=1"), "https://a/b?x=1&cnt=0")

    def test_get_sends_the_user_agent_and_reads_the_reply(self):
        sent = Sent(b'{"ok": 1}')
        with mock.patch.object(http.urllib.request, "urlopen", sent):
            self.assertEqual(http_get("https://archive.org/metadata/x", user_agent="tool/1"), b'{"ok": 1}')
        self.assertEqual(sent.requests[0].get_header("User-agent"), "tool/1")
        self.assertEqual(sent.requests[0].full_url, "https://archive.org/metadata/x")  # not a download: no cnt

    def test_get_errors(self):
        for error, expected in [(urllib.error.HTTPError("u", 404, "gone", {}, None), "HTTP 404"),
                                (urllib.error.URLError("no route"), "no route"), (TimeoutError("slow"), "slow")]:
            with self.subTest(expected), mock.patch.object(http.urllib.request, "urlopen", Sent(error=error)):
                with self.assertRaises(FetchError) as caught:
                    http_get("https://archive.org/x")
                self.assertIn(expected, str(caught.exception))
        with mock.patch.object(http.urllib.request, "urlopen", Sent(b"x" * 11)):
            with self.assertRaises(FetchError) as caught:
                http_get("https://archive.org/x", limit=10)
            self.assertIn("larger than 10 bytes", str(caught.exception))

    def test_urls_are_quoted_but_keep_subfolders(self):
        self.assertEqual(metadata_url("a b"), "https://archive.org/metadata/a%20b")
        self.assertEqual(download_url("id", "disc 1/t01 #1.flac"),
                         "https://archive.org/download/id/disc%201/t01%20%231.flac")

    def test_retry_after(self):
        self.assertEqual(retry_after(urllib.error.HTTPError("u", 429, "", {"Retry-After": "7"}, None), 30), 7.0)
        self.assertEqual(retry_after(urllib.error.HTTPError("u", 429, "", {}, None), 30), 30)
        self.assertEqual(retry_after(urllib.error.HTTPError("u", 429, "", {"Retry-After": "soon"}, None), 30), 30)


if __name__ == "__main__":
    unittest.main()
