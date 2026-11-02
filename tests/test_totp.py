"""Real HTTP/cookie/CSRF and PostgreSQL security boundary tests, isolated schema."""

import asyncio
import base64
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import AsyncMock

import aiohttp_jinja2
import jinja2
import pyotp
import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer
from aiohttp_session import session_middleware
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import vchat.middlewares as mdw
from vchat.app import init_jinja
from vchat.models import User, UserSession, UserTotp, AdminEvent
from vchat.settings import cfg, REDIS_KEY
from vchat.totp import accepted_counter, cipher
from vchat.utils import login_required
from vchat.views.auth import views as auth
from vchat.views.auth.totp import totp
from vchat.views.user.views import notify_ws


@pytest_asyncio.fixture
async def runtime(monkeypatch):
    schema = "test_totp_" + uuid4().hex
    owner = create_async_engine(cfg.database_uri)
    async with owner.begin() as conn:
        await conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        cfg.database_uri, connect_args={"server_settings": {"search_path": schema}}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: User.metadata.create_all(
                c,
                tables=[
                    User.__table__,
                    UserSession.__table__,
                    UserTotp.__table__,
                    AdminEvent.__table__,
                ],
            )
        )
    async with factory() as db:
        db.add(
            User(
                id=1,
                email="admin@example.com",
                name="Admin",
                password=auth.password_context.hash("test-password"),
                is_active=True,
                is_ldap=False,
            )
        )
        await db.commit()
    monkeypatch.setattr(mdw, "async_session_factory", factory)
    app = web.Application(
        middlewares=[
            session_middleware(EncryptedCookieStorage(cfg.cookie_key, secure=False)),
            mdw.meta_middleware,
            mdw.db_session_middleware,
            mdw.auth_middleware,
        ]
    )
    app[REDIS_KEY] = AsyncMock()
    app[REDIS_KEY].exists.return_value = 0
    aiohttp_jinja2.setup(
        app,
        loader=jinja2.PackageLoader("vchat", "templates"),
        context_processors=[init_jinja, aiohttp_jinja2.request_processor],
    )
    # Render the real TOTP body/macros without unrelated static/layout routes.
    env = aiohttp_jinja2.get_env(app)
    env.loader = jinja2.ChoiceLoader(
        [
            jinja2.DictLoader({"admin.html": "{% block fullbody %}{% endblock %}"}),
            env.loader,
        ]
    )
    app.router.add_get(
        "/static/images/{filename:.*}",
        lambda request: web.Response(),
        name="static_images",
    )
    app.router.add_route("*", "/login/", auth.login, name="login")
    app.router.add_get("/login/totp/", totp, name="totp")
    app.router.add_post("/login/totp/", totp)

    @login_required()
    async def protected(request):
        return web.Response(text=str(request["user"].id))

    app.router.add_get("/", protected, name="index")
    app.router.add_post("/operation", protected)
    app.router.add_get("/ws/notify", notify_ws)
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    try:
        yield client, factory, app
    finally:
        await client.close()
        await engine.dispose()
        async with owner.begin() as conn:
            await conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner.dispose()


async def begin(client, next_path=""):
    query = {"next": next_path} if next_path else {}
    response = await client.get("/login/")
    html = await response.text()
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)[1]
    response = await client.post(
        "/login/",
        params=query,
        data={
            "email": "admin@example.com",
            "password": "test-password",
            "csrf_token": csrf,
        },
        allow_redirects=False,
    )
    assert response.status == 302 and response.headers["Location"] == "/login/totp/"


async def page(client):
    response = await client.get("/login/totp/", allow_redirects=False)
    html = await response.text()
    assert response.status == 200, html
    assert response.headers["Cache-Control"] == "no-store, private"
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)[1]
    return csrf, html


async def secret(factory):
    async with factory() as db:
        state = await db.get(UserTotp, 1)
        return (
            cipher().decrypt((state.secret or state.pending_secret).encode()).decode()
        )


