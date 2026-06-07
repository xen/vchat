import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import create_engine, delete, func, or_, select, true
from sqlalchemy.orm import Session

from jobs.crawler.document_pipeline import (
    extract_binary_url_document,
    extract_url_document,
    normalize_title_candidate,
)
from jobs.indexing.documents import (
    document_content_effectively_unchanged,
    raw_content_payload,
    sync_document_has_chunks,
)
from vchat.document_content import (
    content_sha256,
    document_too_big_message,
    is_document_too_big,
)
from vchat.document_types import guess_document_type
from vchat.models.data import Chunk, CrawlRun, Page, PageLink, PageShingle, Source
from vchat.page_status import PageStatus, PageStatusError
from vchat.settings import config
from vchat.triggers import source_trigger_rules_match_url
from jobs.crawler.tasks import schedule_index_document, update_page_shingles
from jobs.crawler.url_rules import (
    normalize_url_for_queue,
    build_source_id_by_host,
    resolve_source_id_for_url,
)

AUTH_URL_SEGMENTS = ("/login", "/auth", "/signin", "/account/login", "/user/login")
AUTH_QUERY_PARAMS = ("next", "return", "redirect", "next_url")
HUB_INTERNAL_LINK_THRESHOLD = 40
LOW_CONTENT_MAX_WORDS = 40
LOW_CONTENT_MAX_CHARS = 250
ERROR_META_KEYS = (
    "error",
    "message",
    "reason",
    "exception_class",
    "duplicate_of_page_id",
    "duplicate_of_uri",
)
AUTO_INDEX_POLICY_META_KEYS = (
    "index_policy",
    "index_policy_reason",
)


@dataclass(frozen=True)
class DuplicatePage:
    id: int
    uri: str


def is_auth_redirect(original_url: str, final_url: str) -> bool:
    if original_url == final_url:
        return False
    parsed = urlparse(final_url)
    path = parsed.path.lower()
    if any(seg in path for seg in AUTH_URL_SEGMENTS):
        return True
    query = parsed.query.lower()
    if any(f"{p}=" in query for p in AUTH_QUERY_PARAMS):
        return True
    orig_host = urlparse(original_url).netloc
    final_host = parsed.netloc
    if orig_host and final_host and orig_host != final_host:
        return True
    return False


def count_internal_links(content: str, source_domain: str) -> int:
    """Count markdown links pointing to the same domain."""
    pattern = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
    count = 0
    for _, href in pattern.findall(content or ""):
        href_lower = href.lower()
        if source_domain in href_lower or href.startswith("/"):
            count += 1
    return count


