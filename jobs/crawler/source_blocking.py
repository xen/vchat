from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from vchat.settings import config


class SourceBlockedReason(str, Enum):
    robots_txt = "robots_txt"
    site_unreachable = "site_unreachable"
    http_5xx = "http_5xx"
    dns_unresolved = "dns_unresolved"
    redirect_other_domain = "redirect_other_domain"


SOURCE_BLOCKED_REASON_LABELS: dict[SourceBlockedReason, str] = {
    SourceBlockedReason.robots_txt: "robots.txt запрещает обход",
    SourceBlockedReason.site_unreachable: "Сайт не открывается",
    SourceBlockedReason.http_5xx: "Главная страница возвращает 5xx",
    SourceBlockedReason.dns_unresolved: "Домен не резолвится",
    SourceBlockedReason.redirect_other_domain: "Главная страница редиректит на другой домен",
}

_CRAWLER_USER_AGENT = config.get("crawler_user_agent", "Dzen-AI/1.0")


@dataclass(slots=True)
class SourceBlockCheckResult:
    reason: SourceBlockedReason | None
    message: str | None
    checked_at: datetime

    @property
    def is_blocked(self) -> bool:
        return self.reason is not None


def describe_blocked_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    try:
        return SOURCE_BLOCKED_REASON_LABELS[SourceBlockedReason(reason)]
    except ValueError:
        return reason


def _normalize_host(host: str | None) -> str:
    return (host or "").strip().lower().rstrip(".")


def _hosts_share_domain(left: str | None, right: str | None) -> bool:
    left_norm = _normalize_host(left)
    right_norm = _normalize_host(right)
    if not left_norm or not right_norm:
        return False
    return (
        left_norm == right_norm
        or left_norm.endswith("." + right_norm)
        or right_norm.endswith("." + left_norm)
    )


def _blocked(
    reason: SourceBlockedReason,
    message: str,
    *,
    checked_at: datetime,
) -> SourceBlockCheckResult:
    return SourceBlockCheckResult(
        reason=reason,
        message=message,
        checked_at=checked_at,
    )


def _resolve_hostname(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    return True


def _check_robots_txt(
    source_uri: str,
    *,
    headers: dict[str, str],
    checked_at: datetime,
) -> SourceBlockCheckResult | None:
    robots_url = urljoin(source_uri.rstrip("/") + "/", "robots.txt")
    try:
        resp = requests.get(
            robots_url,
            headers=headers,
            timeout=15,
            allow_redirects=True,
        )
    except requests.RequestException:
        return None

    if resp.status_code != 200 or not resp.text.strip():
        return None

    parser = RobotFileParser()
    parser.parse(resp.text.splitlines())
    probe_url = urljoin(source_uri.rstrip("/") + "/", "/")
    if parser.can_fetch(_CRAWLER_USER_AGENT, probe_url):
        return None

    return _blocked(
        SourceBlockedReason.robots_txt,
        f"robots.txt запрещает обход для {_CRAWLER_USER_AGENT}.",
        checked_at=checked_at,
    )


def check_source_blocking(
    source_uri: str,
    *,
    ignore_robots_txt: bool = False,
) -> SourceBlockCheckResult:
    checked_at = datetime.now(timezone.utc)
    parsed_source = urlparse(source_uri)
    source_host = _normalize_host(parsed_source.hostname)

    if not source_host or not _resolve_hostname(source_host):
        return _blocked(
            SourceBlockedReason.dns_unresolved,
            "Не удалось разрешить DNS-имя источника.",
            checked_at=checked_at,
        )

    headers = {"User-Agent": _CRAWLER_USER_AGENT}

    if not ignore_robots_txt:
        robots_result = _check_robots_txt(
            source_uri,
            headers=headers,
            checked_at=checked_at,
        )
        if robots_result is not None:
            return robots_result

    try:
        resp = requests.get(
            source_uri,
            headers=headers,
            timeout=15,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return _blocked(
            SourceBlockedReason.site_unreachable,
            f"Не удалось открыть главную страницу: {exc}",
            checked_at=checked_at,
        )

    final_host = _normalize_host(urlparse(resp.url).hostname)
    if resp.history and not _hosts_share_domain(source_host, final_host):
        return _blocked(
            SourceBlockedReason.redirect_other_domain,
            f"Главная страница редиректит на {resp.url}.",
            checked_at=checked_at,
        )

    if 500 <= resp.status_code < 600:
        return _blocked(
            SourceBlockedReason.http_5xx,
            f"Главная страница вернула HTTP {resp.status_code}.",
            checked_at=checked_at,
        )

    return SourceBlockCheckResult(
        reason=None,
        message=None,
        checked_at=checked_at,
    )


def apply_source_blocking_result(
    source: object, result: SourceBlockCheckResult
) -> None:
    setattr(source, "blocked_reason", result.reason.value if result.reason else None)
    setattr(source, "blocked_message", result.message if result.is_blocked else None)
    setattr(source, "blocked_checked_at", result.checked_at)
