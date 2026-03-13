import logging
import sys
from typing import Any, Dict, List

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobs.db import create_sync_engine
from core.models import Document, Source
from core.settings import config
from core.utils import get_file_hash

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_API_URL = "https://www.googleapis.com/drive/v3"
REQUEST_TIMEOUT = 30


def refresh_access_token(refresh_token: str) -> str | None:
    """Refresh Google Access Token."""
    data = {
        "client_id": config["google_client_id"],
        "client_secret": config["google_client_secret"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    response = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        logger.error("Failed to refresh token: %s", response.text)
        return None
    token_data = response.json()
    return token_data.get("access_token")


def list_files(
    http: requests.Session, access_token: str, folder_id: str
) -> List[Dict[str, Any]]:
    """Recursively list files in a Google Drive folder."""
    files: List[Dict[str, Any]] = []
    params = {
        "q": f"'{folder_id}' in parents and trashed = false",
        "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime)",
        "pageSize": 100,
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        response = http.get(
            f"{GOOGLE_DRIVE_API_URL}/files",
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            logger.error("Failed to list files: %s", response.text)
            break

        data = response.json()
        current_files = data.get("files", [])

        for file in current_files:
            if file["mimeType"] == "application/vnd.google-apps.folder":
                files.extend(list_files(http, access_token, file["id"]))
            else:
                files.append(file)

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        params["pageToken"] = page_token

    return files


def download_file_content(
    http: requests.Session, access_token: str, file_id: str, mime_type: str
) -> str:
    """Download file content."""
    headers = {"Authorization": f"Bearer {access_token}"}

    if mime_type.startswith("application/vnd.google-apps."):
        export_mime = "text/plain"
        if "spreadsheet" in mime_type:
            export_mime = "text/csv"

        url = f"{GOOGLE_DRIVE_API_URL}/files/{file_id}/export"
        params = {"mimeType": export_mime}
    else:
        url = f"{GOOGLE_DRIVE_API_URL}/files/{file_id}"
        params = {"alt": "media"}

    response = http.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        logger.error("Failed to download file %s: %s", file_id, response.text)
        return ""
    return response.text


def crawl_google_drive(source_id: int):
    """Main crawler function."""
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            source = session.scalar(select(Source).where(Source.id == source_id))
            if not source:
                logger.error("Source %s not found", source_id)
                return

            refresh_token = source.config.get("refresh_token")
            folder_id = source.config.get("folder_id")

            if not refresh_token or not folder_id:
                logger.error("Missing configuration for source %s", source_id)
                return

            access_token = refresh_access_token(refresh_token)
            if not access_token:
                logger.error("Failed to obtain access token")
                return

            with requests.Session() as http:
                files = list_files(http, access_token, folder_id)
                logger.info(
                    "Found %s files in Google Drive folder %s", len(files), folder_id
                )

                for file in files:
                    content = download_file_content(
                        http, access_token, file["id"], file["mimeType"]
                    )
                    if not content:
                        continue

                    doc = session.scalar(
                        select(Document).where(
                            Document.source_id == source_id,
                            Document.uri == f"gdrive://{file['id']}",
                        )
                    )

                    doc_hash = get_file_hash(content)

                    if doc:
                        if doc.hash_value != doc_hash:
                            doc.content = content
                            doc.hash_value = content
                            doc.length = content
                            doc.language = content
                            doc.title = file["name"]
                            doc.status = "indexed"
                            logger.info("Updated document %s", file["name"])
                    else:
                        doc = Document(
                            source_id=source_id,
                            uri=f"gdrive://{file['id']}",
                            content=content,
                            title=file["name"],
                            status="indexed",
                            meta={
                                "gdrive_id": file["id"],
                                "mimeType": file["mimeType"],
                            },
                        )
                        doc.hash_value = content
                        doc.length = content
                        doc.language = content
                        session.add(doc)
                        logger.info("Created document %s", file["name"])

                session.commit()
    finally:
        engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m jobs.crawler.google_drive_crawler <source_id>")
        sys.exit(1)

    crawl_google_drive(int(sys.argv[1]))
