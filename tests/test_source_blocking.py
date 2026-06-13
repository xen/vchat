from __future__ import annotations

import ipaddress
from types import SimpleNamespace

import pytest
import requests

from jobs.crawler.source_blocking import (
    SourceBlockedReason,
    apply_source_blocking_result,
    check_source_blocking,
)


class _Resp:
    def __init__(
        self,
        *,
        status_code=200,
        text="",
        url="https://example.com/",
        history=None,
        headers=None,
    ):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.history = history or []
        self.headers = headers or {}


def _public_addresses(_hostname: str):
    return [ipaddress.ip_address("93.184.216.34")]


def test_check_source_blocking_marks_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jobs.crawler.source_blocking._resolve_hostname",
        lambda hostname: [],
    )

    result = check_source_blocking("https://missing.example")
    assert result.reason == SourceBlockedReason.dns_unresolved
    assert result.is_blocked is True


def test_check_source_blocking_marks_robots_disallow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jobs.crawler.source_blocking._resolve_hostname",
        _public_addresses,
    )

    def fake_get(url, **kwargs):
        if url.endswith("/robots.txt"):
            return _Resp(text="User-agent: *\nDisallow: /\n", url=url)
        return _Resp(url=url)

    monkeypatch.setattr(requests, "get", fake_get)

    result = check_source_blocking("https://example.com")
    assert result.reason == SourceBlockedReason.robots_txt


def test_check_source_blocking_can_skip_robots_txt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jobs.crawler.source_blocking._resolve_hostname",
        _public_addresses,
    )

    requested_urls = []

    def fake_get(url, **kwargs):
        requested_urls.append(url)
        return _Resp(url=url)

    monkeypatch.setattr(requests, "get", fake_get)

    result = check_source_blocking("https://example.com", ignore_robots_txt=True)
    assert result.reason is None
    assert requested_urls == ["https://example.com"]


def test_check_source_blocking_marks_external_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jobs.crawler.source_blocking._resolve_hostname",
        _public_addresses,
    )
    monkeypatch.setattr(
        "jobs.crawler.source_blocking._check_robots_txt",
        lambda *args, **kwargs: None,
    )

    monkeypatch.setattr(
        requests,
        "get",
        lambda url, **kwargs: _Resp(
            status_code=302,
            url="https://example.com/",
            headers={"Location": "https://otherdomain.example/"},
        ),
    )

    result = check_source_blocking("https://example.com")
    assert result.reason == SourceBlockedReason.redirect_other_domain


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.10",
        "172.16.0.10",
        "192.168.0.10",
        "169.254.169.254",
        "255.255.255.255",
        "8.8.8.0",
        "8.8.8.255",
        "::1",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::",
    ],
)
def test_check_source_blocking_rejects_private_or_special_addresses(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    monkeypatch.setattr(
        "jobs.crawler.source_blocking._resolve_hostname",
        lambda hostname: [ipaddress.ip_address(address)],
    )

    result = check_source_blocking("https://example.com")
    assert result.reason == SourceBlockedReason.private_address


def test_check_source_blocking_rejects_same_domain_redirect_to_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_resolve(hostname):
        if hostname == "private.example.com":
            return [ipaddress.ip_address("10.0.0.10")]
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr("jobs.crawler.source_blocking._resolve_hostname", fake_resolve)
    monkeypatch.setattr(
        "jobs.crawler.source_blocking._check_robots_txt",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, **kwargs: _Resp(
            status_code=302,
            url="https://example.com/",
            headers={"Location": "https://private.example.com/"},
        ),
    )

    result = check_source_blocking("https://example.com")
    assert result.reason == SourceBlockedReason.private_address


def test_apply_source_blocking_result_sets_fields() -> None:
    source = SimpleNamespace(
        blocked_reason=None,
        blocked_message=None,
        blocked_checked_at=None,
    )
    result = SimpleNamespace(
        reason=SourceBlockedReason.http_5xx,
        message="Главная страница вернула HTTP 500.",
        checked_at="stamp",
        is_blocked=True,
    )

    apply_source_blocking_result(source, result)
    assert source.blocked_reason == "http_5xx"
    assert source.blocked_message == "Главная страница вернула HTTP 500."
    assert source.blocked_checked_at == "stamp"
