import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from vchat.document_pipeline import (
    extract_url_document,
    normalize_title_candidate,
)
from vchat.document_indexing import (
    document_content_effectively_unchanged,
    sync_document_has_chunks,
)
from vchat.document_types import guess_document_type
from vchat.models.data import Chunk, CrawlRun, Page
from vchat.page_status import PageStatus, PageStatusError
from vchat.settings import config
from jobs.embedder.tasks import schedule_index_document

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
)


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


class DatabasePipeline:
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.engine = create_engine(
            sync_uri(config["database_uri"]),
        )
        self._crawl_run_id: int | None = None

    def open_spider(self, spider):
        source_id = getattr(spider, "source_id", None)
        if source_id:
            try:
                with Session(bind=self.engine) as session:
                    run = CrawlRun(
                        source_id=source_id, started_at=datetime.now(timezone.utc)
                    )
                    session.add(run)
                    session.commit()
                    self._crawl_run_id = run.id
            except Exception as exc:
                self.logger.warning("Failed to create CrawlRun: %s", exc)

    def process_item(self, item, spider):
        url = item["url"]
        source_id = item["source_id"]
        final_url = item.get("final_url", url)
        http_status = item.get("http_status", 200)
        etag = item.get("etag")

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
                reason="excluded_auth_redirect",
                message="Request redirected to an auth/login page.",
            )
            increment_run_stat(self.engine, self._crawl_run_id, "pages_excluded")
            return item

        # Handle 4xx errors
        if http_status and 400 <= http_status < 500:
            handle_error_page(
                self.engine, url, source_id, http_status, etag, self.logger
            )
            increment_run_stat(self.engine, self._crawl_run_id, "pages_errors")
            return item

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
                reason="http_5xx",
                message=f"Source returned HTTP {http_status}.",
                error=f"HTTP {http_status}",
            )
            increment_run_stat(self.engine, self._crawl_run_id, "pages_errors")
            return item

        html_body = item.get("content")
        content_type = item.get("content_type")
        markdown_content = None
        try:
            markdown_content, normalized_title, extracted_meta = extract_url_document(
                url,
                html_body=html_body,
                content_type=content_type,
            )
        except Exception as exc:
            spider.logger.error("Extraction failed for %s: %s", url, exc, exc_info=True)
            save_page_status(
                self.engine,
                url,
                source_id,
                PageStatus.crawler,
                PageStatusError.extraction_failed,
                http_status,
                etag,
                self.logger,
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
                reason="empty_extracted_content",
                message="No useful text remained after extraction.",
            )
            increment_run_stat(self.engine, self._crawl_run_id, "pages_excluded")
            return item

        low_content = is_low_content_page(markdown_content, extracted_meta)

        try:
            with Session(bind=self.engine) as session:
                stmt = select(Page).where(Page.source_id == source_id, Page.uri == url)
                page = session.execute(stmt).scalar_one_or_none()
                is_new = page is None

                if page is None:
                    page = Page(source_id=source_id, uri=url)
                    session.add(page)

                if page.status_error == PageStatusError.excluded_ignored:
                    page.status = PageStatus.crawler
                    page.content = ""
                    page.title = ""
                    page.length = 0
                    page.last_crawled_at = datetime.now(timezone.utc)
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
                meta.update(extracted_meta)
                if item_meta:
                    meta.update(item_meta)
                content_type = item.get("content_type")
                doc_type = guess_document_type(url, content_type)
                if doc_type:
                    meta["doc_type"] = doc_type
                if content_type:
                    meta["content_type"] = content_type

                page.content = markdown_content
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
                    page.status = PageStatus.crawler
                    page.status_error = PageStatusError.low_content
                    if page.id is not None:
                        session.execute(delete(Chunk).where(Chunk.page_id == page.id))
                else:
                    clear_error_meta(meta)
                page.meta = meta

                if normalized_title:
                    page.title = normalized_title
                elif item.get("title"):
                    fallback_title = normalize_title_candidate(item.get("title"))
                    if fallback_title:
                        page.title = fallback_title

                session.commit()

                if low_content:
                    increment_run_stat(
                        self.engine, self._crawl_run_id, "pages_excluded"
                    )
                    spider.logger.info(
                        "Excluded %s from indexing due to low content (%s chars)",
                        url,
                        len(markdown_content.strip()),
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
                    try:
                        schedule_index_document(page.id)
                    except Exception as embed_exc:
                        spider.logger.error(
                            "Failed to schedule chunking for %s: %s",
                            url,
                            embed_exc,
                            exc_info=True,
                        )
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
                reason="pipeline_processing_failed",
                message="Crawler pipeline failed while saving or scheduling the document.",
                error=str(e),
                exception_class=type(e).__name__,
            )

        return item

    def close_spider(self, spider):
        if self._crawl_run_id:
            try:
                with Session(bind=self.engine) as session:
                    run = session.get(CrawlRun, self._crawl_run_id)
                    if run and run.finished_at is None:
                        run.finished_at = datetime.now(timezone.utc)
                        run.exit_reason = "finished"
                        session.commit()
            except Exception:
                pass
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
) -> None:
    try:
        with Session(bind=engine) as session:
            stmt = select(Page).where(Page.source_id == source_id, Page.uri == url)
            page = session.execute(stmt).scalar_one_or_none()
            if page is None:
                page = Page(source_id=source_id, uri=url)
                page._hash = ""
                session.add(page)

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

            meta = dict(page.meta or {})
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

            session.commit()
    except Exception as exc:
        logger.error("Failed to save page status for %s: %s", url, exc, exc_info=True)


def handle_error_page(
    engine, url: str, source_id: int, http_status: int, etag: str | None, logger
) -> None:
    try:
        with Session(bind=engine) as session:
            stmt = select(Page).where(Page.source_id == source_id, Page.uri == url)
            page = session.execute(stmt).scalar_one_or_none()
            if page is None:
                page = Page(source_id=source_id, uri=url)
                page._hash = ""
                session.add(page)

            page.http_status = http_status
            page.last_crawled_at = datetime.now(timezone.utc)
            page.error_count = (page.error_count or 0) + 1
            if etag:
                page.last_etag = etag

            page.status = PageStatus.crawler
            page.status_error = PageStatusError.http_4xx
            if page.error_count >= 2:
                page.check_interval_days = 90

            meta = dict(page.meta or {})
            set_error_meta(
                meta,
                reason="http_4xx",
                message=f"Source returned HTTP {http_status}.",
                error=f"HTTP {http_status}",
            )
            page.meta = meta

            session.commit()
    except Exception as exc:
        logger.error("Failed to handle error page %s: %s", url, exc, exc_info=True)


def increment_run_stat(engine, crawl_run_id: int | None, field: str) -> None:
    if not crawl_run_id:
        return
    try:
        with Session(bind=engine) as session:
            run = session.get(CrawlRun, crawl_run_id)
            if run:
                current = getattr(run, field, 0) or 0
                setattr(run, field, current + 1)
                session.commit()
    except Exception:
        pass
