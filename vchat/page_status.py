from enum import Enum


class PageStatus(str, Enum):
    crawler = "crawler"
    parsing = "parsing"
    ready = "ready"


class PageStatusError(str, Enum):
    # Crawler
    http_4xx = "http_4xx"
    http_5xx = "http_5xx"
    redirect = "redirect"
    excluded_robots = "excluded_robots"
    excluded_rules = "excluded_rules"
    excluded_auth = "excluded_auth"
    excluded_ignored = "excluded_ignored"
    extraction_failed = "extraction_failed"
    no_content = "no_content"
    low_content = "low_content"

    # Parser / Embedder
    index_failed = "index_failed"
    embedder_failed = "embedder_failed"


STATUS_ERROR_DESCRIPTIONS: dict[PageStatusError, str] = {
    PageStatusError.http_4xx: "Клиентская ошибка HTTP",
    PageStatusError.http_5xx: "Серверная ошибка HTTP",
    PageStatusError.redirect: "Страница редиректит на другой URL",
    PageStatusError.excluded_robots: "Заблокировано правилами robots.txt",
    PageStatusError.excluded_rules: "Заблокировано правилами источника",
    PageStatusError.excluded_auth: "Страница ведёт на авторизацию",
    PageStatusError.excluded_ignored: "Исключено вручную",
    PageStatusError.extraction_failed: "Ошибка извлечения содержимого",
    PageStatusError.no_content: "Страница не содержит полезного текста",
    PageStatusError.low_content: "Слишком мало содержимого для индексации",
    PageStatusError.index_failed: "Ошибка при индексировании",
    PageStatusError.embedder_failed: "Ошибка эмбеддера",
}
