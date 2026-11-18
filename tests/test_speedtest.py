"""Тесты для консольной утилиты замера скорости скачивания."""

from __future__ import annotations

import csv
import io
import json
import socket
from collections.abc import Iterator

import httpx
import pytest

from speedtest import (
    Config,
    OutputFormat,
    RequestStats,
    TotalStats,
    config_from_args,
    fetch_once,
    format_total_stats_as_csv,
    format_total_stats_as_json,
    format_total_stats_as_prometheus,
    format_total_stats_as_text,
    normalize_and_validate_url,
    resolve_with_timing,
    run_benchmark,
)

# ─────────────────────────────────────────────────────────────────────────────
# Фикстуры
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def payload() -> bytes:
    return b"x" * 100_000


@pytest.fixture
def ok_transport(payload: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    return httpx.MockTransport(handler)


@pytest.fixture
def client(ok_transport: httpx.MockTransport) -> Iterator[httpx.Client]:
    with httpx.Client(transport=ok_transport) as c:
        yield c


@pytest.fixture
def base_config() -> Config:
    return Config(url="https://example.com/file.bin", requests_count=3)


# ─────────────────────────────────────────────────────────────────────────────
# normalize_and_validate_url
# ─────────────────────────────────────────────────────────────────────────────


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("https://example.com", "https://example.com"),
            ("http://example.com/path?q=1", "http://example.com/path?q=1"),
            ("example.com", "https://example.com"),
            ("  example.com  ", "https://example.com"),
            ("http://127.0.0.1:8080/f", "http://127.0.0.1:8080/f"),
            ("http://localhost:8000", "http://localhost:8000"),
            ("http://[::1]:8000/f", "http://[::1]:8000/f"),
        ],
    )
    def test_valid(self, raw: str, expected: str) -> None:
        assert normalize_and_validate_url(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "ftp://example.com",
            "http://",
            "http://exa mple.com",
            "just-a-word",
            "http:///path",
        ],
    )
    def test_invalid(self, raw: str) -> None:
        assert normalize_and_validate_url(raw) is None

    def test_ipv4_accepted(self) -> None:
        assert normalize_and_validate_url("http://192.168.1.1/x") is not None

    def test_ipv6_accepted(self) -> None:
        assert normalize_and_validate_url("http://[::1]/x") is not None


