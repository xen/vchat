from types import SimpleNamespace

import jinja2


def test_error_templates_render_contact_link_on_separate_line() -> None:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("vchat/templates"),
        autoescape=True,
    )

    def translate(value: str) -> str:
        return value

    context = {
        "_": translate,
        "csrf_token": None,
        "current_year": 2026,
        "debug": False,
        "get_flash_messages": lambda: [],
        "meta": SimpleNamespace(title="Error", description=None, author=None),
        "page_title": None,
        "project_settings": {},
        "request": SimpleNamespace(
            path_qs="/broken",
            rel_url=SimpleNamespace(path="/broken"),
        ),
        "static_version": "test",
        "url": lambda _name, **_kwargs: "/",
        "user": None,
    }

    for code in (403, 404, 405, 500):
        html = env.get_template(f"misc/{code}.html").render(context)
        assert (
            '<p class="mb-5 text-base font-normal text-gray-500 md:text-lg dark:text-gray-400">\n'
            '        <a href="https://dzen.dev?from=vchat" target="_blank" '
            'rel="noopener">Please contact us.</a>\n'
            "      </p>"
            in html
        )
        assert "href='/about/contact'" not in html
