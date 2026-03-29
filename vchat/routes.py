from pathlib import Path

from aiohttp import web
from yarl import URL

from .metrics import metrics_handler
from .views import admin, api, auth, chat, frontend, projects, support, user


STATIC_PATH = Path(__file__).parent.parent / "static"
DATA_PATH = Path(__file__).parent.parent / "data"


def setup_routes(app: web.Application) -> None:
    add = app.router.add_route

    # observability
    add("GET", "/metrics", metrics_handler, name="metrics")

    # frontend
    add("GET", "/check", frontend.healthcheck)
    add("GET", "/home", projects.project_view, name="index")
    add("GET", "/widget", frontend.widget_js, name="widget")
    add("GET", "/js", frontend.widget_js, name="widget_js")
    add("*", "/robots.txt", frontend.robots_txt)
    add("*", "/favicon.ico", frontend.favicon)

    # public api
    add("GET", "/api/update", api.update_document, name="api_update")
    add(
        "POST",
        "/api/support/request",
        api.create_support_request,
        name="api_support_request",
    )

    # auth
    add("*", "/auth/login/", auth.login, name="login")
    add("*", "/auth/register/", auth.login, name="register")
    add("*", "/auth/logout/", auth.logout, name="logout")
    add("*", "/auth/settings/", user.settings, name="settings")
    add("*", "/auth/settings/password/", user.settings, name="settings_password")

    # admin
    add("GET", "/admin/", admin.dashboard, name="admin_dashboard")
    add("*", "/admin/users/", admin.user_list, name="admin_users")
    add("GET", "/admin/login-as/", admin.login_as, name="admin_login_as")

    # support
    add(
        "GET",
        "/requests/{ticket_id}",
        support.admin_request_detail,
        name="admin_ticket_detail",
    )
    add("GET", "/requests/", support.admin_request_all, name="user_tickets")

    # single-project admin pages
    add("GET", "/", projects.project_view, name="project_view")
    add("GET", "/dashboard", projects.project_view, name="dashboard")
    add("*", "/settings/project", projects.project_edit, name="project_edit")
    add("GET", "/documents/json", projects.project_documents_json, name="project_documents_json")
    add("*", "/topics", projects.project_topics, name="project_topics")
    add("*", "/sources", projects.project_edit_sources, name="project_edit_sources")
    add("*", r"/source/{source_id:[0-9]+}", projects.project_source_edit, name="project_source_edit")
    add("GET", r"/document/{document_id:[0-9]+}/content", projects.project_document_content, name="project_document_content")
    add("GET", "/stats", projects.project_stats, name="project_stats")
    add("GET", "/files", projects.project_files, name="project_files")
    add("GET", r"/files/download/{file_id:[0-9]+}", projects.secure_download, name="secure_download")
    add("POST", r"/files/delete/{file_id:[0-9]+}", projects.delete_file, name="delete_file")
    add("*", "/actions/project/{action}/{item_id}", projects.project_action, name="project_actions")

    # chat pages + monitor
    add("GET", "/chat", projects.project_chat, name="project_chat")
    add("GET", r"/chat/{chat_id:[a-zA-Z0-9-]+}", projects.project_chat, name="project_chat_with_id")
    add("GET", "/chat/widget", projects.public_widget_chat, name="public_widget_chat")
    add("GET", "/integration", projects.project_integration, name="project_integration")
    add("GET", "/chats", projects.chats_list, name="project_chats_list")
    add("GET", "/history", projects.history_list, name="project_history")
    add("GET", r"/history/{chat_id:[a-zA-Z0-9-]+}", projects.history_detail, name="project_history_detail")
    add("GET", r"/chats/monitor/{chat_id:[a-zA-Z0-9-]+}", projects.chat_monitor_ws, name="project_chat_monitor_ws")

    # chat websocket + chat actions
    add("GET", "/ws/chat/{payload}", chat.websocket, name="chat_ws")
    add("POST", "/actions/chat/{action}/{item_id}", chat.chat_actions, name="chat_actions")

    # static files
    app.router.add_static(
        "/static/",
        path=STATIC_PATH.absolute(),
        name="static",
        follow_symlinks=True,
    )
    app.router.add_static(
        "/data/",
        path=DATA_PATH.absolute(),
        name="data",
        follow_symlinks=True,
    )


def to_path(url: URL, *, has_trailing_slash: bool = True) -> str:
    """Convert URL instance into string path, suitable for aiohttp.web router.

    When `has_trailing_slash` is `True` - append trailing slash for URL, if it not
    already appended.
    """
    if url.parts[-1]:
        url = url / "" if has_trailing_slash else url
    return url.human_repr()