# ─────────────────────────────────────────────────────────────────────────────
# resolve_with_timing
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveWithTiming:
    def test_returns_ip_and_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_getaddrinfo(*a, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        ip, ms = resolve_with_timing("example.com", 443)
        assert ip == "1.2.3.4"
        assert ms >= 0.0

    def test_returns_none_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_getaddrinfo(*a, **kw):
            raise socket.gaierror("nope")

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        ip, ms = resolve_with_timing("bad.invalid", 443)
        assert ip is None
        assert ms >= 0.0


# ─────────────────────────────────────────────────────────────────────────────
# RequestStats / TotalStats
# ─────────────────────────────────────────────────────────────────────────────


class TestRequestStats:
    def test_mbps_computed(self) -> None:
        r = RequestStats(bytes=1_000_000, duration=1.0)
        assert r.mbps == pytest.approx(8.0)

    def test_mbps_zero_duration(self) -> None:
        assert RequestStats(bytes=100, duration=0.0).mbps == 0.0

    def test_ttfb_ms(self) -> None:
        assert RequestStats(ttfb=0.123).ttfb_ms == pytest.approx(123.0)

    def test_to_dict_contains_trace_fields(self) -> None:
        r = RequestStats(
            bytes=10,
            duration=0.1,
            ttfb=0.01,
            dns_ms=1,
            tcp_ms=2,
            tls_ms=3,
            req_ms=4,
            headers_ms=5,
            body_ms=6,
        )
        d = r.to_dict()
        for key in ("tcp_ms", "tls_ms", "req_ms", "body_ms", "dns_ms", "failed"):
            assert key in d


class TestTotalStats:
    def test_aggregates(self) -> None:
        reqs = [
            RequestStats(bytes=1_000_000, duration=1.0, ttfb=0.1),
            RequestStats(bytes=2_000_000, duration=2.0, ttfb=0.2),
        ]
        s = TotalStats(url="https://x/y", count=2, requests=reqs)
        assert s.total_bytes == 3_000_000
        assert s.total_duration == pytest.approx(3.0)
        assert s.avg_duration == pytest.approx(1.5)
        assert s.avg_speed_mbps == pytest.approx(8.0)
        assert s.min_speed_mbps == pytest.approx(8.0)
        assert s.max_speed_mbps == pytest.approx(8.0)
        assert s.median_speed_mbps == pytest.approx(8.0)
        assert s.error_count == 0

    def test_failed_excluded(self) -> None:
        reqs = [
            RequestStats(bytes=1_000_000, duration=1.0),
            RequestStats(bytes=0, duration=0.5, failed=True),
        ]
        s = TotalStats(url="u", count=2, requests=reqs)
        assert s.error_count == 1
        assert s.total_bytes == 1_000_000
        assert len(s.successful_requests) == 1

    def test_empty(self) -> None:
        s = TotalStats(url="u", count=0, requests=[])
        assert s.avg_speed_mbps == 0.0
        assert s.avg_ttfb_ms == 0.0
        assert s.min_speed_mbps == 0.0
        assert s.max_speed_mbps == 0.0
        assert s.median_speed_mbps == 0.0
        assert s.error_count == 0

    def test_avg_timings(self) -> None:
        reqs = [
            RequestStats(
                tcp_ms=10, tls_ms=20, req_ms=1, headers_ms=2, body_ms=3, dns_ms=4
            ),
            RequestStats(
                tcp_ms=30, tls_ms=40, req_ms=5, headers_ms=6, body_ms=7, dns_ms=8
            ),
        ]
        s = TotalStats(url="u", count=2, requests=reqs)
        assert s.avg_tcp_ms == pytest.approx(20.0)
        assert s.avg_tls_ms == pytest.approx(30.0)
        assert s.avg_req_ms == pytest.approx(3.0)
        assert s.avg_headers_ms == pytest.approx(4.0)
        assert s.avg_body_ms == pytest.approx(5.0)
        assert s.avg_dns_ms == pytest.approx(6.0)


# ─────────────────────────────────────────────────────────────────────────────
# fetch_once
# ─────────────────────────────────────────────────────────────────────────────


class TestFetchOnce:
    def test_success(self, client: httpx.Client, payload: bytes) -> None:
        stats = fetch_once(
            "https://example.com/file.bin",
            client,
            Config(url="https://example.com/file.bin"),
        )
        assert stats.failed is False
        assert stats.bytes == len(payload)
        assert stats.duration > 0
        assert stats.ttfb > 0
        assert stats.dns_ms >= 0

    def test_http_error_marks_failed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"nope")

        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            stats = fetch_once(
                "https://example.com/missing",
                c,
                Config(url="https://example.com/missing"),
            )
        assert stats.failed is True
        assert stats.bytes == 0

    def test_network_error_marks_failed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            stats = fetch_once(
                "https://example.com/x", c, Config(url="https://example.com/x")
            )
        assert stats.failed is True
        assert stats.bytes == 0

    def test_invalid_url_raises(self, client: httpx.Client) -> None:
        with pytest.raises(ValueError):
            fetch_once("ftp://example.com/x", client, Config(url="ftp://example.com/x"))

    def test_chunked_read_sums_bytes(self) -> None:
        chunks = [b"a" * 10, b"b" * 20, b"c" * 30]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=iter(chunks))

        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            stats = fetch_once(
                "https://example.com/f",
                c,
                Config(url="https://example.com/f", chunk_size=8),
            )
        assert stats.bytes == 60

    def test_empty_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            stats = fetch_once(
                "https://example.com/e", c, Config(url="https://example.com/e")
            )
        assert stats.failed is False
        assert stats.bytes == 0


