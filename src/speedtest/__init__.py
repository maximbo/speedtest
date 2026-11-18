"""Консольная утилита для последовательного замера скорости скачивания по HTTP/HTTPS."""

import argparse
import csv
import io
import json
import socket
import statistics
import sys
import time
from dataclasses import dataclass, replace
from enum import Enum
from functools import cached_property
from typing import Any
from urllib.parse import urlparse

import httpx

from typing import assert_never


class OutputFormat(str, Enum):
    TEXT = "text"
    JSON = "json"
    CSV = "csv"
    PROMETHEUS = "prometheus"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=False)
class Config:
    url: str
    requests_count: int = 10
    timeout: float = 10.0
    chunk_size: int = 65536  # 64 KiB
    user_agent: str = "Mozilla/5.0 (compatible; SpeedCheck/1.0)"
    proxy: str | None = None
    disable_keep_alive: bool = False
    display_progress: bool = True
    headers_override: tuple[tuple[str, str], ...] = ()
    output_format: OutputFormat = OutputFormat.TEXT
    http2: bool = False


@dataclass(frozen=True, slots=False)
class RequestStats:
    bytes: int = 0
    duration: float = 0
    ttfb: float = 0.0
    dns_ms: float = 0.0
    headers_ms: float = 0.0
    body_ms: float = 0.0
    req_ms: float = 0.0
    tcp_ms: float = 0.0
    tls_ms: float = 0.0
    failed: bool = False

    @property
    def mbps(self) -> float:
        return (
            (self.bytes * 8) / (self.duration * 1_000_000) if self.duration > 0 else 0.0
        )

    @property
    def ttfb_ms(self) -> float:
        return self.ttfb * 1000

    def to_dict(self) -> dict[str, int | float | bool]:
        return {
            "bytes": self.bytes,
            "duration": round(self.duration, 4),
            "mbps": round(self.mbps, 2),
            "ttfb_ms": round(self.ttfb_ms, 2),
            "headers_ms": round(self.headers_ms, 3),
            "dns_ms": round(self.dns_ms, 3),
            "tcp_ms": round(self.tcp_ms, 3),
            "tls_ms": round(self.tls_ms, 3),
            "req_ms": round(self.req_ms, 3),
            "body_ms": round(self.body_ms, 3),
            "failed": self.failed,
        }


@dataclass(frozen=True, slots=False)
class TotalStats:
    url: str
    count: int
    requests: list[RequestStats]

    @cached_property
    def successful_requests(self) -> list[RequestStats]:
        return [req for req in self.requests if not req.failed]

    @cached_property
    def failed_requests(self) -> list[RequestStats]:
        return [req for req in self.requests if req.failed]

    @cached_property
    def total_bytes(self) -> int:
        return sum(req.bytes for req in self.successful_requests)

    @cached_property
    def total_duration(self) -> float:
        return sum(req.duration for req in self.successful_requests)

    @cached_property
    def avg_duration(self) -> float:
        return (
            self.total_duration / len(self.successful_requests)
            if self.successful_requests
            else 0.0
        )

    @cached_property
    def avg_speed_mbps(self) -> float:
        return (
            (self.total_bytes * 8) / (self.total_duration * 1_000_000)
            if self.total_duration > 0
            else 0.0
        )

    @cached_property
    def avg_speed_mbytes(self) -> float:
        return (
            (self.total_bytes / (1024 * 1024)) / self.total_duration
            if self.total_duration > 0
            else 0.0
        )

    @cached_property
    def avg_ttfb_ms(self) -> float:
        return (
            statistics.mean(req.ttfb_ms for req in self.successful_requests)
            if self.successful_requests
            else 0.0
        )

    @cached_property
    def avg_tcp_ms(self) -> float:
        return self._avg("tcp_ms")

    @cached_property
    def avg_tls_ms(self) -> float:
        return self._avg("tls_ms")

    @cached_property
    def avg_req_ms(self) -> float:
        return self._avg("req_ms")

    @cached_property
    def avg_headers_ms(self) -> float:
        return self._avg("headers_ms")

    @cached_property
    def avg_body_ms(self) -> float:
        return self._avg("body_ms")

    @cached_property
    def avg_dns_ms(self) -> float:
        return self._avg("dns_ms")

    def _avg(self, attr: str) -> float:
        reqs = self.successful_requests
        return statistics.mean(getattr(r, attr) for r in reqs) if reqs else 0.0

    @cached_property
    def min_speed_mbps(self) -> float:
        return min((req.mbps for req in self.successful_requests), default=0.0)

    @cached_property
    def max_speed_mbps(self) -> float:
        return max((req.mbps for req in self.successful_requests), default=0.0)

    @cached_property
    def median_speed_mbps(self) -> float:
        return (
            statistics.median(req.mbps for req in self.successful_requests)
            if self.successful_requests
            else 0.0
        )

    @cached_property
    def error_count(self) -> int:
        return len(self.failed_requests)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "count": self.count,
            "error_count": self.error_count,
            "total_bytes": self.total_bytes,
            "total_duration": round(self.total_duration, 4),
            "avg_duration": round(self.avg_duration, 4),
            "avg_speed_mbps": round(self.avg_speed_mbps, 2),
            "avg_speed_mbytes": round(self.avg_speed_mbytes, 2),
            "avg_ttfb_ms": round(self.avg_ttfb_ms, 2),
            "avg_dns_ms": round(self.avg_dns_ms, 3),
            "avg_tcp_ms": round(self.avg_tcp_ms, 3),
            "avg_tls_ms": round(self.avg_tls_ms, 3),
            "avg_req_ms": round(self.avg_req_ms, 3),
            "avg_headers_ms": round(self.avg_headers_ms, 3),
            "avg_body_ms": round(self.avg_body_ms, 3),
            "min_speed_mbps": round(self.min_speed_mbps, 2),
            "max_speed_mbps": round(self.max_speed_mbps, 2),
            "median_speed_mbps": round(self.median_speed_mbps, 2),
            "requests": [req.to_dict() for req in self.requests],
        }


