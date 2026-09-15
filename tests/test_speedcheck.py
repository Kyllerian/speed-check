import argparse
import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from speedcheck import (
    BYTES_PER_MEGABYTE,
    SpeedcheckError,
    download,
    main,
    megabytes_per_second,
    run_measurements,
    validate_url,
)


PAYLOAD = b"speedcheck" * 10_000


class PayloadHandler(BaseHTTPRequestHandler):
    active_requests = 0
    max_active_requests = 0
    completed_requests = 0
    lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802 - method name is defined by BaseHTTPRequestHandler
        with self.lock:
            type(self).active_requests += 1
            type(self).max_active_requests = max(
                type(self).max_active_requests, type(self).active_requests
            )

        try:
            time.sleep(0.01)
            if self.path == "/error":
                self.send_error(503, "test failure")
                return

            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
        finally:
            with self.lock:
                type(self).active_requests -= 1
                type(self).completed_requests += 1

    def log_message(self, format: str, *args: object) -> None:
        pass


class SpeedcheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), PayloadHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        host, port = cls.server.server_address
        cls.url = f"http://{host}:{port}/large-file.bin"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def setUp(self) -> None:
        PayloadHandler.active_requests = 0
        PayloadHandler.max_active_requests = 0
        PayloadHandler.completed_requests = 0

    def test_download_reads_the_entire_response(self) -> None:
        measurement = download(self.url, timeout=1, on_progress=lambda *_: None)

        self.assertEqual(measurement.byte_count, len(PAYLOAD))
        self.assertGreater(measurement.seconds, 0)

    def test_requests_are_sequential(self) -> None:
        output = io.StringIO()

        measurements = run_measurements(
            self.url, count=3, timeout=1, output=output
        )

        self.assertEqual(len(measurements), 3)
        self.assertEqual(PayloadHandler.completed_requests, 3)
        self.assertEqual(PayloadHandler.max_active_requests, 1)
        self.assertIn("Запрос  3/3", output.getvalue())
        self.assertIn("100.00%", output.getvalue())
        self.assertIn("готово за", output.getvalue())

    def test_speed_uses_total_bytes_divided_by_total_time(self) -> None:
        speed = megabytes_per_second(2 * BYTES_PER_MEGABYTE, 4)

        self.assertEqual(speed, 0.5)

    def test_http_error_is_reported(self) -> None:
        with self.assertRaisesRegex(SpeedcheckError, "HTTP 503"):
            download(
                f"{self.url.rsplit('/', 1)[0]}/error",
                timeout=1,
                on_progress=lambda *_: None,
            )

    def test_cli_uses_ten_requests_by_default(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main([self.url, "--timeout", "1"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(PayloadHandler.completed_requests, 10)
        self.assertIn("Успешных запросов:       10", output.getvalue())
        self.assertIn(f"({len(PAYLOAD) * 10} байт)", output.getvalue())

    def test_cli_returns_130_when_interrupted(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch("speedcheck.run_measurements", side_effect=KeyboardInterrupt),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = main([self.url])

        self.assertEqual(exit_code, 130)
        self.assertIn("Остановлено пользователем", stderr.getvalue())

    def test_download_does_not_create_files(self) -> None:
        original_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            try:
                os.chdir(temporary_directory)
                with redirect_stdout(io.StringIO()):
                    exit_code = main([self.url, "--count", "1", "--timeout", "1"])
            finally:
                os.chdir(original_directory)

            self.assertEqual(exit_code, 0)
            self.assertEqual(list(Path(temporary_directory).iterdir()), [])

    def test_url_must_be_http_or_https(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            validate_url("example.com/file.jpg")
        with self.assertRaises(argparse.ArgumentTypeError):
            validate_url("ftp://example.com/file.jpg")


if __name__ == "__main__":
    unittest.main()
