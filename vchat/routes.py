from pathlib import Path

from aiohttp import web
from aiohttp_swagger3 import SwaggerDocs, SwaggerInfo, SwaggerUiSettings

from vchat.views.metrics import metrics_handler
from .views import frontend
from .views import health
from .views.admin import views as admin
from .views.api import views as api
from .views.auth import views as auth
from .views.chat import views as chat
from .views.projects import chats as project_chats
from .views.projects import views as projects
from .views.user import views as user


FRONTEND_DIST_PATH = Path(__file__).parent.parent / "frontend" / "dist"
FRONTEND_CHAT_DIST_PATH = Path(__file__).parent.parent / "frontend_chat" / "dist"


async def _static_favicon(request: web.Request) -> web.FileResponse:
    return web.FileResponse(FRONTEND_DIST_PATH / "favicon.ico")


def _add_static_build_routes(app: web.Application) -> None:
    app.router.add_get(
        "/static/favicon.ico", _static_favicon, name="static_favicon_ico"
    )
    frontend_dirs = (
        "assets",
        "audio",
        "favicon",
        "fonts",
        "images",
        "js",
        "mail",
    )
    for dirname in frontend_dirs:
        app.router.add_static(
            f"/static/{dirname}/",
            path=(FRONTEND_DIST_PATH / dirname).absolute(),
            name=f"static_{dirname}",
            follow_symlinks=False,
        )
    app.router.add_static(
        "/static/chat/",
        path=FRONTEND_CHAT_DIST_PATH.absolute(),
        name="static_chat",
        follow_symlinks=False,
    )


def setup_routes(app: web.Application) -> None:
    add = app.router.add_route

    # observability
    add("GET", "/metrics", metrics_handler, name="metrics")
    add("GET", "/health/live", health.live, name="health_live")
    add("GET", "/health/ready", health.ready, name="health_ready")

    # frontend
    add("GET", "/check", frontend.healthcheck)
    add("GET", "/demo", frontend.demo_page, name="demo_page")
    add("GET", r"/widget/{code:[a-zA-Z0-9_-]+}", frontend.widget_js, name="widget")
    add(
        "GET",
        "/api/triggers/resolve",
        frontend.widget_triggers_resolve,
        name="widget_triggers_resolve",
    )
    add("*", "/robots.txt", frontend.robots_txt)
    add("*", "/favicon.ico", frontend.favicon)

    # public api
    swagger = SwaggerDocs(
        app,
        validate=False,
        info=SwaggerInfo(
            title="vchat Public API",
            version="1.0.0",
            description="Public endpoints for document update integrations.",
        ),
        swagger_ui_settings=SwaggerUiSettings(
            path="/api-docs",
            validatorUrl=None,
        ),
    )
    swagger.add_routes(
        [
            web.post("/api/update", api.update_document, name="api_update"),
        ]
    )

    # auth
    add("*", "/login/", auth.login, name="login")
    add("*", "/login/ldap/", auth.login_ldap, name="login_ldap")
    add("GET", "/sessions/", auth.sessions, name="sessions")
    add("POST", "/sessions/", auth.sessions_action, name="sessions_action")
    add("*", "/logout/", auth.logout, name="logout")

    # admin
    add("*", "/users", admin.user_list, name="users")
    add("*", "/api-clients", admin.api_client_list, name="api_clients")
    add("GET", "/events", admin.event_list, name="admin_events")

    # single-project admin pages
    add("GET", "/", projects.project_stats, name="index")
    add("GET", "/page", projects.project_view, name="project_view")
    add(
        "GET",
        "/documents/csv",
        projects.project_documents_csv,
        name="project_documents_csv",
    )
    add(
        "GET",
        "/files/json",
        projects.project_files_json,
        name="project_files_json",
    )
    add("*", "/source", projects.project_edit_sources, name="project_edit_sources")
    add(
        "*",
        r"/source/{source_id:[0-9]+}",
        projects.project_source_settings,
        name="project_source_settings",
    )
    add(
        "GET",
        r"/source/{source_id:[0-9]+}/sitemaps",
        projects.source_sitemaps,
        name="source_sitemaps",
    )
    add(
        "GET",
        r"/page/{document_id:[0-9]+}",
        projects.project_document_detail,
        name="project_document_detail",
    )
    add(
        "GET",
        r"/page/{document_id:[0-9]+}/content",
        projects.project_document_content,
        name="project_document_content",
    )
    add(
        "GET",
        r"/page/{document_id:[0-9]+}/content/rest",
        projects.project_document_content_rest,
        name="project_document_content_rest",
    )
    add("GET", "/stats", projects.project_stats, name="project_stats")
    add("*", "/files", projects.project_files, name="project_files")
    add("*", "/triggers", projects.project_triggers, name="project_triggers")
    add("GET", "/llm-cache", projects.project_llm_cache, name="project_llm_cache")
    add(
        "GET",
        "/triggers/count",
        projects.project_trigger_rule_count,
        name="project_trigger_rule_count",
    )
    add(
        "*",
        r"/file/{document_id:[0-9]+}",
        projects.file_document,
        name="file_document",
    )

    add(
        "*",
        "/actions/{action}/{item_id}",
        projects.project_action,
        name="actions",
    )

    # chat pages + monitor
    add("GET", "/chat", projects.project_chat, name="project_chat")
    add(
        "GET",
        r"/chat/{chat_id:[a-zA-Z0-9-]+}",
        projects.project_chat,
        name="project_chat_with_id",
    )
    add(
        "GET",
        r"/chat/widget/{code:[a-zA-Z0-9_-]+}",
        projects.public_widget_chat,
        name="public_widget_chat",
    )
    add("*", "/integration", projects.project_integration, name="project_integration")
    add(
        "*",
        r"/integration/{widget_id:[0-9]+}",
        projects.project_widget_edit,
        name="project_widget_edit",
    )
    add("GET", "/chats", project_chats.chats_list, name="project_chats_list")
    add("GET", "/history", project_chats.history_list, name="project_history")
    add(
        "GET",
        r"/history/{chat_id:[a-zA-Z0-9-]+}",
        project_chats.history_detail,
        name="project_history_detail",
    )
    # chat websocket + chat actions
    add("GET", "/ws/notify", user.notify_ws, name="notify_ws")
    add("GET", "/ws/chat/{payload}", chat.websocket, name="chat_ws")
    add(
        "POST",
        "/actions/chat/{action}/{item_id}",
        chat.chat_actions,
        name="chat_actions",
    )

    # static files
    _add_static_build_routes(app)