def resolve_with_timing(host: str, port: int = 443) -> tuple[str | None, float]:
    """DNS-резолвинг; возвращает (IP или None, время в мс)."""
    start = time.perf_counter()
    try:
        infos = socket.getaddrinfo(
            host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
        ip = infos[0][4][0]
    except socket.gaierror:
        ip = None
    return ip, (time.perf_counter() - start) * 1000


def fetch_once(
    url: str,
    client: httpx.Client,
    config: Config,
) -> RequestStats:
    """Один HTTP-запрос: стриминг тела чанками + сбор trace-метрик."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Некорректный URL: {url!r}")

    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    _, dns_ms = resolve_with_timing(host, port)

    marks: dict[str, float] = {}

    def trace(event_name: str, info: dict) -> None:
        t = time.perf_counter()
        match event_name:
            case "connection.connect_tcp.started":
                marks["tcp_start"] = t
            case "connection.connect_tcp.complete":
                marks["tcp_end"] = t
            case "connection.start_tls.started":
                marks["tls_start"] = t
            case "connection.start_tls.complete":
                marks["tls_end"] = t
            case "http11.send_request_headers.started":
                marks["req_start"] = t
            case "http11.send_request_headers.complete":
                marks["req_end"] = t
            case "http11.receive_response_headers.complete":
                marks["headers_end"] = t
            case "http11.receive_response_body.started":
                marks["body_start"] = t
            case "http11.receive_response_body.complete":
                marks["body_end"] = t
            case "http11.response_closed.complete":
                marks["closed_end"] = t
            # HTTP/2
            case "http2.send_request_headers.started":
                marks["req_start"] = t
            case "http2.send_request_headers.complete":
                marks["req_end"] = t
            case "http2.receive_response_headers.complete":
                marks["headers_end"] = t
            case "http2.receive_response_body.started":
                marks["body_start"] = t
            case "http2.receive_response_body.complete":
                marks["body_end"] = t
            case "http2.response_closed.complete":
                marks["closed_end"] = t

    total_bytes = 0
    ttfb = 0.0
    headers_time = 0.0
    failed = True
    start_time = time.perf_counter()

    try:
        # URL не подменяем на IP — TLS-сертификат проверяется по домену.
        # Заголовки клиента не мутируем — Host передаём per-request.
        with client.stream(
            "GET",
            url,
            extensions={"trace": trace},
            headers={"Host": host},
            follow_redirects=False,
        ) as response:
            headers_time = time.perf_counter() - start_time
            response.raise_for_status()

            iterator = response.iter_bytes(chunk_size=config.chunk_size)
            first_chunk = next(iterator, b"")
            ttfb = time.perf_counter() - start_time

            if first_chunk:
                total_bytes += len(first_chunk)
            for chunk in iterator:
                total_bytes += len(chunk)

            failed = False
    except httpx.HTTPStatusError as e:
        print(
            f"HTTP ошибка при запросе: {e.response.status_code} {e.response.reason_phrase}",
            file=sys.stderr,
        )
    except httpx.RequestError as e:
        print(f"Сетевая ошибка: {e}", file=sys.stderr)
    except httpx.StreamError as e:
        print(f"Ошибка потока: {e}", file=sys.stderr)
    except httpx.HTTPError as e:
        print(f"Общая ошибка HTTP: {e}", file=sys.stderr)

    elapsed = time.perf_counter() - start_time

    def diff(a: str, b: str) -> float:
        return (marks.get(b, 0.0) - marks.get(a, 0.0)) * 1000

    return RequestStats(
        bytes=total_bytes,
        duration=elapsed,
        ttfb=ttfb,
        dns_ms=dns_ms,
        headers_ms=headers_time * 1000,
        body_ms=diff("body_start", "body_end"),
        req_ms=diff("req_start", "req_end"),
        tcp_ms=diff("tcp_start", "tcp_end"),
        tls_ms=diff("tls_start", "tls_end"),
        failed=failed,
    )


def run_benchmark(
    config: Config,
    transport: httpx.BaseTransport | None = None,
) -> TotalStats:
    if config.requests_count <= 0:
        raise ValueError("Количество запросов должно быть больше нуля.")

    headers = httpx.Headers(
        [
            ("Cache-Control", "no-cache"),
            ("Cache-Control", "no-store"),
            ("Pragma", "no-cache"),
            ("User-Agent", config.user_agent),
        ]
    )
    if config.headers_override:
        headers.update(config.headers_override)

    if config.display_progress:
        print(f"Цель:       {config.url}", file=sys.stderr)
        if config.proxy:
            print(f"Прокси:     {config.proxy}", file=sys.stderr)
        print(
            f"Keep-Alive: {'нет' if config.disable_keep_alive else 'да'}\n",
            file=sys.stderr,
        )
        print(
            f"Запросов:   {config.requests_count} (последовательно)\n", file=sys.stderr
        )

    limits = (
        httpx.Limits(max_keepalive_connections=0)
        if config.disable_keep_alive
        else httpx.Limits()
    )

    stats = TotalStats(url=config.url, count=config.requests_count, requests=[])

    with httpx.Client(
        headers=headers,
        timeout=config.timeout,
        proxy=config.proxy,
        http2=config.http2,
        limits=limits,
        follow_redirects=True,
        transport=transport,
    ) as client:
        for i in range(1, config.requests_count + 1):
            request = fetch_once(config.url, client, config)
            stats.requests.append(request)

            if config.display_progress:
                print(
                    f"[{i:02d}/{config.requests_count:02d}] "
                    f"Ошибка: {'Да' if request.failed else 'Нет'} | "
                    f"Скачано: {request.bytes / (1024 * 1024):.2f} MiB | "
                    f"TTFB: {request.ttfb_ms:.1f} мс | "
                    f"TCP: {request.tcp_ms:.1f} | TLS: {request.tls_ms:.1f} | "
                    f"Req: {request.req_ms:.1f} | Body: {request.body_ms:.1f} | "
                    f"Время: {request.duration:.3f} с | "
                    f"Скорость: {request.mbps:.2f} Mbps",
                    file=sys.stderr,
                )

    return stats


def format_bytes(num_bytes: int) -> str:
    """Десятичные MB и двоичные MiB."""
    mb = num_bytes / 1_000_000
    mib = num_bytes / (1024 * 1024)
    return f"{num_bytes:,} Б ({mb:.2f} MB / {mib:.2f} MiB)".replace(",", " ")


def format_total_stats_as_text(stats: TotalStats) -> str:
    return "\n".join(
        [
            f"URL:                   {stats.url}",
            f"Запросов:              {stats.count}",
            f"Ошибок:                {stats.error_count}",
            f"Всего скачано:         {format_bytes(stats.total_bytes)}",
            f"Общее время загрузки:  {stats.total_duration:.3f} s",
            f"Среднее время запроса: {stats.avg_duration:.3f} s",
            f"Средняя скорость:      {stats.avg_speed_mbps:.2f} Mbps ({stats.avg_speed_mbytes:.2f} MiB/s)",
            f"Медиана / Min / Max:   {stats.median_speed_mbps:.2f} / {stats.min_speed_mbps:.2f} / {stats.max_speed_mbps:.2f} Mbps",
            "",
            "─── Средние тайминги (по успешным запросам) ───",
            f"DNS:                   {stats.avg_dns_ms:.2f} ms",
            f"TCP connect:           {stats.avg_tcp_ms:.2f} ms",
            f"TLS handshake:         {stats.avg_tls_ms:.2f} ms",
            f"Отправка запроса:      {stats.avg_req_ms:.2f} ms",
            f"Ожидание заголовков:   {stats.avg_headers_ms:.2f} ms",
            f"TTFB (до 1-го байта):  {stats.avg_ttfb_ms:.2f} ms",
            f"Скачивание тела:       {stats.avg_body_ms:.2f} ms",
        ]
    )


def format_total_stats_as_json(stats: TotalStats) -> str:
    return json.dumps(stats.to_dict(), ensure_ascii=False)


def format_total_stats_as_csv(stats: TotalStats) -> str:
    buffer = io.StringIO()
    fieldnames = [
        "request_index",
        "url",
        "bytes",
        "duration_sec",
        "mbps",
        "dns_ms",
        "tcp_ms",
        "tls_ms",
        "req_ms",
        "headers_ms",
        "ttfb_ms",
        "body_ms",
        "failed",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()

    for idx, req in enumerate(stats.requests, start=1):
        writer.writerow(
            {
                "request_index": idx,
                "url": stats.url,
                "bytes": req.bytes,
                "duration_sec": round(req.duration, 4),
                "mbps": round(req.mbps, 2),
                "dns_ms": round(req.dns_ms, 3),
                "tcp_ms": round(req.tcp_ms, 3),
                "tls_ms": round(req.tls_ms, 3),
                "req_ms": round(req.req_ms, 3),
                "headers_ms": round(req.headers_ms, 3),
                "ttfb_ms": round(req.ttfb_ms, 2),
                "body_ms": round(req.body_ms, 3),
                "failed": int(req.failed),
            }
        )

    return buffer.getvalue().rstrip("\n")


def _escape_prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_total_stats_as_prometheus(stats: TotalStats) -> str:
    label = f'url="{_escape_prom_label(stats.url)}"'
    lines = [
        "# HELP speedtest_downloaded_bytes_total Total downloaded bytes across all requests.",
        "# TYPE speedtest_downloaded_bytes_total counter",
        f"speedtest_downloaded_bytes_total{{{label}}} {stats.total_bytes}",
        "# HELP speedtest_errors_total Number of failed requests.",
        "# TYPE speedtest_errors_total counter",
        f"speedtest_errors_total{{{label}}} {stats.error_count}",
        "# HELP speedtest_request_duration_seconds_avg Average HTTP request duration in seconds.",
        "# TYPE speedtest_request_duration_seconds_avg gauge",
        f"speedtest_request_duration_seconds_avg{{{label}}} {stats.avg_duration:.4f}",
        "# HELP speedtest_ttfb_milliseconds_avg Average Time To First Byte in milliseconds.",
        "# TYPE speedtest_ttfb_milliseconds_avg gauge",
        f"speedtest_ttfb_milliseconds_avg{{{label}}} {stats.avg_ttfb_ms:.2f}",
        "# HELP speedtest_timing_milliseconds_avg Average timing breakdown in milliseconds.",
        "# TYPE speedtest_timing_milliseconds_avg gauge",
        f'speedtest_timing_milliseconds_avg{{{label},phase="dns"}} {stats.avg_dns_ms:.2f}',
        f'speedtest_timing_milliseconds_avg{{{label},phase="tcp"}} {stats.avg_tcp_ms:.2f}',
        f'speedtest_timing_milliseconds_avg{{{label},phase="tls"}} {stats.avg_tls_ms:.2f}',
        f'speedtest_timing_milliseconds_avg{{{label},phase="request"}} {stats.avg_req_ms:.2f}',
        f'speedtest_timing_milliseconds_avg{{{label},phase="headers"}} {stats.avg_headers_ms:.2f}',
        f'speedtest_timing_milliseconds_avg{{{label},phase="body"}} {stats.avg_body_ms:.2f}',
        "# HELP speedtest_download_speed_mbps Download speed statistics in Mbps.",
        "# TYPE speedtest_download_speed_mbps gauge",
        f'speedtest_download_speed_mbps{{{label},stat="avg"}} {stats.avg_speed_mbps:.2f}',
        f'speedtest_download_speed_mbps{{{label},stat="median"}} {stats.median_speed_mbps:.2f}',
        f'speedtest_download_speed_mbps{{{label},stat="min"}} {stats.min_speed_mbps:.2f}',
        f'speedtest_download_speed_mbps{{{label},stat="max"}} {stats.max_speed_mbps:.2f}',
    ]
    return "\n".join(lines)


def _is_ip(host: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return True
        except OSError:
            continue
    return False


def normalize_and_validate_url(raw_url: str) -> str | None:
    url = raw_url.strip()
    if not url:
        return None

    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if " " in url:
        return None

    host = parsed.hostname or ""
    if not host:
        return None
    if _is_ip(host) or host == "localhost" or "." in host:
        return url
    return None


def config_from_args(argv: list[str] | None = None) -> Config:
    def parse_header(item: str) -> tuple[str, str]:
        key, sep, value = item.partition(":")
        key, value = key.strip(), value.strip()
        if not key or not value or sep != ":":
            raise argparse.ArgumentTypeError(
                f"Неверный HTTP заголовок '{item}'. Ожидается 'ключ:значение'."
            )
        return key, value

    def parse_proxy(value: str) -> str:
        allowed_schemes = ("http://", "https://", "socks5://", "socks5h://")
        if not value.startswith(allowed_schemes):
            raise argparse.ArgumentTypeError(
                f"Неподдерживаемая схема прокси '{value}'. "
                f"Допустимы: {', '.join(allowed_schemes)}"
            )
        return value

    parser = argparse.ArgumentParser(
        description="Последовательный замер скорости скачивания по указанному URL."
    )

    parser.add_argument(
        "-H", "--headers", type=parse_header, action="append", default=[]
    )
    parser.add_argument(
        "-x",
        "--proxy",
        type=parse_proxy,
        default=None,
        help="URL прокси-сервера (например, http://127.0.0.1:8080 или socks5://127.0.0.1:1080)",
    )
    parser.add_argument(
        "url",
        help="Целевой URL для скачивания (например, ссылка на тяжёлое изображение или файл)",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=Config.requests_count,
        help=f"Количество последовательных запросов (по умолчанию: {Config.requests_count})",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=Config.timeout,
        help=f"Таймаут одного запроса в секундах (по умолчанию: {Config.timeout})",
    )
    parser.add_argument(
        "-p",
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Показывать прогресс по каждому запросу в stderr (по умолчанию: включено, отключить: --no-progress)",
    )
    parser.add_argument(
        "-f",
        "--format",
        type=OutputFormat,
        choices=list(OutputFormat),
        default=OutputFormat.TEXT,
        help="Формат вывода: text, json, csv, prometheus (по умолчанию: text)",
    )
    parser.add_argument(
        "--no-keep-alive",
        action="store_true",
        help="Отключить keep-alive",
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default=Config.user_agent,
        help="User-Agent",
    )
    parser.add_argument(
        "--http2",
        action="store_true",
        help="Использовать HTTP/2",
    )

    args = parser.parse_args(argv)
    url = normalize_and_validate_url(args.url)
    if not url:
        print("Ошибка: неверный url", file=sys.stderr)
        sys.exit(1)

    return replace(
        Config(url=url),
        requests_count=args.count,
        timeout=args.timeout,
        user_agent=args.user_agent,
        output_format=args.format,
        proxy=args.proxy,
        disable_keep_alive=args.no_keep_alive,
        display_progress=args.progress,
        http2=args.http2,
        headers_override=tuple(args.headers),
    )


def main(argv: list[str] | None = None) -> None:
    config = config_from_args(argv)

    try:
        total_stats = run_benchmark(config)
    except KeyboardInterrupt:
        print("Замер прерван пользователем.", file=sys.stderr)
        sys.exit(130)
    except ValueError as e:
        print(f"Ошибка параметров: {e}", file=sys.stderr)
        sys.exit(1)
    else:
        match config.output_format:
            case OutputFormat.JSON:
                output = format_total_stats_as_json(total_stats)
            case OutputFormat.TEXT:
                output = format_total_stats_as_text(total_stats)
            case OutputFormat.PROMETHEUS:
                output = format_total_stats_as_prometheus(total_stats)
            case OutputFormat.CSV:
                output = format_total_stats_as_csv(total_stats)
            case _:
                assert_never(config.output_format)
        print(output)


if __name__ == "__main__":
    main()
