from __future__ import annotations

from datetime import date
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zipfile import BadZipFile, ZipFile

import pytest
from openpyxl import Workbook
from scrapy.http import HtmlResponse

from jobs.crawler.document_pipeline import (
    detect_binary_document_kind,
    extract_binary_url_document,
)
from jobs.crawler.pipelines import DatabasePipeline
from jobs.crawler.spiders.general import GeneralSpider
from jobs.documents.content import content_sha256
from jobs.documents.types import guess_document_type
from jobs.embedder import chunking
from vchat.views.projects.page_status import PageStatusError

MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def workbook_bytes(amount=125):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Ресурсы"
    sheet.append(["Название", "Количество", "Дата"])
    sheet.append(["Стипендия Альфа", amount, date(2026, 11, 2)])
    sheet.append(["Итог", "=SUM(B2:B2)"])
    sheet.merge_cells("A4:C4")
    sheet["A4"] = "Объединенный текст"
    sheet.protection.sheet = True
    hidden = workbook.create_sheet("Архив")
    hidden.sheet_state = "veryHidden"
    hidden.append(["Скрытый документ"])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


@pytest.mark.parametrize(
    "mime",
    [
        MIME,
        "application/octet-stream",
        "application/vnd.ms-excel",
        "application/vnd.ms-excel.sheet.macroenabled.12",
    ],
)
def test_excel_text_metadata_and_detection(mime):
    raw = workbook_bytes()
    assert detect_binary_document_kind(raw, mime) == "excel"
    content, title, meta = extract_binary_url_document(
        "https://example.org/resources.xlsx", raw, mime
    )
    assert title == "resources"
    assert "B2 [Количество]: 125" in content
    assert "C2 [Дата]: 2026-11-02T00:00:00" in content
    assert "Formula (not evaluated): =SUM(B2:B2)" in content
    assert content.count("Объединенный текст") == 1
    assert "Sheet: Архив (veryHidden)" in content
    assert meta["file"]["sheet_count"] == 2
    assert meta["file"]["formula_count"] == 1
    assert meta["extraction"]["extractor"] == "openpyxl"
    assert any(
        block["section_path"] == "Sheet: Ресурсы (visible)"
        for block in meta["structure"]
    )


def test_xlsm_container_is_read_without_macro_execution():
    # A macro-enabled content-type, with opaque VBA bytes; extraction never loads VBA.
    source = BytesIO(workbook_bytes())
    target = BytesIO()
    with ZipFile(source) as original, ZipFile(target, "w") as output:
        for item in original.infolist():
            value = original.read(item.filename)
            if item.filename == "[Content_Types].xml":
                value = value.replace(
                    b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                    b"application/vnd.ms-excel.sheet.macroEnabled.main+xml",
                )
            output.writestr(item, value)
        output.writestr("xl/vbaProject.bin", b"opaque VBA bytes, never executed")
    content, _title, meta = extract_binary_url_document(
        "https://example.org/resources.xlsm",
        target.getvalue(),
        "application/octet-stream",
    )
    assert "Стипендия Альфа" in content
    assert meta["file"]["format"] == "excel"
    assert guess_document_type("https://example.org/a.xlsm") == "office"


@pytest.mark.parametrize("extension", ["xlsx", "xlsm", "xls"])
def test_excel_extension_routes_corrupt_downloads_to_explicit_error(extension):
    url = f"https://example.org/a.{extension}"
    assert (
        detect_binary_document_kind(b"broken", "application/octet-stream", url)
        == "excel"
    )
    with pytest.raises(BadZipFile):
        extract_binary_url_document(url, b"broken", "application/octet-stream")


def test_excel_empty_and_legacy_fail_explicitly():
    buffer = BytesIO()
    Workbook().save(buffer)
    with pytest.raises(ValueError, match="no extractable cell text"):
        extract_binary_url_document(
            "https://example.org/empty.xlsx", buffer.getvalue(), MIME
        )
    with pytest.raises(ValueError, match="Legacy XLS and encrypted"):
        extract_binary_url_document(
            "https://example.org/file.xls",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
            "application/vnd.ms-excel",
        )


@pytest.mark.parametrize("extension", ["xlsx", "xlsm", "xls"])
def test_spider_discovers_excel_links(extension):
    spider = GeneralSpider(url="https://example.org", source_id=1, config={})
    links = spider._link_extractor.extract_links(
        HtmlResponse(
            url="https://example.org",
            body=f'<a href="/file.{extension}">Workbook</a>'.encode(),
            encoding="utf-8",
        )
    )
    assert [link.url for link in links] == [f"https://example.org/file.{extension}"]


class WordTokenizer:
    def __call__(self, text, **_options):
        return {"input_ids": text.split()}

    def decode(self, ids, **_options):
        return " ".join(ids)


def test_extracted_excel_chunks_keep_searchable_cells_and_sheet_context(monkeypatch):
    monkeypatch.setattr(chunking, "get_embed_tokenizer", lambda: WordTokenizer())
    content, _title, _meta = extract_binary_url_document(
        "https://example.org/a.xlsx", workbook_bytes(), MIME
    )
    chunks = chunking.chunk_document_text(content)
    matched = [chunk for chunk in chunks if "Стипендия Альфа" in chunk.text]
    assert matched
    assert all(chunk.section_path == "Sheet: Ресурсы (visible)" for chunk in matched)
    assert any("B2 [Количество]: 125" in chunk.text for chunk in matched)


