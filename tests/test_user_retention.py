"""User lifecycle against PostgreSQL, in a disposable isolated schema."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiohttp import web
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vchat.models import AdminEvent, User, UserSession
from vchat.settings import cfg
from vchat.views.projects import views as projects
from vchat.views.auth import views as auth
import vchat.middlewares as middleware


@pytest_asyncio.fixture
async def database():
    schema = f"test_retention_{uuid4().hex}"
    owner = create_async_engine(cfg.database_uri)
    async with owner.begin() as conn:
        await conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        cfg.database_uri,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync: User.metadata.create_all(
                    sync,
                    tables=[
                        User.__table__,
                        UserSession.__table__,
                        AdminEvent.__table__,
                    ],
                )
            )
            # Materialize the schema before this migration and existing history.
            await conn.execute(
                sa.text("ALTER TABLE users DROP CONSTRAINT ck_users_blocked_inactive")
            )
            await conn.execute(sa.text("ALTER TABLE users DROP COLUMN blocked_at"))
            await conn.execute(
                sa.text("""
                INSERT INTO users(id,email,name,password,is_active,is_ldap) VALUES
                (1,'actor@example.com','Actor','hash',true,false),
                (2,'target@example.com','Target','hash',true,false),
                (3,'inactive@example.com','Inactive','hash',false,false)
            """)
            )
            await conn.execute(
                sa.text("""
                INSERT INTO user_session(user_id,session_id,last_seen_at) VALUES
                (1,'actor-session',now()), (2,'target-session',now())
            """)
            )
            await conn.execute(
                sa.text("""
                INSERT INTO admin_event(user_id,user_email,event_name) VALUES
                (2,'target@example.com','historical_action')
            """)
            )
            before = (
                await conn.execute(
                    sa.text(
                        "SELECT id,email,name,password,is_active,is_ldap FROM users ORDER BY id"
                    )
                )
            ).all()
            spec = importlib.util.spec_from_file_location(
                "retention_migration",
                Path(__file__).parents[1]
                / "migrations/versions/t4u5v6w7x8y9_permanent_user_block.py",
            )
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)

            def upgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.upgrade()

            await conn.run_sync(upgrade)
            after = (
                await conn.execute(
                    sa.text(
                        "SELECT id,email,name,password,is_active,is_ldap FROM users ORDER BY id"
                    )
                )
            ).all()
            assert before == after
            assert await conn.scalar(sa.text("SELECT count(*) FROM user_session")) == 2
            assert await conn.scalar(sa.text("SELECT count(*) FROM admin_event")) == 1
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with owner.begin() as conn:
            await conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner.dispose()


class Request(dict):
    def __init__(
        self, db, action="user_block_permanently", target="2", method="POST", actor=1
    ):
        super().__init__(
            db=db, user=SimpleNamespace(id=actor, email="actor@example.com")
        )
        self.match_info = {"action": action, "item_id": target}
        self.method = method
        self.headers = {"HX-Request": "true"}
        self.remote = "127.0.0.1"
        self.path = f"/actions/{action}/{target}"
        self.path_qs = self.path


async def block(db, target="2", actor=1):
    return await projects.project_action.__wrapped__.__wrapped__(
        Request(db, target=target, actor=actor)
    )


@pytest.mark.asyncio
async def test_lifecycle_preserves_records_revokes_sessions_and_audits(
    database, monkeypatch
):
    async with database() as db:
        response = await block(db)
        assert response.headers["HX-Refresh"] == "true"
        target = await db.get(User, 2)
        assert target.blocked_at is not None and target.is_active is False
        assert target.email == "target@example.com" and target.password == "hash"
        assert await db.scalar(sa.select(sa.func.count(User.id))) == 3
        session = await db.scalar(
            sa.select(UserSession).where(UserSession.user_id == 2)
        )
        assert session.revoked_at and session.revoked_reason == "permanent_block"
        assert (
            await db.scalar(sa.select(UserSession).where(UserSession.user_id == 1))
        ).revoked_at is None
        events = (await db.scalars(sa.select(AdminEvent).order_by(AdminEvent.id))).all()
        assert events[0].user_id == 2 and events[0].event_name == "historical_action"
        assert events[1].user_id == 1
        assert "user_block_permanently target_user_id=2" in events[1].event_name
        await db.rollback()
        with pytest.raises(web.HTTPConflict):
            await block(db)
        await db.rollback()
        for method in ("GET", "POST"):
            with pytest.raises(web.HTTPForbidden):
                await projects.project_action.__wrapped__.__wrapped__(
                    Request(db, "user_password", method=method)
                )
            await db.rollback()
        with pytest.raises(web.HTTPNotFound) as removed:
            await projects.project_action.__wrapped__.__wrapped__(
                Request(db, "user_delete")
            )
        assert "Location" not in removed.value.headers

        class Session(dict):
            def invalidate(self):
                self.clear()

        cookie = Session(
            user_id=2,
            session_id="target-session",
            login_at=int(datetime.now(timezone.utc).timestamp()),
        )

        async def get_session(_request):
            return cookie

        monkeypatch.setattr(middleware, "get_session", get_session)
        request = Request(db)
        request.path = "/sources/"

        async def protected_operation(req):
            assert req["user"] is None
            assert not req["auth_session"]
            return web.Response(status=403)

        response = await middleware.auth_middleware(request, protected_operation)
        assert response.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    [
        "TRUNCATE users CASCADE",
        "DELETE FROM users WHERE id=1",
        "DELETE FROM users WHERE id=2",
        "UPDATE users SET blocked_at=NULL WHERE id=2",
        "UPDATE users SET is_active=true WHERE id=2",
        "UPDATE users SET password='new' WHERE id=2",
        "UPDATE users SET email='reuse@example.com' WHERE id=2",
    ],
)
async def test_database_prevents_erasure_and_restoration(database, statement):
    async with database() as db:
        await block(db)
        with pytest.raises(DBAPIError):
            await db.execute(sa.text(statement))
            await db.commit()
        await db.rollback()
        assert await db.scalar(sa.select(sa.func.count(User.id))) == 3
        assert (await db.get(User, 2)).blocked_at is not None


@pytest.mark.asyncio
async def test_inactive_is_distinct_and_admin_guard(database):
    async with database() as db:
        with pytest.raises(web.HTTPBadRequest, match=""):
            await block(db, target="1")
        await db.rollback()
        # An inactive actor cannot bypass the guard to block the last active admin.
        await block(db)
        with pytest.raises(web.HTTPForbidden):
            await block(db, target="1", actor=2)
        await db.rollback()
        inactive = await db.get(User, 3)
        assert inactive.blocked_at is None
        inactive.is_active = True
        await db.commit()
        await block(db, target="3")
        assert inactive.blocked_at is not None
        assert (await db.get(User, 1)).is_active


@pytest.mark.asyncio
async def test_finish_login_rechecks_status_after_credentials(database, monkeypatch):
    async with database() as db:
        stale = await db.get(User, 2)
        await db.commit()
        async with database() as other:
            await block(other)

        async def new_session(_request):
            pytest.fail("Blocked user must never get a new session")

        monkeypatch.setattr(auth, "new_session", new_session)
        with pytest.raises(web.HTTPForbidden):
            await auth._finish_login(
                Request(db), user=stale, redis=None, failure_key="unused"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("ldap", [False, True])
async def test_login_routes_reject_permanently_blocked_user(
    database, monkeypatch, ldap
):
    from unittest.mock import AsyncMock
    from vchat.settings import REDIS_KEY

    class Form:
        def __init__(self, _data, meta):
            self.email = SimpleNamespace(data="target@example.com", errors=[])
            self.password = SimpleNamespace(data="password")

        def validate(self):
            return True

        def add_email_error(self, message):
            self.email.errors.append(message)

    async with database() as db:
        user = await db.get(User, 2)
        user.is_ldap = ldap
        await db.commit()
        await block(db)
        request = Request(db)
        request.app = {
            REDIS_KEY: SimpleNamespace(
                exists=AsyncMock(return_value=False), set=AsyncMock()
            )
        }
        request.rel_url = SimpleNamespace(query={})
        request.post = AsyncMock(return_value={})
        monkeypatch.setattr(auth, "get_session", AsyncMock(return_value={}))
        monkeypatch.setattr(auth.forms, "Login", Form)
        monkeypatch.setattr(auth.cfg, "auth_basic_enabled", True)
        monkeypatch.setattr(auth.cfg, "auth_ldap_enabled", True)
        monkeypatch.setattr(
            auth,
            "authenticate_ldap",
            AsyncMock(return_value={"email": "target@example.com", "name": "Target"}),
        )
        new_session = AsyncMock(
            side_effect=AssertionError("Blocked login created a session")
        )
        monkeypatch.setattr(auth, "new_session", new_session)
        login = auth.login_ldap if ldap else auth.login
        response = await login.__wrapped__.__wrapped__(request)
        assert "заблокирован" in " ".join(response["form"].email.errors)
        new_session.assert_not_called()


@pytest.mark.asyncio
async def test_block_only_accepts_post(database):
    async with database() as db:
        with pytest.raises(web.HTTPMethodNotAllowed):
            await projects.project_action.__wrapped__.__wrapped__(
                Request(db, method="GET")
            )
        assert (await db.get(User, 2)).blocked_at is None


@pytest.mark.asyncio
async def test_protected_mutation_rechecks_actor_after_middleware(
    database, monkeypatch
):
    from unittest.mock import AsyncMock
    import vchat.utils as utils

    class Cookie(dict):
        def invalidate(self):
            self.clear()

    async with database() as db:
        await block(db)
        request = Request(db, "source_delete", actor=2)
        cookie = Cookie(user_id=2)
        monkeypatch.setattr(utils, "get_session", AsyncMock(return_value=cookie))
        operation = AsyncMock()
        protected = utils.login_required()(operation)
        with pytest.raises(web.HTTPForbidden):
            await protected(request)
        operation.assert_not_called()
        assert not cookie


@pytest.mark.asyncio
async def test_open_notification_socket_closes_after_block(database, monkeypatch):
    from unittest.mock import AsyncMock
    from vchat.views.user import views as notifications

    async with database() as db:
        session_id = await db.scalar(
            sa.select(UserSession.id).where(UserSession.user_id == 2)
        )
        await db.rollback()
        await block(db)
    monkeypatch.setattr(notifications, "async_session_factory", database)
    monkeypatch.setattr(notifications.asyncio, "sleep", AsyncMock())
    ws = SimpleNamespace(closed=False, close=AsyncMock())
    await notifications._watch_session(ws, {"auth_user_session_id": session_id})
    ws.close.assert_awaited_once_with(code=1008, message=b"Session revoked")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_removed_user_url_is_unroutable_without_redirect(monkeypatch, method):
    from aiohttp.test_utils import make_mocked_request
    from vchat import routes

    # Test the actual registered path; static asset builds are unrelated here.
    monkeypatch.setattr(routes, "_add_static_build_routes", lambda _app: None)
    app = web.Application()
    routes.setup_routes(app)
    match = await app.router.resolve(
        make_mocked_request(method, "/actions/user_delete/2")
    )
    assert match.http_exception.status == 404
    assert "Location" not in match.http_exception.headers


@pytest.mark.asyncio
async def test_concurrent_blocks_keep_an_active_administrator(database):
    import asyncio

    async with database() as first, database() as second:
        results = await asyncio.gather(
            block(first, target="2", actor=1),
            block(second, target="1", actor=2),
            return_exceptions=True,
        )
        assert sum(result.status == 200 for result in results) == 1
        assert sum(isinstance(result, web.HTTPForbidden) for result in results) == 1
        await first.rollback()
        await second.rollback()
        active = await first.scalar(
            sa.select(sa.func.count(User.id)).where(
                User.is_active.is_(True), User.blocked_at.is_(None)
            )
        )
        assert active == 1


@pytest.mark.asyncio
async def test_block_waits_for_an_already_authorized_write(database, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    import vchat.utils as utils

    monkeypatch.setattr(utils, "get_session", AsyncMock(return_value={"user_id": 2}))
    entered = asyncio.Event()
    finish = asyncio.Event()
    async with database() as writer, database() as blocker:
        request = Request(writer, "source_delete", actor=2)

        @utils.login_required()
        async def operation(req):
            entered.set()
            await finish.wait()
            req["db"].add(
                AdminEvent(
                    user_id=2,
                    user_email="target@example.com",
                    event_name="authorized_before_block",
                )
            )
            await req["db"].commit()
            return web.Response()

        writing = asyncio.create_task(operation(request))
        await entered.wait()
        blocking = asyncio.create_task(block(blocker))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(blocking), timeout=0.1)
        finally:
            finish.set()
        await writing
        await blocking
        await writer.rollback()
        events = (
            await writer.scalars(sa.select(AdminEvent).order_by(AdminEvent.id))
        ).all()
        assert events[-2].event_name == "authorized_before_block"
        assert "user_block_permanently" in events[-1].event_name