def compute_adaptive_interval(page: Page, content_changed: bool) -> int:
    current = page.check_interval_days or 7
    if content_changed:
        return max(1, current // 2)
    else:
        return min(90, int(current * 1.5))


def get_page_by_uri(session: Session, uri: str) -> Page | None:
    return session.execute(select(Page).where(Page.uri == uri)).scalars().first()


def source_has_trigger_context(source: Source | None) -> bool:
    return source is not None and isinstance(getattr(source, "uri", None), str)


def get_or_create_page(
    session: Session,
    *,
    source_id: int,
    uri: str,
    discover_by: str | None = None,
    discover_source: str | None = None,
) -> tuple[Page, bool]:
    page = get_page_by_uri(session, uri)
    created = page is None
    source = session.get(Source, source_id)
    if page is None:
        page = Page(
            source_id=source_id,
            uri=uri,
            discover_by=discover_by,
            discover_source=discover_source,
        )
        page._hash = ""
        session.add(page)
    if source_has_trigger_context(source):
        page.has_triggers = source_trigger_rules_match_url(source, uri)
    return page, created


def sync_page_links(
    session: Session,
    *,
    source_page: Page,
    source_id: int,
    out_links: list[str] | None,
    source_rules: list[dict] | None = None,
) -> None:
    if source_page.id is None or not source_page.uri:
        return

    source_rows = session.execute(select(Source.id, Source.uri)).all()
    source_id_by_host = build_source_id_by_host(source_rows)

    unique_links: list[str] = []
    seen: set[str] = set()
    for raw_url in out_links or []:
        url = normalize_url_for_queue(raw_url or "", source_rules)
        if not url or url == source_page.uri or url in seen:
            continue
        seen.add(url)
        unique_links.append(url)

    session.execute(
        delete(PageLink).where(
            PageLink.source_page_id == source_page.id,
        )
    )

    for target_uri in unique_links:
        target_source_id = resolve_source_id_for_url(target_uri, source_id_by_host)
        target_page = None
        if target_source_id is not None:
            target_page = get_page_by_uri(session, target_uri)
            if target_page is None:
                target_page = Page(
                    source_id=target_source_id,
                    uri=target_uri,
                    discover_by="page",
                    discover_source=source_page.uri,
                )
                target_page._hash = ""
                session.add(target_page)
                session.flush()
            target_source = session.get(Source, target_source_id)
            if source_has_trigger_context(target_source):
                target_page.has_triggers = source_trigger_rules_match_url(
                    target_source,
                    target_uri,
                )

        session.add(
            PageLink(
                source_uri=source_page.uri,
                target_uri=target_uri,
                source_page_id=source_page.id,
                target_page_id=target_page.id if target_page is not None else None,
                source_id=source_id,
            )
        )


def is_low_content_page(content: str, extracted_meta: dict[str, object]) -> bool:
    stripped = (content or "").strip()
    if not stripped:
        return False

    extraction = extracted_meta.get("extraction")
    if not isinstance(extraction, dict):
        return False

    word_count = extraction.get("word_count")
    try:
        words = int(word_count or 0)
    except (TypeError, ValueError):
        words = 0

    return words <= LOW_CONTENT_MAX_WORDS and len(stripped) <= LOW_CONTENT_MAX_CHARS


def clear_error_meta(meta: dict) -> dict:
    for key in ERROR_META_KEYS:
        meta.pop(key, None)
    return meta


def clear_auto_index_policy_meta(meta: dict) -> dict:
    for key in AUTO_INDEX_POLICY_META_KEYS:
        meta.pop(key, None)
    return meta


def set_error_meta(
    meta: dict,
    *,
    reason: str,
    message: str | None = None,
    error: str | None = None,
    exception_class: str | None = None,
) -> dict:
    clear_error_meta(meta)
    meta["reason"] = reason
    if message:
        meta["message"] = message
    if error:
        meta["error"] = error
    if exception_class:
        meta["exception_class"] = exception_class
    return meta


def find_duplicate_content_page(
    session: Session,
    *,
    page_id: int | None,
    source_id: int,
    uri: str,
    content_hash: str,
) -> DuplicatePage | None:
    row = session.execute(
        select(Page.id, Page.uri)
        .where(
            Page.source_id == source_id,
            Page._hash == content_hash,
            Page.uri.isnot(None),
            Page.status_error.is_(None),
        )
        .where(Page.id != page_id if page_id is not None else true())
        .order_by(func.length(Page.uri).asc(), Page.id.asc())
        .limit(1)
    ).first()
    if row is None:
        return None

    duplicate_id = getattr(row, "id", None)
    duplicate_uri = getattr(row, "uri", None)
    if not isinstance(duplicate_id, int) or not duplicate_uri:
        return None
    current_key = (len(uri or ""), page_id or 0)
    duplicate_key = (len(duplicate_uri), duplicate_id)
    if current_key <= duplicate_key:
        return None
    return DuplicatePage(id=duplicate_id, uri=duplicate_uri)


def mark_existing_duplicate_content_pages(
    session: Session,
    *,
    canonical_page: Page,
    content_hash: str,
) -> int:
    if (
        canonical_page.id is None
        or canonical_page.source_id is None
        or not canonical_page.uri
    ):
        return 0

    canonical_key = (len(canonical_page.uri), canonical_page.id)
    rows = session.execute(
        select(Page).where(
            Page.source_id == canonical_page.source_id,
            Page._hash == content_hash,
            Page.id != canonical_page.id,
            Page.uri.isnot(None),
            or_(
                Page.status_error.is_(None),
                Page.status_error == PageStatusError.duplicate_content,
            ),
        )
    ).scalars()

    duplicate_ids: list[int] = []
    for page in rows:
        if page.id is None or not page.uri:
            continue
        if (len(page.uri), page.id) <= canonical_key:
            continue
        meta = dict(page.meta or {})
        set_error_meta(
            meta,
            reason=PageStatusError.duplicate_content.value,
            message=(
                "Extracted content fully matches another page from the same source."
            ),
        )
        meta["duplicate_of_page_id"] = canonical_page.id
        meta["duplicate_of_uri"] = canonical_page.uri
        page.status = PageStatus.ready
        page.status_error = PageStatusError.duplicate_content
        page.meta = meta
        duplicate_ids.append(page.id)

    if duplicate_ids:
        session.execute(delete(Chunk).where(Chunk.page_id.in_(duplicate_ids)))
        session.execute(
            delete(PageShingle).where(PageShingle.page_id.in_(duplicate_ids))
        )
    return len(duplicate_ids)


class DatabasePipeline:
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.engine = create_engine(
            sync_uri(config["database_uri"]),
        )
        self._crawl_run_id: int | None = None

    def open_spider(self, spider):
        source_id = getattr(spider, "source_id", None)
        configured_run_id = getattr(spider, "crawl_run_id", None)
        if isinstance(configured_run_id, int):
            normalized_run_id = configured_run_id
        elif isinstance(configured_run_id, str) and configured_run_id.strip().isdigit():
            normalized_run_id = int(configured_run_id)
        else:
            normalized_run_id = None
        if normalized_run_id:
            self._crawl_run_id = normalized_run_id
            return
        if source_id:
            with Session(bind=self.engine) as session:
                run = CrawlRun(
                    source_id=source_id, started_at=datetime.now(timezone.utc)
                )
                session.add(run)
                session.commit()
                self._crawl_run_id = run.id

    def process_item(self, item, spider):
        url = item["url"]
        source_id = item["source_id"]
        final_url = item.get("final_url", url)
        http_status = item.get("http_status", 200)
        etag = item.get("etag")
        source_rules = list(getattr(spider, "source_rules", []) or [])
        raw_content, raw_content_meta = raw_content_payload(item.get("raw_content"))

        spider.logger.info(f"Pipeline received {url}")

        # Auth redirect detection
        if is_auth_redirect(url, final_url):
            save_page_status(
                self.engine,
                url,
                source_id,
                PageStatus.crawler,
                PageStatusError.excluded_auth,
                http_status,
                etag,
                self.logger,
                raw_content=raw_content,
                raw_content_type=item.get("content_type"),
                raw_content_meta=raw_content_meta,
                reason="excluded_auth_redirect",
                message="Request redirected to an auth/login page.",
            )
            increment_run_stat(self.engine, self._crawl_run_id, "pages_excluded")
            return item

        html_body = item.get("content")
        content_type = item.get("content_type")
        markdown_content = None
        normalized_title = None
        extracted_meta = None

        # Handle 4xx errors
        if http_status and 400 <= http_status < 500:
            if html_body:
                try:
                    markdown_content, normalized_title, extracted_meta = (
                        extract_url_document(
                            url,
                            html_body=html_body,
                            content_type=content_type,
                        )
                    )
                except Exception as exc:
                    spider.logger.error(
                        "Soft-4xx extraction failed for %s: %s",
                        url,
                        exc,
                        exc_info=True,
                    )
            if not markdown_content:
                handle_error_page(
                    self.engine,
                    url,
                    source_id,
                    http_status,
                    etag,
                    self.logger,
                    raw_content=raw_content,
                    raw_content_type=content_type,
                    raw_content_meta=raw_content_meta,
                )
                increment_run_stat(self.engine, self._crawl_run_id, "pages_errors")
                return item
            spider.logger.warning(
                "Treating HTTP %s for %s as soft-4xx because extractable content exists",
                http_status,
                url,
            )

        # Handle 5xx errors
        if http_status and http_status >= 500:
            save_page_status(
                self.engine,
                url,
                source_id,
                PageStatus.crawler,
                PageStatusError.http_5xx,
                http_status,
                etag,
                self.logger,
                raw_content=raw_content,
                raw_content_type=content_type,
                raw_content_meta=raw_content_meta,
                reason="http_5xx",
                message=f"Source returned HTTP {http_status}.",
                error=f"HTTP {http_status}",
            )
            increment_run_stat(self.engine, self._crawl_run_id, "pages_errors")
            return item

        if markdown_content is None:
            try:
                doc_type_hint = guess_document_type(url, content_type)
                if doc_type_hint == "office":
                    markdown_content, normalized_title, extracted_meta = (
                        extract_binary_url_document(
                            url,
                            raw_content or b"",
                            content_type=content_type,
                        )
                    )
                else:
                    markdown_content, normalized_title, extracted_meta = (
                        extract_url_document(
                            url,
                            html_body=html_body,
                            content_type=content_type,
                        )
                    )
            except Exception as exc:
                spider.logger.error(
                    "Extraction failed for %s: %s", url, exc, exc_info=True
                )
                save_page_status(
                    self.engine,
                    url,
                    source_id,
                    PageStatus.crawler,
                    PageStatusError.extraction_failed,
                    http_status,
                    etag,
                    self.logger,
                    raw_content=raw_content,
                    raw_content_type=content_type,
                    raw_content_meta=raw_content_meta,
                    reason="extraction_failed",
                    message="Document extraction failed after the page was downloaded.",
                    error=str(exc),
                    exception_class=type(exc).__name__,
                )
                increment_run_stat(self.engine, self._crawl_run_id, "pages_errors")
                return item

        if not markdown_content:
            save_page_status(
                self.engine,
                url,
                source_id,
                PageStatus.crawler,
                PageStatusError.no_content,
                http_status,
                etag,
                self.logger,
                raw_content=raw_content,
                raw_content_type=content_type,
                raw_content_meta=raw_content_meta,
                reason="empty_extracted_content",
                message="No useful text remained after extraction.",
            )
            increment_run_stat(self.engine, self._crawl_run_id, "pages_excluded")
            return item

        low_content = is_low_content_page(markdown_content, extracted_meta)
        too_big = is_document_too_big(markdown_content)
        content_hash = content_sha256(markdown_content)

        try:
            with Session(bind=self.engine) as session:
                page, is_new = get_or_create_page(
                    session,
                    source_id=source_id,
                    uri=url,
                    discover_by="page" if item.get("referer_url") else None,
                    discover_source=item.get("referer_url") or None,
                )
                session.flush()

                if page.status_error == PageStatusError.excluded_ignored:
                    page.status = PageStatus.ready
                    page.content = ""
                    page.title = ""
                    page.length = 0
                    page.last_crawled_at = datetime.now(timezone.utc)
                    session.flush()
                    update_page_shingles(
                        session,
                        page_id=page.id,
                        source_id=page.source_id,
                        content=page.content,
                    )
                    session.commit()
                    increment_run_stat(
                        self.engine, self._crawl_run_id, "pages_excluded"
                    )
                    return item

                force_reprocess = bool((page.meta or {}).get("force_reprocess_once"))
                effectively_unchanged = document_content_effectively_unchanged(
                    page, markdown_content
                )
                has_chunks = (
                    sync_document_has_chunks(session, page.id)
                    if (
                        effectively_unchanged
                        and not force_reprocess
                        and page.id is not None
                    )
                    else False
                )

                content_changed = force_reprocess or not effectively_unchanged

                # Hub page detection
                source_domain = urlparse(url).netloc
                internal_links = count_internal_links(markdown_content, source_domain)
                if internal_links >= HUB_INTERNAL_LINK_THRESHOLD:
                    page.is_hub_page = True
                    page.content_value = 0.05
                elif not page.is_hub_page:
                    if page.content_value is None:
                        page.content_value = 0.8

                now = datetime.now(timezone.utc)
                item_meta = item.get("meta", {})
                meta = dict(page.meta or {})
                meta.pop("force_reprocess_once", None)
                clear_error_meta(meta)
                clear_auto_index_policy_meta(meta)
                meta.update(extracted_meta)
                if item_meta:
                    meta.update(item_meta)
                content_type = item.get("content_type")
                meta["raw_content"] = raw_content_meta
                doc_type = guess_document_type(url, content_type)
                if doc_type:
                    meta["doc_type"] = doc_type
                if content_type:
                    meta["content_type"] = content_type

                duplicate_page = None
                if not low_content and not too_big:
                    duplicate_page = find_duplicate_content_page(
                        session,
                        page_id=page.id,
                        source_id=source_id,
                        uri=url,
                        content_hash=content_hash,
                    )

                page.content = markdown_content
                page.raw_content = raw_content
                page.raw_content_size = raw_content_meta["size"]
                page.raw_content_type = content_type
                page.status = PageStatus.parsing
                page.status_error = None
                page.hash_value = markdown_content
                page.language = ""
                page.length = len(markdown_content)
                page.http_status = http_status
                page.last_crawled_at = now
                if etag:
                    page.last_etag = etag
                if content_changed:
                    page.last_modified_at = now
                    page.stable_count = 0
                    page.error_count = 0
                else:
                    page.stable_count = (page.stable_count or 0) + 1
                    page.error_count = 0
                page.check_interval_days = compute_adaptive_interval(
                    page, content_changed
                )

                if low_content:
                    extraction = meta.get("extraction")
                    word_count = None
                    if isinstance(extraction, dict):
                        word_count = extraction.get("word_count")
                    set_error_meta(
                        meta,
                        reason="low_content",
                        message=(
                            "Extracted content is too small to index safely "
                            f"({word_count or 0} words, {len(markdown_content.strip())} chars)."
                        ),
                    )
                    page.status = PageStatus.ready
                    page.status_error = PageStatusError.low_content
                    if page.id is not None:
                        session.execute(delete(Chunk).where(Chunk.page_id == page.id))
                elif too_big:
                    set_error_meta(
                        meta,
                        reason=PageStatusError.too_big.value,
                        message=document_too_big_message(markdown_content),
                    )
                    page.status = PageStatus.ready
                    page.status_error = PageStatusError.too_big
                    if page.id is not None:
                        session.execute(delete(Chunk).where(Chunk.page_id == page.id))
                elif duplicate_page is not None:
                    set_error_meta(
                        meta,
                        reason=PageStatusError.duplicate_content.value,
                        message=(
                            "Extracted content fully matches another page from "
                            "the same source."
                        ),
                    )
                    meta["duplicate_of_page_id"] = duplicate_page.id
                    meta["duplicate_of_uri"] = duplicate_page.uri
                    page.status = PageStatus.ready
                    page.status_error = PageStatusError.duplicate_content
                    if page.id is not None:
                        session.execute(delete(Chunk).where(Chunk.page_id == page.id))
                else:
                    clear_error_meta(meta)
                page.meta = meta

                if not low_content and not too_big and duplicate_page is None:
                    mark_existing_duplicate_content_pages(
                        session,
                        canonical_page=page,
                        content_hash=content_hash,
                    )

                if normalized_title:
                    page.title = normalized_title
                elif item.get("title"):
                    fallback_title = normalize_title_candidate(item.get("title"))
                    if fallback_title:
                        page.title = fallback_title

                session.flush()
                shingle_content = None if duplicate_page is not None else page.content
                update_page_shingles(
                    session,
                    page_id=page.id,
                    source_id=page.source_id,
                    content=shingle_content,
                )
                sync_page_links(
                    session,
                    source_page=page,
                    source_id=source_id,
                    out_links=item.get("out_links"),
                    source_rules=source_rules,
                )

                session.commit()

                if low_content or too_big or duplicate_page is not None:
                    increment_run_stat(
                        self.engine, self._crawl_run_id, "pages_excluded"
                    )
                    if low_content:
                        spider.logger.info(
                            "Excluded %s from indexing due to low content (%s chars)",
                            url,
                            len(markdown_content.strip()),
                        )
                    elif too_big:
                        spider.logger.info(
                            "Excluded %s from indexing due to oversized content (%s chars)",
                            url,
                            len(markdown_content),
                        )
                    elif duplicate_page is not None:
                        spider.logger.info(
                            "Excluded %s from indexing as duplicate of %s",
                            url,
                            duplicate_page.uri,
                        )
                    return item

                if is_new:
                    increment_run_stat(self.engine, self._crawl_run_id, "pages_new")
                elif content_changed:
                    increment_run_stat(self.engine, self._crawl_run_id, "pages_changed")
                else:
                    increment_run_stat(self.engine, self._crawl_run_id, "pages_crawled")

                spider.logger.info("Indexed %s (changed=%s)", url, content_changed)

                if effectively_unchanged and has_chunks and not force_reprocess:
                    spider.logger.info(
                        "Skipping chunk refresh for %s: content unchanged", url
                    )
                    page.status = PageStatus.ready
                    page.status_error = None
                    session.commit()
                else:
                    session.commit()
                    schedule_index_document(page.id)
        except Exception as e:
            spider.logger.error(f"Error processing {url}: {e}", exc_info=True)
            save_page_status(
                self.engine,
                url,
                source_id,
                PageStatus.crawler,
                PageStatusError.extraction_failed,
                http_status,
                etag,
                self.logger,
                raw_content=raw_content,
                raw_content_type=content_type,
                raw_content_meta=raw_content_meta,
                reason="pipeline_processing_failed",
                message="Crawler pipeline failed while saving or scheduling the document.",
                error=str(e),
                exception_class=type(e).__name__,
            )

        return item

    def close_spider(self, spider):
        if self._crawl_run_id:
            with Session(bind=self.engine) as session:
                run = session.get(CrawlRun, self._crawl_run_id)
                if run and run.finished_at is None:
                    run.finished_at = datetime.now(timezone.utc)
                    run.exit_reason = "finished"
                    session.commit()
        self.engine.dispose()


def sync_uri(uri: str) -> str:
    if "+asyncpg" in uri:
        return uri.replace("+asyncpg", "+psycopg", 1)
    return uri


def save_page_status(
    engine,
    url: str,
    source_id: int,
    status: PageStatus,
    status_error: PageStatusError | None,
    http_status: int | None,
    etag: str | None,
    logger,
    *,
    reason: str | None = None,
    message: str | None = None,
    error: str | None = None,
    exception_class: str | None = None,
    raw_content: bytes | None = None,
    raw_content_type: str | None = None,
    raw_content_meta: dict | None = None,
) -> None:
    with Session(bind=engine) as session:
        page, _ = get_or_create_page(session, source_id=source_id, uri=url)

        page.status = status
        page.status_error = status_error
        page.last_crawled_at = datetime.now(timezone.utc)
        if http_status:
            page.http_status = http_status

        if status_error in (PageStatusError.http_4xx, PageStatusError.http_5xx):
            page.error_count = (page.error_count or 0) + 1
            if (
                status_error == PageStatusError.http_4xx
                and (page.error_count or 0) >= 3
            ):
                page.check_interval_days = 90
        else:
            page.error_count = 0

        if etag:
            page.last_etag = etag
        if raw_content_meta is not None:
            page.raw_content = raw_content
            page.raw_content_size = raw_content_meta["size"]
            page.raw_content_type = raw_content_type

        if status == PageStatus.crawler and status_error in (
            PageStatusError.http_4xx,
            PageStatusError.redirect,
            PageStatusError.excluded_robots,
            PageStatusError.excluded_rules,
            PageStatusError.excluded_auth,
            PageStatusError.excluded_ignored,
            PageStatusError.duplicate_content,
            PageStatusError.extraction_failed,
            PageStatusError.no_content,
            PageStatusError.low_content,
            PageStatusError.too_big,
        ):
            page.status = PageStatus.ready

        meta = dict(page.meta or {})
        if raw_content_meta is not None:
            meta["raw_content"] = raw_content_meta
        if reason:
            set_error_meta(
                meta,
                reason=reason,
                message=message,
                error=error,
                exception_class=exception_class,
            )
        else:
            clear_error_meta(meta)
        page.meta = meta
        session.flush()
        update_page_shingles(
            session,
            page_id=page.id,
            source_id=page.source_id,
            content=None,
        )

        session.commit()


def handle_error_page(
    engine,
    url: str,
    source_id: int,
    http_status: int,
    etag: str | None,
    logger,
    *,
    raw_content: bytes | None = None,
    raw_content_type: str | None = None,
    raw_content_meta: dict | None = None,
) -> None:
    with Session(bind=engine) as session:
        page, _ = get_or_create_page(session, source_id=source_id, uri=url)

        page.http_status = http_status
        page.last_crawled_at = datetime.now(timezone.utc)
        page.error_count = (page.error_count or 0) + 1
        if etag:
            page.last_etag = etag
        if raw_content_meta is not None:
            page.raw_content = raw_content
            page.raw_content_size = raw_content_meta["size"]
            page.raw_content_type = raw_content_type

        page.status = PageStatus.ready
        page.status_error = PageStatusError.http_4xx
        if page.error_count >= 2:
            page.check_interval_days = 90

        meta = dict(page.meta or {})
        if raw_content_meta is not None:
            meta["raw_content"] = raw_content_meta
        set_error_meta(
            meta,
            reason="http_4xx",
            message=f"Source returned HTTP {http_status}.",
            error=f"HTTP {http_status}",
        )
        page.meta = meta
        session.flush()
        update_page_shingles(
            session,
            page_id=page.id,
            source_id=page.source_id,
            content=None,
        )

        session.commit()


def increment_run_stat(engine, crawl_run_id: int | None, field: str) -> None:
    if not crawl_run_id:
        return
    with Session(bind=engine) as session:
        run = session.get(CrawlRun, crawl_run_id)
        if run:
            current = getattr(run, field, 0) or 0
            setattr(run, field, current + 1)
            session.commit()
