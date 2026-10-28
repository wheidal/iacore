import hashlib
import http.client
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from iacore import download
from iacore.download import DownloadError, RemoteFile, Throttle, fetch_file, parse_rate

DATA = b"".join(bytes([i % 251]) for i in range(3000))
URL = "https://archive.org/download/item/a.flac"


def reply(data: bytes, status: int = 200, content_range: str | None = None):
    response = io.BytesIO(data)
    response.status = status
    response.headers = {"Content-Range": content_range} if content_range else {}
    return response


class Server:
    """archive.org for one file: honours Range with a proper Content-Range. `script` replaces the first
    replies (a callable taking start, or an exception to raise)."""

    def __init__(self, data=DATA, script=()):
        self.data, self.script, self.starts = data, list(script), []

    def __call__(self, url, start=0):
        self.starts.append(start)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step(start)
        if start:
            return reply(self.data[start:], 206, f"bytes {start}-{len(self.data) - 1}/{len(self.data)}")
        return reply(self.data)


class FetchFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = Path(tmp.name) / "a.flac"
        self.part = Path(tmp.name) / "a.flac.part"
        self.f = RemoteFile("a.flac", len(DATA), hashlib.md5(DATA).hexdigest())
        self.sleeps, self.logged = [], []

    def fetch(self, server, **kw):
        fetch_file(URL, self.f, self.dest, opener=server, sleep=self.sleeps.append, log=self.logged.append, **kw)

    def test_downloads_and_verifies(self):
        self.fetch(Server())
        self.assertEqual(self.dest.read_bytes(), DATA)
        self.assertFalse(self.part.exists())

    def test_resumes_a_part_file(self):
        self.part.write_bytes(DATA[:1000])
        server = Server()
        self.fetch(server)
        self.assertEqual(server.starts, [1000])
        self.assertEqual(self.dest.read_bytes(), DATA)

    def test_md5_is_taken_while_downloading_not_read_back(self):
        reads, real = [], download.md5_of
        with mock.patch.object(download, "md5_of", lambda path: reads.append(path.name) or real(path)):
            self.fetch(Server())
            self.assertEqual(reads, [])
            self.dest.unlink()
            self.part.write_bytes(DATA[:1000])
            self.fetch(Server())
        self.assertEqual(reads, ["a.flac.part"])  # resuming: the part already there, read once

    def test_a_server_that_ignores_the_range_starts_over(self):
        self.part.write_bytes(DATA[:1000])
        self.fetch(Server(script=[lambda start: reply(DATA)]))
        self.assertEqual(self.dest.read_bytes(), DATA)

    def test_a_connection_cut_off_mid_file_resumes(self):
        """http.client ends a cut-off body quietly (or, chunked, with IncompleteRead): resumed, not restarted."""
        for cut in ("short", "incomplete"):
            with self.subTest(cut):
                self.dest.unlink(missing_ok=True)

                def first(start, cut=cut):
                    response = reply(DATA[:1200])
                    if cut == "incomplete":
                        def read(n, body=response.read):
                            if chunk := body(n):
                                return chunk
                            raise http.client.IncompleteRead(b"", 1800)
                        response.read = read
                    return response

                server = Server(script=[first])
                self.fetch(server)
                self.assertEqual(server.starts, [0, 1200])
                self.assertEqual(self.dest.read_bytes(), DATA)

    def test_a_corrupt_file_is_fetched_again(self):
        self.fetch(Server(script=[lambda start: reply(b"X" * len(DATA))]))
        self.assertEqual(self.dest.read_bytes(), DATA)
        self.assertTrue(any("MD5 doesn't match" in line for line in self.logged))

    def test_still_wrong_after_every_try_gives_up(self):
        bad = [lambda start: reply(b"X" * len(DATA))] * 3
        with self.assertRaises(DownloadError) as caught:
            self.fetch(Server(script=bad))
        self.assertIn("still wrong after 3 tries", str(caught.exception))
        self.assertFalse(self.dest.exists())

    def test_backs_off_as_long_as_asked(self):
        self.fetch(Server(script=[urllib.error.HTTPError(URL, 429, "slow down", {"Retry-After": "7"}, None)]))
        self.assertEqual(self.sleeps, [7.0])

    def test_server_errors_are_retried_and_missing_files_are_not(self):
        self.fetch(Server(script=[urllib.error.HTTPError(URL, 500, "oops", {}, None)]))
        self.assertEqual(self.sleeps, [60])
        self.dest.unlink()
        server = Server(script=[urllib.error.HTTPError(URL, 404, "gone", {}, None)])
        with self.assertRaises(DownloadError) as caught:
            self.fetch(server)
        self.assertIn("HTTP 404", str(caught.exception))
        self.assertEqual(server.starts, [0])  # tried once

    def test_a_resumed_reply_for_the_wrong_bytes_starts_the_file_over(self):
        for name, header in [("elsewhere", "bytes 0-2999/3000"), ("other size", "bytes 1000-3999/4000"),
                             ("missing", None)]:
            with self.subTest(name):
                self.dest.unlink(missing_ok=True)
                self.part.write_bytes(DATA[:1000])
                server = Server(script=[lambda start, header=header: reply(b"Z" * 2000, 206, header)])
                self.fetch(server)
                self.assertEqual(server.starts, [1000, 0])
                self.assertEqual(self.dest.read_bytes(), DATA)  # none of the Zs

    def test_416_on_resume_starts_the_file_over(self):
        self.part.write_bytes(DATA[:1000])
        server = Server(script=[urllib.error.HTTPError(URL, 416, "Range Not Satisfiable", {}, None)])
        self.fetch(server)
        self.assertEqual(server.starts, [1000, 0])
        self.assertEqual(self.dest.read_bytes(), DATA)

    def test_a_file_of_unknown_size_already_whole_is_resumed_by_starting_over(self):
        """archive.org lists no size for _files.xml: a whole part file gets a 416 when resumed."""
        f = RemoteFile("item_files.xml", None, None)
        self.part.write_bytes(b"<files/>")
        server = Server(data=b"<files/>", script=[urllib.error.HTTPError(URL, 416, "Range Not Satisfiable", {}, None)])
        fetch_file(URL, f, self.dest, opener=server, sleep=self.sleeps.append)
        self.assertEqual(self.dest.read_bytes(), b"<files/>")

    def test_the_part_path_can_be_chosen(self):
        part = self.dest.with_name("staged.part")
        part.write_bytes(DATA[:500])
        server = Server()
        fetch_file(URL, self.f, self.dest, part, opener=server, sleep=self.sleeps.append)
        self.assertEqual(server.starts, [500])
        self.assertFalse(part.exists())

    def test_reports_progress(self):
        events = []

        class Spy:
            def __getattr__(self, name):
                return lambda *a: events.append((name, *a))

        self.part.write_bytes(DATA[:1000])
        self.fetch(Server(), progress=Spy())
        self.assertEqual(events[0], ("at", 1000))
        self.assertEqual(sum(e[1] for e in events if e[0] == "add"), len(DATA) - 1000)


class RateTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_rate("4M"), 4 * 1024 ** 2)
        self.assertEqual(parse_rate("500K"), 500 * 1024)
        self.assertEqual(parse_rate("2.5mb/s"), 2.5 * 1024 ** 2)
        self.assertIsNone(parse_rate("none"))
        self.assertIsNone(parse_rate(None))
        with self.assertRaises(ValueError):
            parse_rate("fast")

    def test_throttle_sleeps_to_hold_the_rate(self):
        now, slept = [0.0], []
        throttle = Throttle(1000, clock=lambda: now[0], sleep=slept.append)
        throttle(3000)  # 3 s worth at 1000 bytes/s, sent instantly
        self.assertAlmostEqual(slept[0], 3.0)

    def test_throttle_doesnt_bank_idle_time(self):
        now, slept = [0.0], []
        throttle = Throttle(1000, clock=lambda: now[0],
                            sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
        throttle(1000)
        now[0] += 10  # a pause between files
        throttle(3000)  # without a fresh window this would pass at once, 10 s of saved-up allowance
        self.assertAlmostEqual(slept[-1], 2.0)  # 3 s worth, less the one second of credit

    def test_no_cap(self):
        slept = []
        Throttle(None, sleep=slept.append)(10 ** 9)
        self.assertEqual(slept, [])


if __name__ == "__main__":
    unittest.main()
