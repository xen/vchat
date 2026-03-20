import logging

from docling.document_converter import DocumentConverter
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from vchat.models.data import Document
from vchat.document_types import guess_document_type, has_html_form
from vchat.settings import config
import html


class DatabasePipeline:
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.converter = DocumentConverter()
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
            result = self.converter.convert(url)
            markdown_content = result.document.export_to_markdown()
        except Exception as exc:
            spider.logger.error(f"Docling failed for {url}: {exc}", exc_info=True)

        if not markdown_content:
            markdown_content = item.get("content")

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
                document.language = markdown_content
                document.length = markdown_content

                item_meta = item.get("meta", {})
                meta = dict(document.meta or {})
                if item_meta:
                    meta.update(item_meta)
                content_type = item.get("content_type")
                doc_type = guess_document_type(url, content_type)
                if doc_type:
                    meta["doc_type"] = doc_type
                if content_type:
                    meta["content_type"] = content_type

                if has_html_form(item.get("content")):
                    meta["form"] = True
                elif "form" in meta:
                    meta.pop("form", None)

                document.meta = meta
                if item.get("title"):
                    clean_title = item["title"].strip()
                    document.title = html.escape(clean_title)

                session.commit()
                spider.logger.info("Indexed %s", url)
        except Exception as e:
            spider.logger.error(f"Error processing {url}: {e}", exc_info=True)

        return item

    def close_spider(self, spider):
        self.engine.dispose()
