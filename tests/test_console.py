"""Local console regression tests; no OCR model or network download needed."""

import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import web_console  # noqa: E402


class UploadTests(unittest.TestCase):
    def multipart(self, name: str, payload: bytes) -> tuple[bytes, str]:
        safe_header = name.replace("\\", "\\\\").replace('"', '\\"')
        body = (
            b'--test-boundary\r\nContent-Disposition: form-data; name="files"; filename="'
            + safe_header.encode("ascii")
            + b'"\r\nContent-Type: image/png\r\n\r\n'
            + payload
            + b'\r\n--test-boundary--\r\n'
        )
        return body, "multipart/form-data; boundary=test-boundary"

    def test_rejects_false_image(self):
        body, content_type = self.multipart("private.png", b"not a png")
        self.assertEqual(web_console.parse_upload(body, content_type), [])

    def test_strips_windows_path_from_filename(self):
        body, content_type = self.multipart(
            r"folder\photo.png", b"\x89PNG\r\n\x1a\ncontent"
        )
        self.assertEqual(web_console.parse_upload(body, content_type)[0][0], "photo.png")

    def test_rejects_large_upload(self):
        with patch.object(web_console, "MAX_UPLOAD_BYTES", 3):
            with self.assertRaises(ValueError):
                web_console.parse_upload(b"long", "multipart/form-data; boundary=x")

    def test_only_upload_batches_can_be_cleaned(self):
        self.assertEqual(
            web_console.uploaded_session("input/web-console/session-0123abcd"),
            web_console.UPLOAD_ROOT / "session-0123abcd",
        )
        for path in ("input", "output", "input/web-console/session-../../output"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                web_console.uploaded_session(path)

    def test_external_paths_are_not_returned_to_status_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(web_console.public_path(directory), "<external path>")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        (root / "private.txt").write_text("sensitive text", encoding="utf-8")
        self.previous = web_console.JOB.snapshot()
        with web_console.JOB.lock:
            web_console.JOB.output_dir = str(root)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_console.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        with web_console.JOB.lock:
            web_console.JOB.output_dir = self.previous["output_dir"]

    def request(self, path, token=None):
        headers = {"X-OCR-Token": token} if token else {}
        return urlopen(Request(
            f"http://127.0.0.1:{self.server.server_port}{path}", headers=headers
        ))

    def test_query_string_cannot_authenticate_download(self):
        with self.assertRaises(HTTPError) as error:
            self.request(f"/files/private.txt?token={web_console.ACCESS_TOKEN}")
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_authenticated_download_is_not_cached(self):
        with self.request("/files/private.txt", web_console.ACCESS_TOKEN) as response:
            self.assertEqual(response.read(), b"sensitive text")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_download_cannot_escape_output_directory(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/files/..%2Fother.txt", web_console.ACCESS_TOKEN)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_index_never_contains_a_fixed_token(self):
        with self.request("/") as response:
            self.assertIn(web_console.ACCESS_TOKEN.encode(), response.read())
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_rejects_upload_before_reading_oversized_body(self):
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}/api/upload",
            data=b"x",
            headers={
                "X-OCR-Token": web_console.ACCESS_TOKEN,
                "Content-Type": "multipart/form-data; boundary=x",
                "Content-Length": str(web_console.MAX_UPLOAD_BYTES + 1),
            },
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_status_does_not_reveal_project_path(self):
        with self.request("/api/status", web_console.ACCESS_TOKEN) as response:
            body = response.read()
        self.assertNotIn(str(web_console.PROJECT_ROOT).encode(), body)

    def test_cleanup_only_removes_requested_upload_batch(self):
        root = Path(self.tempdir.name) / "uploads"
        session = root / "session-0123abcd"
        session.mkdir(parents=True)
        (session / "sample.png").write_bytes(b"test")
        other = root / "session-0123abce"
        other.mkdir()
        (other / "keep.png").write_bytes(b"test")
        with patch.object(web_console, "UPLOAD_ROOT", root):
            request = Request(
                f"http://127.0.0.1:{self.server.server_port}/api/cleanup",
                data=(f'{{"input_dir":"{session.as_posix()}"}}').encode(),
                headers={
                    "X-OCR-Token": web_console.ACCESS_TOKEN,
                    "Content-Type": "application/json",
                },
            )
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
        self.assertFalse(session.exists())
        self.assertTrue((other / "keep.png").exists())


if __name__ == "__main__":
    unittest.main()
