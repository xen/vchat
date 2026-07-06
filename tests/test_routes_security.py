from __future__ import annotations

from aiohttp import web

from vchat.routes import setup_routes


def test_public_static_routes_do_not_follow_symlinks_or_mount_data() -> None:
    app = web.Application()
    setup_routes(app)

    static_prefixes: set[str] = set()
    for resource in app.router.resources():
        info = resource.get_info()
        prefix = info.get("prefix")
        if prefix:
            static_prefixes.add(prefix)
            assert getattr(resource, "_follow_symlinks", False) is False
        assert prefix != "/data/"
        assert info.get("path") != "/data/"

    assert "/static/assets" in static_prefixes
    assert "/static/chat" in static_prefixes


def test_chat_page_route_is_widget_only() -> None:
    app = web.Application()
    setup_routes(app)

    named_routes = set(app.router.named_resources())

    assert "project_chat" not in named_routes
    assert "project_chat_with_id" not in named_routes
    assert str(app.router["public_widget_chat"].url_for(code="widget-code")) == (
        "/chat/widget/widget-code"
    )