@pytest.mark.parametrize(
    "previous_amount,force,expected",
    [(None, False, 1), (125, False, 0), (126, False, 1), (125, True, 1)],
)
def test_excel_pipeline_real_extraction_and_reindex(previous_amount, force, expected):
    previous = ""
    if previous_amount is not None:
        previous = extract_binary_url_document(
            "https://example.org/a.xlsx", workbook_bytes(previous_amount), MIME
        )[0]
    page = SimpleNamespace(
        id=56,
        source_id=1,
        uri="https://example.org/a.xlsx",
        meta={"force_reprocess_once": True} if force else {},
        status_error=None,
        is_hub_page=False,
        content_value=None,
        stable_count=0,
        error_count=0,
        check_interval_days=7,
        title="",
        content=previous,
        hash_value=content_sha256(previous),
    )
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute.return_value.scalars.return_value.first.return_value = page
    pipeline = DatabasePipeline.__new__(DatabasePipeline)
    pipeline.logger = MagicMock()
    pipeline.engine = MagicMock()
    pipeline._crawl_run_id = None
    with (
        patch("jobs.crawler.pipelines.Session", return_value=session),
        patch("jobs.crawler.pipelines.sync_document_has_chunks", return_value=True),
        patch("jobs.crawler.pipelines.find_duplicate_content_page", return_value=None),
        patch(
            "jobs.crawler.pipelines.source_trigger_rules_match_url", return_value=False
        ),
        patch("jobs.crawler.pipelines.sync_page_links"),
        patch("jobs.crawler.pipelines.update_page_shingles"),
        patch("jobs.crawler.pipelines.schedule_index_document") as schedule,
    ):
        pipeline.process_item(
            {
                "url": page.uri,
                "source_id": 1,
                "http_status": 200,
                "content_type": "application/octet-stream",
                "raw_content": workbook_bytes(),
                "content": "",
                "meta": {},
                "out_links": [],
            },
            MagicMock(),
        )
    assert schedule.call_count == expected
    assert "B2 [Количество]: 125" in page.content
    assert page.meta["file"]["format"] == "excel"
    assert page.raw_content


def test_excel_pipeline_reports_extraction_failure():
    pipeline = DatabasePipeline.__new__(DatabasePipeline)
    pipeline.engine = MagicMock()
    pipeline._crawl_run_id = None
    with patch("jobs.crawler.pipelines.save_page_status") as status:
        pipeline.process_item(
            {
                "url": "https://example.org/broken.xlsx",
                "source_id": 1,
                "http_status": 200,
                "content_type": MIME,
                "raw_content": b"broken",
                "content": "",
            },
            MagicMock(),
        )
    assert status.call_args.args[3] == PageStatusError.extraction_failed
    assert status.call_args.kwargs["error_info"].exception_class == "BadZipFile"


def test_small_workbook_is_indexable_and_never_metadata_only():
    from jobs.crawler.pipelines import is_low_content_page
    from jobs.crawler.tasks import _metadata_only_index_reason

    workbook = Workbook()
    workbook.active["A1"] = "Искомый текст"
    buffer = BytesIO()
    workbook.save(buffer)
    content, _title, meta = extract_binary_url_document(
        "https://example.org/a.xlsx", buffer.getvalue(), MIME
    )
    assert not is_low_content_page(content, meta)
    assert (
        _metadata_only_index_reason(SimpleNamespace(content=content, meta=meta)) is None
    )


def test_excel_text_limit_is_explicit_error(monkeypatch):
    from vchat.settings import cfg

    monkeypatch.setattr(cfg, "embedding_document_max_chars", 50)
    with pytest.raises(ValueError, match="exceeds embedding_document_max_chars"):
        extract_binary_url_document(
            "https://example.org/a.xlsx", workbook_bytes(), MIME
        )


def test_array_formula_is_stable_text_and_data_table_formula_is_explicit_error():
    from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = ArrayFormula(ref="A1:A2", text="=SUM(B1:B2)")
    sheet["B1"] = 12
    buffer = BytesIO()
    workbook.save(buffer)
    first = extract_binary_url_document(
        "https://example.org/a.xlsx", buffer.getvalue(), MIME
    )[0]
    second = extract_binary_url_document(
        "https://example.org/a.xlsx", buffer.getvalue(), MIME
    )[0]
    assert first == second
    assert "Formula (not evaluated): =SUM(B1:B2)" in first
    sheet["A1"] = DataTableFormula(ref="A1:A2")
    buffer = BytesIO()
    workbook.save(buffer)
    with pytest.raises(ValueError, match="data-table formulas"):
        extract_binary_url_document(
            "https://example.org/a.xlsx", buffer.getvalue(), MIME
        )


def test_sparse_cells_preserve_zero_false_and_hidden_rows():
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "Название"
    sheet["C1"] = "Число"
    sheet["A2"] = False
    sheet["C2"] = 0
    sheet.row_dimensions[2].hidden = True
    sheet.column_dimensions["C"].hidden = True
    buffer = BytesIO()
    workbook.save(buffer)
    content, _title, _meta = extract_binary_url_document(
        "https://example.org/a.xlsx", buffer.getvalue(), MIME
    )
    assert "A2 [Название]: False" in content
    assert "C2 [Число]: 0" in content
    assert "B2:" not in content
