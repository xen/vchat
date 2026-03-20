"""
S3 Document Crawler
Crawls documents from S3 bucket and creates Document records.
"""

import logging
from pathlib import Path
import tempfile
import os

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from docling.document_converter import DocumentConverter
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from vchat.document_types import guess_document_type, has_html_form
from vchat.models.data import Document, Source
from vchat.settings import config

logger = logging.getLogger(__name__)

# Supported document extensions
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".md",
    ".html",
    ".rtf",
    ".odt",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
}


class S3Crawler:
    """Crawler for S3 buckets that downloads and processes documents."""

    def __init__(self, source_id: int):
        self.source_id = source_id
        self.converter = DocumentConverter()
        self.engine = create_engine(
            self._sync_uri(config["database_uri"]),
            echo=False,
        )

    def _sync_uri(self, uri: str) -> str:
        """Convert async database URI to sync URI."""
        if "+asyncpg" in uri:
            return uri.replace("+asyncpg", "+psycopg", 1)
        return uri

    def crawl(self):
        """Main crawl method."""
        logger.info(f"Starting S3 crawl for source {self.source_id}")

        with Session(bind=self.engine) as session:
            # Get source configuration
            source = session.execute(
                select(Source).where(Source.id == self.source_id)
            ).scalar_one_or_none()

            if not source:
                logger.error(f"Source {self.source_id} not found")
                return

            if source.type != "s3":
                logger.error(f"Source {self.source_id} is not an S3 source")
                return

            config_data = source.config or {}
            aws_access_key_id = config_data.get("aws_access_key_id")
            aws_secret_access_key = config_data.get("aws_secret_access_key")
            bucket_name = config_data.get("bucket_name")
            endpoint_url = config_data.get("endpoint_url", "")
            region = config_data.get("region", "us-east-1")
            prefix = config_data.get("prefix", "")

            if not all([aws_access_key_id, aws_secret_access_key, bucket_name]):
                logger.error(f"Missing S3 credentials for source {self.source_id}")
                return

            # Initialize S3 client
            try:
                client_kwargs = {
                    "aws_access_key_id": aws_access_key_id,
                    "aws_secret_access_key": aws_secret_access_key,
                    "region_name": region,
                }

                # Add custom endpoint URL if provided (for S3-compatible storage)
                if endpoint_url:
                    client_kwargs["endpoint_url"] = endpoint_url
                    logger.info(f"Using custom S3 endpoint: {endpoint_url}")

                s3_client = boto3.client("s3", **client_kwargs)
            except (ClientError, NoCredentialsError) as e:
                logger.error(f"Failed to connect to S3: {e}")
                return

            # List objects in bucket
            try:
                paginator = s3_client.get_paginator("list_objects_v2")
                pages = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

                processed_count = 0
                for page in pages:
                    if "Contents" not in page:
                        continue

                    for obj in page["Contents"]:
                        key = obj["Key"]

                        # Check if file extension is supported
                        ext = Path(key).suffix.lower()
                        if ext not in SUPPORTED_EXTENSIONS:
                            logger.debug(f"Skipping unsupported file: {key}")
                            continue

                        # Process the document
                        if self._process_document(
                            session, s3_client, bucket_name, key, source
                        ):
                            processed_count += 1

                logger.info(
                    f"S3 crawl completed. Processed {processed_count} documents"
                )

            except ClientError as e:
                logger.error(f"Error listing S3 objects: {e}")

        self.engine.dispose()

    def _process_document(
        self, session: Session, s3_client, bucket_name: str, key: str, source: Source
    ) -> bool:
        """Download and process a single document from S3."""
        uri = f"s3://{bucket_name}/{key}"
        logger.info(f"Processing {uri}")

        # Check if document already exists
        existing_doc = session.execute(
            select(Document).where(
                Document.source_id == self.source_id, Document.uri == uri
            )
        ).scalar_one_or_none()

        if existing_doc and existing_doc.is_ignored:
            logger.info(f"Skipping ignored document: {uri}")
            return False

        # Download file to temporary location
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=Path(key).suffix
        ) as tmp_file:
            tmp_path = tmp_file.name

        try:
            s3_client.download_file(bucket_name, key, tmp_path)

            # Convert document to markdown using docling
            try:
                result = self.converter.convert(tmp_path)
                markdown_content = result.document.export_to_markdown()
            except Exception as e:
                logger.error(f"Docling conversion failed for {uri}: {e}")
                # For text files, try reading directly
                if Path(key).suffix.lower() in {".txt", ".md"}:
                    try:
                        with open(tmp_path, "r", encoding="utf-8") as f:
                            markdown_content = f.read()
                    except Exception as read_error:
                        logger.error(f"Failed to read text file {uri}: {read_error}")
                        return False
                else:
                    return False

            if not markdown_content:
                logger.warning(f"No content extracted from {uri}")
                return False

            # Create or update document
            if existing_doc:
                document = existing_doc
            else:
                document = Document(
                    source_id=self.source_id,
                    uri=uri,
                    status="indexed",
                )
                session.add(document)

            document.content = markdown_content
            document.status = "indexed"
            document.hash_value = markdown_content
            document.language = markdown_content
            document.length = markdown_content
            document.title = Path(key).stem

            meta = dict(document.meta or {})
            doc_type = guess_document_type(uri, None)
            if doc_type:
                meta["doc_type"] = doc_type

            if doc_type == "html":
                raw_html = None
                try:
                    with open(
                        tmp_path, "r", encoding="utf-8", errors="ignore"
                    ) as raw_file:
                        raw_html = raw_file.read()
                except Exception:
                    raw_html = None

                if has_html_form(raw_html):
                    meta["form"] = True
                elif "form" in meta:
                    meta.pop("form", None)
            elif "form" in meta:
                meta.pop("form", None)

            document.meta = meta

            session.commit()
            logger.info(f"Indexed {uri}")
            return True

        except ClientError as e:
            logger.error(f"Failed to download {uri}: {e}")
            return False
        finally:
            # Clean up temporary file
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


def main(source_id: int):
    """Entry point for S3 crawler."""
    crawler = S3Crawler(source_id)
    crawler.crawl()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m jobs.crawler.s3_crawler <source_id>")
        sys.exit(1)
    main(int(sys.argv[1]))
