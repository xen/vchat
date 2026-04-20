import logging
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from vchat.document_pipeline import extract_url_document
from vchat.document_types import guess_document_type
from vchat.models.data import Document
from vchat.settings import config


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
            markdown_content, normalized_title, extracted_meta = extract_url_document(url)
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
                    document.title = item["title"].strip()[:512]

                session.commit()
                spider.logger.info("Indexed %s", url)
        except Exception as e:
            spider.logger.error(f"Error processing {url}: {e}", exc_info=True)

        return item

    def close_spider(self, spider):
        self.engine.dispose()
