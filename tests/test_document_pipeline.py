from __future__ import annotations

from io import BytesIO

from docx import Document
from scrapy.linkextractors import IGNORED_EXTENSIONS

from jobs.crawler.document_pipeline import (
    extract_binary_url_document,
    extract_url_document,
)
from jobs.crawler.spiders.general import GeneralSpider


def test_extract_url_document_prefers_html_title_and_strips_auth_forms() -> None:
    html = """
    <html>
      <head>
        <title>Антибуллинговые карточки</title>
      </head>
      <body>
        <div class="auth-modal">
          <h2>Авторизация</h2>
          <form>
            <label>E-mail*</label>
            <input name="USER_LOGIN" />
            <label>Пароль*</label>
            <input type="password" name="USER_PASSWORD" />
          </form>
        </div>
        <main>
          <h1>Антибуллинговые карточки</h1>
          <p>Автор: АНО «БО "Журавлик"»</p>
          <p>Основное описание продукта.</p>
          <p>
            «Антибуллинговые карточки» помогают ребенку распознавать травлю,
            различать конфликт и буллинг, а также тренировать эмоциональную
            регуляцию, критическое мышление и безопасные способы реагирования
            на агрессию со стороны сверстников.
          </p>
          <p>
            Материал подходит для индивидуальной беседы и для групповой работы.
            Дополнительный текст нужен здесь только для того, чтобы тестовая
            страница не выглядела как пустая SPA-заглушка для эвристики
            `_static_text_length`.
          </p>
        </main>
      </body>
    </html>
    """

    content, title, meta = extract_url_document(
        "https://example.com/doc",
        html_body=html,
        content_type="text/html; charset=utf-8",
    )

    assert title == "Антибуллинговые карточки"
    assert "Авторизация" not in content
    assert "USER_LOGIN" not in content
    assert "Основное описание продукта." in content
    assert meta["extraction"]["extractor"] == "beautifulsoup"
    assert meta["extraction"]["boilerplate_removed_count"] >= 1


def test_extract_binary_url_document_extracts_docx_text_and_tables() -> None:
    document = Document()
    document.add_heading("Регламент поддержки", level=1)
    document.add_paragraph("PDF и Word документы должны попадать в базу знаний.")
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Раздел"
    table.rows[0].cells[1].text = "Ответственный"
    table.rows[1].cells[0].text = "Индексация"
    table.rows[1].cells[1].text = "Команда поиска"
    buffer = BytesIO()
    document.save(buffer)

    content, title, meta = extract_binary_url_document(
        "https://example.com/files/Support%20Rules.docx",
        buffer.getvalue(),
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
    )

    assert title == "Support Rules"
    assert "PDF и Word документы должны попадать в базу знаний." in content
    assert "| Раздел | Ответственный |" in content
    assert meta["doc_type"] == "office"
    assert meta["extraction"]["extractor"] == "python-docx"
    assert meta["file"]["table_count"] == 1


def test_general_spider_allows_word_and_pdf_links() -> None:
    spider = GeneralSpider(url="https://example.com", source_id=1, config={})
    try:
        assert {"pdf", "doc", "docx"}.issubset(IGNORED_EXTENSIONS)
        assert "pdf" not in spider._link_extractor.deny_extensions
        assert "doc" not in spider._link_extractor.deny_extensions
        assert "docx" not in spider._link_extractor.deny_extensions
    finally:
        spider.closed("test")
