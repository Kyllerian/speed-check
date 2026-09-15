#!/usr/bin/env python3
"""Measure download speed by fetching the same URL sequentially."""

import argparse
import math
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_REQUEST_COUNT = 10
DEFAULT_TIMEOUT_SECONDS = 30.0
CHUNK_SIZE = 64 * 1024
BYTES_PER_MEGABYTE = 1_000_000
PROGRESS_INTERVAL = 0.1


class SpeedcheckError(Exception):
    """A network error that can be displayed without a traceback."""


@dataclass(frozen=True)
class Measurement:
    seconds: float
    byte_count: int


def megabytes_per_second(byte_count: int, seconds: float) -> float:
    return byte_count / BYTES_PER_MEGABYTE / seconds if seconds > 0 else 0.0


def format_megabytes(byte_count: int) -> str:
    return f"{byte_count / BYTES_PER_MEGABYTE:.2f} МБ"


class Progress:
    """Print progress for one request on a single terminal line."""

    def __init__(self, output: TextIO, number: int, count: int) -> None:
        self.output = output
        self.prefix = f"Запрос {number:>2}/{count}: "
        self.last_update = 0.0
        self.line_width = 0

    def update(self, downloaded: int, total: Optional[int]) -> None:
        now = time.perf_counter()
        finished = total is not None and downloaded >= total
        if downloaded and not finished and now - self.last_update < PROGRESS_INTERVAL:
            return

        if total is None:
            text = f"скачано {format_megabytes(downloaded)}"
        else:
            percent = 100.0 if total == 0 else min(downloaded / total * 100, 100.0)
            text = (
                f"{percent:6.2f}% | "
                f"{format_megabytes(downloaded)} / {format_megabytes(total)}"
            )

        self._print(text)
        self.last_update = now

    def finish(self, result: Measurement) -> None:
        speed = megabytes_per_second(result.byte_count, result.seconds)
        self._print(
            f"готово за {result.seconds:.3f} с, "
            f"{format_megabytes(result.byte_count)}, {speed:.2f} МБ/с",
            final=True,
        )

    def stop(self, reason: str) -> None:
        self._print(reason, final=True)

    def _print(self, text: str, final: bool = False) -> None:
        line = self.prefix + text
        padding = " " * max(0, self.line_width - len(line))
        ending = "\n" if final else ""
        self.output.write(f"\r{line}{padding}{ending}")
        self.output.flush()
        self.line_width = 0 if final else len(line)


def get_content_length(value: Optional[str]) -> Optional[int]:
    try:
        length = int(value) if value is not None else -1
    except ValueError:
        return None
    return length if length >= 0 else None


def download(
    url: str,
    timeout: float,
    on_progress: Callable[[int, Optional[int]], None],
) -> Measurement:
    """Read a response completely without saving it to disk."""
    request = Request(
        url,
        headers={"Accept-Encoding": "identity", "User-Agent": "speedcheck/1.0"},
    )
    started = time.perf_counter()
    downloaded = 0

    try:
        with urlopen(request, timeout=timeout) as response:
            total = get_content_length(response.headers.get("Content-Length"))
            on_progress(0, total)
            while chunk := response.read(CHUNK_SIZE):
                downloaded += len(chunk)
                on_progress(downloaded, total)
    except HTTPError as error:
        message = f"сервер вернул HTTP {error.code} ({error.reason})"
        error.close()
        raise SpeedcheckError(message) from error
    except URLError as error:
        raise SpeedcheckError(f"не удалось выполнить запрос: {error.reason}") from error
    except TimeoutError as error:
        raise SpeedcheckError(f"истёк тайм-аут {timeout:g} с") from error
    except OSError as error:
        raise SpeedcheckError(f"ошибка сети: {error}") from error

    return Measurement(time.perf_counter() - started, downloaded)


def run_measurements(
    url: str,
    count: int,
    timeout: float,
    output: Optional[TextIO] = None,
) -> list[Measurement]:
    output = output or sys.stdout
    measurements = []

    for number in range(1, count + 1):
        progress = Progress(output, number, count)
        try:
            result = download(url, timeout, progress.update)
        except KeyboardInterrupt:
            progress.stop("остановлен")
            raise
        except SpeedcheckError as error:
            progress.stop("ошибка")
            raise SpeedcheckError(
                f"запрос {number}/{count} завершился ошибкой: {error}"
            ) from error

        measurements.append(result)
        progress.finish(result)

    return measurements


def validate_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError(
            "адрес должен быть полным HTTP(S) URL, например https://example.com/file.jpg"
        )
    return value


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("должно быть целым числом") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("должно быть больше нуля")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("должно быть числом") from error
    if number <= 0 or not math.isfinite(number):
        raise argparse.ArgumentTypeError("должно быть конечным числом больше нуля")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Последовательно скачивает файл и измеряет скорость загрузки."
    )
    parser.add_argument("url", type=validate_url, help="HTTP(S) адрес большого файла")
    parser.add_argument(
        "-n",
        "--count",
        type=positive_int,
        default=DEFAULT_REQUEST_COUNT,
        help=f"количество запросов (по умолчанию: {DEFAULT_REQUEST_COUNT})",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"тайм-аут сетевой операции (по умолчанию: {DEFAULT_TIMEOUT_SECONDS:g} с)",
    )
    return parser


def print_summary(measurements: Sequence[Measurement]) -> None:
    total_seconds = sum(result.seconds for result in measurements)
    total_bytes = sum(result.byte_count for result in measurements)
    speed = megabytes_per_second(total_bytes, total_seconds)

    print("\nИтог:")
    print(f"  Успешных запросов:       {len(measurements)}")
    print(f"  Среднее время запроса:   {total_seconds / len(measurements):.3f} с")
    print(f"  Скачано данных:          {format_megabytes(total_bytes)} ({total_bytes} байт)")
    print(f"  Средняя скорость:        {speed:.2f} МБ/с")
    print(f"                           {speed * 8:.2f} Мбит/с")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"Адрес: {args.url}")
    print(f"Последовательных запросов: {args.count}\n")

    try:
        measurements = run_measurements(args.url, args.count, args.timeout)
    except KeyboardInterrupt:
        print("Остановлено пользователем.", file=sys.stderr)
        return 130
    except SpeedcheckError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1

    print_summary(measurements)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