@pytest.mark.asyncio
async def test_enrollment_login_replay_csrf_and_no_password_only_access(runtime):
    client, factory, _ignore_app = runtime
    await begin(client)
    for path, method in [("/", "GET"), ("/operation", "POST"), ("/ws/notify", "GET")]:
        response = await client.request(method, path, allow_redirects=False)
        assert response.status == 302 and response.headers["Location"].startswith(
            "/login/"
        )
    csrf, html = await page(client)
    assert html.index("<h1") < html.index("<form")
    png = base64.b64decode(re.search('data:image/png;base64,([^"]+)', html)[1])
    assert png.startswith(b"\x89PNG") and len(png) > 500
    key = await secret(factory)
    assert key not in html and "otpauth" not in html
    code = pyotp.TOTP(key).now()
    bad_csrf = await client.post(
        "/login/totp/", data={"code": code, "csrf_token": "bad"}, allow_redirects=False
    )
    assert bad_csrf.status == 400
    async with factory() as db:
        state = await db.get(UserTotp, 1)
        assert state.secret is None and state.failures == 0
    response = await client.post(
        "/login/totp/", data={"code": code, "csrf_token": csrf}, allow_redirects=False
    )
    assert response.status == 302
    assert (await client.get("/")).status == 200
    async with factory() as db:
        state = await db.get(UserTotp, 1)
        assert state.secret and key not in state.secret and state.pending_secret is None
        assert state.token_hash is None
        assert (await db.scalar(sa.select(UserSession))).totp_verified
    await begin(client, "/sessions/")
    csrf, html = await page(client)
    assert "data:image" not in html and "QR-код подключения" not in html
    response = await client.post(
        "/login/totp/", data={"code": code, "csrf_token": csrf}, allow_redirects=False
    )
    assert response.status == 400
    assert (await client.get("/", allow_redirects=False)).status == 302
    fresh_code = pyotp.TOTP(key).at(
        (int(datetime.now(timezone.utc).timestamp()) // 30 + 1) * 30
    )
    response = await client.post(
        "/login/totp/",
        data={"code": fresh_code, "csrf_token": csrf},
        allow_redirects=False,
    )
    assert response.status == 302 and response.headers["Location"] == "/sessions/"
    assert (await client.get("/")).status == 200


@pytest.mark.asyncio
async def test_expiry_abandoned_enrollment_and_password_change(runtime):
    client, factory, _ignore_app = runtime
    await begin(client)
    first_secret = await secret(factory)
    await begin(client)
    assert await secret(factory) != first_secret
    async with factory() as db:
        (await db.get(UserTotp, 1)).expires_at = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
        await db.commit()
    assert (await client.get("/login/totp/", allow_redirects=False)).status == 302
    async with factory() as db:
        state = await db.get(UserTotp, 1)
        assert state.secret is None and state.pending_secret is None
    await begin(client)
    async with factory() as db:
        (await db.get(User, 1)).password = "new-hash"
        await db.commit()
    assert (await client.get("/login/totp/", allow_redirects=False)).status == 302


@pytest.mark.asyncio
async def test_account_bruteforce_survives_new_password_login(runtime):
    client, factory, _ignore_app = runtime
    await begin(client)
    for i in range(5):
        csrf, _ignore_html = await page(client)
        response = await client.post(
            "/login/totp/",
            data={"code": "invalid", "csrf_token": csrf},
            allow_redirects=False,
        )
        assert response.status == (429 if i == 4 else 400)
        if i < 4:
            await begin(client)
    response = await client.get("/login/")
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', await response.text())[1]
    response = await client.post(
        "/login/",
        data={
            "email": "admin@example.com",
            "password": "test-password",
            "csrf_token": csrf,
        },
        allow_redirects=False,
    )
    assert response.status == 429
    async with factory() as db:
        assert (await db.get(UserTotp, 1)).failures == 5
        assert await db.scalar(sa.select(sa.func.count(UserSession.id))) == 0


@pytest.mark.asyncio
async def test_blocked_user_cannot_confirm_or_start_and_old_session_rejected(runtime):
    client, factory, _ignore_app = runtime
    await begin(client)
    csrf, _ignore_html = await page(client)
    code = pyotp.TOTP(await secret(factory)).now()
    async with factory() as db:
        user = await db.get(User, 1)
        user.is_active = False
        user.blocked_at = datetime.now(timezone.utc)
        await db.commit()
    assert (
        await client.post(
            "/login/totp/",
            data={"code": code, "csrf_token": csrf},
            allow_redirects=False,
        )
    ).status == 403
    async with factory() as db:
        assert await db.scalar(sa.select(sa.func.count(UserSession.id))) == 0
        assert (await db.get(UserTotp, 1)).secret is None


@pytest.mark.asyncio
async def test_concurrent_confirmation_only_creates_one_session(runtime):
    client, factory, _ignore_app = runtime
    await begin(client)
    csrf, _ignore_html = await page(client)
    code = pyotp.TOTP(await secret(factory)).now()
    results = await asyncio.gather(
        *(
            client.post(
                "/login/totp/",
                data={"code": code, "csrf_token": csrf},
                allow_redirects=False,
            )
            for _ in range(2)
        )
    )
    assert sorted(r.status for r in results) == [302, 403]
    async with factory() as db:
        assert await db.scalar(sa.select(sa.func.count(UserSession.id))) == 1


def test_rfc6238_time_window_and_replay():
    key = base64.b32encode(b"12345678901234567890").decode()
    now = datetime.fromtimestamp(59, timezone.utc)
    assert accepted_counter(key, "287082", now, None) == 1
    assert accepted_counter(key, "287082", now, 1) is None
    otp = pyotp.TOTP(key)
    assert (
        accepted_counter(
            key, otp.at(30 * 10), datetime.fromtimestamp(30 * 12, timezone.utc), None
        )
        is None
    )
    assert (
        accepted_counter(
            key, otp.at(30 * 11), datetime.fromtimestamp(30 * 12, timezone.utc), None
        )
        == 11
    )
    assert (
        accepted_counter(
            key, otp.at(30 * 13), datetime.fromtimestamp(30 * 12, timezone.utc), None
        )
        == 13
    )


@pytest.mark.asyncio
async def test_old_password_only_cookie_and_block_after_totp(runtime):
    client, factory, app = runtime
    # Build an actual pre-TOTP encrypted cookie with the current cookie key.
    import json
    import time
    from cryptography.fernet import Fernet

    async with factory() as db:
        db.add(
            UserSession(
                user_id=1,
                session_id="old-password-session",
                last_seen_at=datetime.now(timezone.utc),
                totp_verified=False,
            )
        )
        await db.commit()
    cookie = (
        Fernet(cfg.cookie_key.encode())
        .encrypt(
            json.dumps(
                {
                    "created": int(time.time()),
                    "session": {
                        "user_id": 1,
                        "session_id": "old-password-session",
                        "login_at": int(time.time()),
                    },
                }
            ).encode()
        )
        .decode()
    )
    client.session.cookie_jar.update_cookies({"AIOHTTP_SESSION": cookie})
    assert (await client.get("/", allow_redirects=False)).status == 302
    await begin(client)
    csrf, _ignore_html = await page(client)
    code = pyotp.TOTP(await secret(factory)).now()
    assert (
        await client.post(
            "/login/totp/",
            data={"code": code, "csrf_token": csrf},
            allow_redirects=False,
        )
    ).status == 302
    assert (await client.get("/")).status == 200
    async with factory() as db:
        user = await db.get(User, 1)
        user.is_active = False
        user.blocked_at = datetime.now(timezone.utc)
        await db.commit()
    assert (await client.get("/", allow_redirects=False)).status == 302


@pytest.mark.asyncio
async def test_migration_preserves_users_and_revokes_only_local_sessions(runtime):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    client, factory, _ignore_app = runtime
    async with factory() as db:
        db.add(
            User(
                id=2,
                email="ldap@example.com",
                name="LDAP",
                is_active=True,
                is_ldap=True,
            )
        )
        await db.flush()
        db.add_all(
            [
                UserSession(user_id=1, session_id="local"),
                UserSession(user_id=2, session_id="ldap"),
            ]
        )
        await db.commit()
        await db.execute(sa.text("DROP TABLE user_totp"))
        await db.execute(sa.text("ALTER TABLE user_session DROP COLUMN totp_verified"))
        before = (
            await db.execute(
                sa.text("SELECT row_to_json(u)::text FROM users u ORDER BY id")
            )
        ).all()
        conn = await db.connection()
        spec = importlib.util.spec_from_file_location(
            "totp_migration",
            Path(__file__).parents[1]
            / "migrations/versions/u5v6w7x8y9z0_mandatory_totp.py",
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        def upgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()

        await conn.run_sync(upgrade)
        after = (
            await db.execute(
                sa.text("SELECT row_to_json(u)::text FROM users u ORDER BY id")
            )
        ).all()
        assert before == after and len(after) == 2
        rows = (
            await db.execute(
                sa.text(
                    "SELECT session_id,revoked_reason,totp_verified FROM user_session ORDER BY session_id"
                )
            )
        ).all()
        assert rows == [("ldap", None, False), ("local", "mandatory_totp", False)]
        await db.commit()


@pytest.mark.asyncio
async def test_wrong_storage_key_fails_validation(runtime, monkeypatch):
    from cryptography.fernet import Fernet, InvalidToken
    from vchat.totp import validate_totp_storage

    client, factory, _ignore_app = runtime
    await begin(client)
    async with factory() as db:
        await validate_totp_storage(db)
        monkeypatch.setattr(cfg, "totp_encryption_key", Fernet.generate_key().decode())
        with pytest.raises(InvalidToken):
            await validate_totp_storage(db)