# ─────────────────────────────────────────────────────────────────────────────
# run_benchmark
# ─────────────────────────────────────────────────────────────────────────────


class TestRunBenchmark:
    def test_runs_n_requests(self, payload: bytes) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, content=payload)

        transport = httpx.MockTransport(handler)
        config = Config(
            url="https://example.com/f", requests_count=5, display_progress=False
        )
        stats = run_benchmark(config, transport=transport)
        assert calls["n"] == 5
        assert stats.count == 5
        assert len(stats.requests) == 5
        assert stats.total_bytes == 5 * len(payload)

    def test_zero_requests_raises(self) -> None:
        with pytest.raises(ValueError, match="больше нуля"):
            run_benchmark(
                Config(
                    url="https://example.com", requests_count=0, display_progress=False
                )
            )

    def test_negative_requests_raises(self) -> None:
        with pytest.raises(ValueError):
            run_benchmark(
                Config(
                    url="https://example.com", requests_count=-1, display_progress=False
                )
            )

    def test_progress_output(
        self, payload: bytes, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=payload)

        config = Config(
            url="https://example.com/f", requests_count=2, display_progress=True
        )
        run_benchmark(config, transport=httpx.MockTransport(handler))
        err = capsys.readouterr().err
        assert "Цель:" in err
        assert "[01/02]" in err
        assert "[02/02]" in err

    def test_no_progress_output(
        self, payload: bytes, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=payload)

        config = Config(
            url="https://example.com/f", requests_count=2, display_progress=False
        )
        run_benchmark(config, transport=httpx.MockTransport(handler))
        err = capsys.readouterr().err
        assert err == ""

    def test_headers_override_applied(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, content=b"ok")

        config = Config(
            url="https://example.com/f",
            requests_count=1,
            display_progress=False,
            headers_override=(("X-Custom", "yes"),),
        )
        run_benchmark(config, transport=httpx.MockTransport(handler))
        assert seen.get("x-custom") == "yes"
        assert seen.get("user-agent", "").startswith("Mozilla")


# ─────────────────────────────────────────────────────────────────────────────
# Форматтеры
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_stats() -> TotalStats:
    reqs = [
        RequestStats(
            bytes=1_000_000,
            duration=1.0,
            ttfb=0.05,
            dns_ms=2,
            tcp_ms=3,
            tls_ms=4,
            req_ms=1,
            headers_ms=5,
            body_ms=900,
        ),
        RequestStats(
            bytes=2_000_000,
            duration=2.0,
            ttfb=0.10,
            dns_ms=4,
            tcp_ms=6,
            tls_ms=8,
            req_ms=2,
            headers_ms=10,
            body_ms=1800,
        ),
    ]
    return TotalStats(url="https://example.com/file.bin", count=2, requests=reqs)


