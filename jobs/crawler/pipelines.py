import logging
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from vchat.document_pipeline import (
    SPAPageError,
    extract_url_document,
    normalize_title_candidate,
)
from vchat.document_indexing import (
    document_content_effectively_unchanged,
    sync_document_has_chunks,
)
from vchat.document_types import guess_document_type
from vchat.models.data import Document
from vchat.settings import config
from jobs.embedder.tasks import schedule_index_document


class DatabasePipeline:
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.engine = create_engine(
            self._sync_uri(config["database_uri"]),
            echo=True,
        )

    def _sync_uri(self, uri: str) -> str:
        if "+asyncpg" in uri:
            return uri.replace("+asyncpg", "+psycopg", 1)
        return uri

    def process_item(self, item, spider):
        url = item["url"]
        source_id = item["source_id"]
        markdown_content = None

        spider.logger.info(f"Pipeline received {url}")

        try:
            markdown_content, normalized_title, extracted_meta = extract_url_document(
                url
            )
        except SPAPageError as exc:
            spider.logger.warning(
                "SPA detected, skipping indexing for %s: %s", url, exc
            )
            self._mark_spa(url, source_id)
            return item
        except Exception as exc:
            spider.logger.error("Extraction failed for %s: %s", url, exc, exc_info=True)
            return item

        if not markdown_content:
            spider.logger.warning(f"Skipping {url}: no content extracted")
            return item

        try:
            with Session(bind=self.engine) as session:
                stmt = select(Document).where(
                    Document.source_id == source_id, Document.uri == url
                )
                document = session.execute(stmt).scalar_one_or_none()

                if document is None:
                    document = Document(
                        source_id=source_id,
                        uri=url,
                        status="indexed",
                    )
                    session.add(document)

                if document.is_ignored:
                    spider.logger.info(f"Skipping ignored document {url}")
                    document.status = "ignored"
                    document.content = ""
                    document.title = ""
                    document.length = 0
                    document.language = ""
                    session.commit()
                    return item

                effectively_unchanged = document_content_effectively_unchanged(
                    document, markdown_content
                )
                has_chunks = (
                    sync_document_has_chunks(session, document.id)
                    if (effectively_unchanged and document.id is not None)
                    else False
                )
                document.content = markdown_content
                document.status = "indexed"
                document.hash_value = markdown_content
                document.language = ""
                document.length = len(markdown_content)

                item_meta = item.get("meta", {})
                meta = dict(document.meta or {})
                meta.update(extracted_meta)
                if item_meta:
                    meta.update(item_meta)
                content_type = item.get("content_type")
                doc_type = guess_document_type(url, content_type)
                if doc_type:
                    meta["doc_type"] = doc_type
                if content_type:
                    meta["content_type"] = content_type

                document.meta = meta
                if normalized_title:
                    document.title = normalized_title
                elif item.get("title"):
                    fallback_title = normalize_title_candidate(item.get("title"))
                    if fallback_title:
                        document.title = fallback_title

                session.commit()
                spider.logger.info("Indexed %s", url)
                if effectively_unchanged and has_chunks:
                    spider.logger.info(
                        "Skipping chunk refresh for %s: content unchanged",
                        url,
                    )
                else:
                    try:
                        schedule_index_document(document.id)
                    except Exception as embed_exc:
                        spider.logger.error(
                            "Failed to schedule chunking for %s: %s",
                            url,
                            embed_exc,
                            exc_info=True,
                        )
        except Exception as e:
            spider.logger.error(f"Error processing {url}: {e}", exc_info=True)

        return item

    def _mark_spa(self, url: str, source_id: int) -> None:
        try:
            with Session(bind=self.engine) as session:
                stmt = select(Document).where(
                    Document.source_id == source_id, Document.uri == url
                )
                document = session.execute(stmt).scalar_one_or_none()
                if document is None:
                    document = Document(
                        source_id=source_id,
                        uri=url,
                        status="indexed",
                    )
                    session.add(document)
                document.status = "indexed"
                document.content = ""
                document.length = 0
                document.language = ""
                meta = dict(document.meta or {})
                meta["spa_detected"] = True
                document.meta = meta
                session.commit()
        except Exception as exc:
            self.logger.error("Failed to mark SPA for %s: %s", url, exc, exc_info=True)

    def close_spider(self, spider):
        self.engine.dispose()
