from pathlib import Path

from aiohttp import web
from yarl import URL

from .views import (
    admin,
    auth,
    frontend,
    projects,
    chat,
    user,
    support,
)


STATIC_PATH = Path(__file__).parent.parent / "static"
DATA_PATH = Path(__file__).parent.parent / "data"
print(STATIC_PATH)


def setup_routes(app: web.Application) -> None:
    add = app.router.add_route

    # frontend
    add("GET", "/check", frontend.healthcheck)
    add("*", "/", frontend.index, name="index")
    add("*", "/prices", frontend.prices, name="prices")
    add("GET", "/js", frontend.widget_js, name="widget_js")
    add("*", "/robots.txt", frontend.robots_txt)
    add("*", "/favicon.ico", frontend.favicon)

    # auth
    add("*", "/auth/login/", auth.login, name="login")
    add("*", "/auth/logout/", auth.logout, name="logout")
    # registration
    add("*", "/auth/register/", auth.register, name="register")
    add("*", "/auth/confirm/{code}", auth.confirm, name="confirm")
    # password recovery
    add("*", "/auth/resend_code", auth.resend_code, name="resend_code")
    add("*", "/auth/recover/", auth.recover, name="recover")
    add("*", "/auth/reset/{code}", auth.reset, name="reset")
    # settings
    add("*", "/auth/settings/", user.settings, name="settings")
    # add("*", "/settings/email/", auth.change_email, name="settings_email")
    # add("*", "/settings/password/", auth.password, name="settings_password")
    # token
    # add("POST", "/auth/token", auth.get_token)
    # add("POST", "/auth/refresh", auth.refresh_token)

    # admin
    add("GET", "/admin/", admin.dashboard, name="admin_dashboard")
    add("*", "/admin/users/", admin.user_list, name="admin_users")
    add("*", "/admin/login_as/", admin.login_as, name="admin_login_as")

    add(
        "GET",
        "/admin/support/tickets/{ticket_id}",
        support.admin_request_detail,
        name="admin_ticket_detail",
    )

    add(
        "GET",
        "/user/support/tickets",
        support.admin_request_all,
        name="user_tickets",
    )

    add("*", "/project/", projects.index, name="project_list")
    add(
        "*",
        r"/project/{project_id:[a-zA-Z0-9]+}/edit",
        projects.project_edit,
        name="project_edit",
    )
    add(
        "*",
        r"/project/{project_id:[a-zA-Z0-9]+}/data",
        projects.project_view,
        name="project_view",
    )
    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/documents/json",
        projects.project_documents_json,
        name="project_documents_json",
    )
    add(
        "*",
        r"/project/{project_id:[a-zA-Z0-9]+}/topics",
        projects.project_topics,
        name="project_topics",
    )
    add(
        "*",
        r"/project/{project_id:[a-zA-Z0-9]+}/sources",
        projects.project_edit_sources,
        name="project_edit_sources",
    )
    add(
        "*",
        r"/project/{project_id:[a-zA-Z0-9]+}/source/{source_id:[a-zA-Z0-9]+}",
        projects.project_source_edit,
        name="project_source_edit",
    )
    add(
        "*",
        r"/project/{project_id:[a-zA-Z0-9]+}/users",
        projects.project_edit_users,
        name="project_edit_users",
    )
    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/document/{document_id:[a-zA-Z0-9]+}/content",
        projects.project_document_content,
        name="project_document_content",
    )
    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/stats",
        projects.project_stats,
        name="project_stats",
    )
    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/files",
        projects.project_files,
        name="project_files",
    )
    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/files/download/{file_id:[a-zA-Z0-9]+}",
        projects.secure_download,
        name="secure_download",
    )
    add(
        "POST",
        r"/project/{project_id:[a-zA-Z0-9]+}/files/delete/{file_id:[a-zA-Z0-9]+}",
        projects.delete_file,
        name="delete_file",
    )

    add(
        "*",
        "/actions/project/{action}/{item_id}",
        projects.project_action,
        name="project_actions",
    )

    # websockets
    add(
        "GET",
        "/ws/chat/{payload}",
        chat.websocket,
        name="chat_ws",
    )
    add(
        "POST",
        "/actions/chat/{action}/{item_id}",
        chat.chat_actions,
        name="chat_actions",
    )

    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/chat",
        projects.project_chat,
        name="project_chat",
    )
    add(
        "GET",
        r"/chat/widget/{project_id:[a-zA-Z0-9]+}",
        projects.public_project_chat,
        name="public_project_chat",
    )
    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/integration",
        projects.project_integration,
        name="project_integration",
    )
    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/chats",
        projects.chats_list,
        name="project_chats_list",
    )
    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/history",
        projects.history_list,
        name="project_history",
    )
    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/history/{chat_id:[a-zA-Z0-9]+}",
        projects.history_detail,
        name="project_history_detail",
    )
    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/chats/monitor/{chat_id:[a-zA-Z0-9]+}",
        projects.chat_monitor_ws,
        name="project_chat_monitor_ws",
    )
    add(
        "GET",
        r"/project/{project_id:[a-zA-Z0-9]+}/call",
        projects.project_call,
        name="project_call",
    )

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
    # Do not append traling slash if it already added
    if url.parts[-1]:
        url = url / "" if has_trailing_slash else url
    return url.human_repr()