class TestFormatters:
    def test_json(self, sample_stats: TotalStats) -> None:
        data = json.loads(format_total_stats_as_json(sample_stats))
        assert data["url"] == "https://example.com/file.bin"
        assert data["count"] == 2
        assert data["error_count"] == 0
        assert data["total_bytes"] == 3_000_000
        assert "avg_tcp_ms" in data
        assert "avg_tls_ms" in data
        assert "avg_body_ms" in data
        assert len(data["requests"]) == 2

    def test_csv(self, sample_stats: TotalStats) -> None:
        text = format_total_stats_as_csv(sample_stats)
        rows = list(csv.DictReader(io.StringIO(text)))
        assert len(rows) == 2
        assert rows[0]["request_index"] == "1"
        assert rows[0]["bytes"] == "1000000"
        for col in (
            "dns_ms",
            "tcp_ms",
            "tls_ms",
            "req_ms",
            "headers_ms",
            "ttfb_ms",
            "body_ms",
            "failed",
        ):
            assert col in rows[0]

    def test_csv_no_trailing_newline(self, sample_stats: TotalStats) -> None:
        assert not format_total_stats_as_csv(sample_stats).endswith("\n")

    def test_prometheus(self, sample_stats: TotalStats) -> None:
        text = format_total_stats_as_prometheus(sample_stats)
        assert (
            'speedtest_downloaded_bytes_total{url="https://example.com/file.bin"} 3000000'
            in text
        )
        assert 'stat="avg"' in text
        assert 'stat="median"' in text
        assert 'stat="min"' in text
        assert 'stat="max"' in text
        assert 'phase="dns"' in text
        assert 'phase="tcp"' in text
        assert 'phase="tls"' in text
        assert 'phase="request"' in text
        assert 'phase="headers"' in text
        assert 'phase="body"' in text
        assert "speedtest_errors_total" in text

    def test_prometheus_escapes_quotes(self) -> None:
        s = TotalStats(
            url='https://x/"weird"',
            count=1,
            requests=[RequestStats(bytes=1, duration=1)],
        )
        text = format_total_stats_as_prometheus(s)
        assert '\\"weird\\"' in text

    def test_text(self, sample_stats: TotalStats) -> None:
        text = format_total_stats_as_text(sample_stats)
        assert "URL:" in text
        assert "https://example.com/file.bin" in text
        assert "Средняя скорость:" in text
        assert "DNS:" in text
        assert "TCP connect:" in text
        assert "TLS handshake:" in text
        assert "Скачивание тела:" in text


# ─────────────────────────────────────────────────────────────────────────────
# config_from_args
# ─────────────────────────────────────────────────────────────────────────────


class TestConfigFromArgs:
    def test_defaults(self) -> None:
        cfg = config_from_args(["https://example.com/f"])
        assert cfg.url == "https://example.com/f"
        assert cfg.requests_count == 10
        assert cfg.timeout == 10.0
        assert cfg.output_format == OutputFormat.TEXT
        assert cfg.http2 is False
        assert cfg.proxy is None
        assert cfg.disable_keep_alive is False

    def test_all_options(self) -> None:
        cfg = config_from_args(
            [
                "example.com",
                "-n",
                "5",
                "-t",
                "3.5",
                "-f",
                "json",
                "-x",
                "http://127.0.0.1:8080",
                "--no-keep-alive",
                "--http2",
                "--no-progress",
                "--user-agent",
                "TestUA/1.0",
                "-H",
                "X-A: 1",
                "-H",
                "X-B: 2",
            ]
        )
        assert cfg.url == "https://example.com"
        assert cfg.requests_count == 5
        assert cfg.timeout == 3.5
        assert cfg.output_format == OutputFormat.JSON
        assert cfg.proxy == "http://127.0.0.1:8080"
        assert cfg.disable_keep_alive is True
        assert cfg.http2 is True
        assert cfg.display_progress is False
        assert cfg.user_agent == "TestUA/1.0"
        assert cfg.headers_override == (("X-A", "1"), ("X-B", "2"))

    def test_invalid_url_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            config_from_args(["ftp://example.com"])
        assert exc.value.code == 1
        assert "неверный url" in capsys.readouterr().err

    @pytest.mark.parametrize("fmt", ["text", "json", "csv", "prometheus"])
    def test_each_format(self, fmt: str) -> None:
        cfg = config_from_args(["https://example.com", "-f", fmt])
        assert cfg.output_format == OutputFormat(fmt)

    def test_bad_proxy_scheme(self) -> None:
        with pytest.raises(SystemExit):
            config_from_args(["https://example.com", "-x", "ftp://proxy"])

    def test_bad_header(self) -> None:
        with pytest.raises(SystemExit):
            config_from_args(["https://example.com", "-H", "NoColonHere"])

    def test_header_with_colon_in_value(self) -> None:
        cfg = config_from_args(["https://example.com", "-H", "X-Time: 12:30:00"])
        assert cfg.headers_override == (("X-Time", "12:30:00"),)
