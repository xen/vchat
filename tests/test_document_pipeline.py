from __future__ import annotations

from jobs.crawler.document_pipeline import extract_url_document


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
