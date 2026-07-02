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
