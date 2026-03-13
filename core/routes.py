from pathlib import Path

from aiohttp import web
from yarl import URL

from .views import (
    admin,
    auth,
    frontend,
    projects,
    chat,
    blog,
    billing,
    notifications,
    user,
    support,
)
from .views.auth import google
from .views.chat import call


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
    add("*", "/blog/", blog.index_page, name="blog_index")
    add(
        "*",
        "/blog/category/{category_name}",
        blog.category_page,
        name="blog_category_main",
    )
    add("*", "/blog/{post_id}_{post_slug}", blog.postpage, name="blog_page")

    # i18n
    add(
        "GET",
        "/set_lang/{lang}",
        frontend.set_language,
        name="set_language",
    )
    # aux pages
    add("GET", "/about/", frontend.page, name="page_index")
    add("GET", "/about/{page}", frontend.page, name="page")

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
    add("*", "/auth/billing/", user.billing, name="billing")
    add("*", "/auth/projects/", user.user_projects, name="user-projects")
    add("*", "/messages/", user.messages, name="messages")
    add("*", "/actions/user/{action}", user.user_actions, name="user_actions")
    # add("*", "/settings/email/", auth.change_email, name="settings_email")
    # add("*", "/settings/password/", auth.password, name="settings_password")
    # token
    # add("POST", "/auth/token", auth.get_token)
    # add("POST", "/auth/refresh", auth.refresh_token)

    # google auth
    add("GET", "/auth/google/login", google.login, name="google_login")
    add("GET", "/auth/google/callback", google.callback, name="google_callback")
    add("GET", "/api/google/folders", google.list_folders, name="google_list_folders")

    # dashboard
    add("*", "/project/", frontend.projects, name="dashboard")

    # admin
    add("GET", "/admin/", admin.dashboard, name="admin_dashboard")
    add("*", "/admin/users/", admin.user_list, name="admin_users")
    add("*", "/admin/login_as/", admin.login_as, name="admin_login_as")

    # admin blog
    add("*", "/admin/blog/posts", blog.post_list_admin, name="edit_post_list")
    add("*", "/admin/blog/posts/{post_id}", blog.post_edit_admin, name="edit_post_edit")
    add(
        "*",
        "/admin/blog/categories",
        blog.category_list_admin,
        name="edit_category_list",
    )
    add(
        "*",
        "/admin/blog/categories/{category_id}",
        blog.category_edit_admin,
        name="edit_category_edit",
    )

    # support
    add("GET", "/admin/support/chapters", support.admin_chapters, name="admin_chapters")
    add("GET", "/admin/support/pages", support.admin_pages, name="admin_pages")
    add(
        "GET",
        "/admin/support/pages/edit",
        support.admin_page_edit,
        name="admin_page_edit",
    )
    add(
        "GET",
        "/admin/support/favorites",
        support.admin_favorites,
        name="admin_favorites",
    )
    add("GET", "/admin/support/tickets", support.admin_tickets, name="admin_tickets")
    add(
        "GET",
        "/admin/support/tickets/{ticket_id}",
        support.admin_ticket_detail,
        name="admin_ticket_detail",
    )

    add("GET", "/user/support/tickets", support.user_tickets, name="user_tickets")
    add(
        "GET",
        "/user/support/tickets/{ticket_id}",
        support.user_ticket_detail,
        name="user_ticket_detail",
    )
    add(
        "POST",
        "/actions/support/{action}",
        support.support_actions,
        name="support_actions",
    )
    add(
        "GET",
        "/api/support/chapters",
        support.get_chapters_api,
        name="get_chapters_api",
    )

    # Public Support
    add("GET", "/support/", support.support_root, name="support_root")
    add("GET", "/{lang}/support", support.public_support, name="public_support")
    add("GET", "/{lang}/support/{slug}", support.public_article, name="public_article")

    # projects
    add(
        "*",
        "/project/onboarding/",
        projects.project_onboarding,
        name="project_onboarding",
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

    # billing
    add(
        "GET",
        r"/project/{short_id:[a-zA-Z0-9]+}/billing",
        billing.billing_page,
        name="billing_page",
    )
    add(
        "POST",
        r"/project/{short_id:[a-zA-Z0-9]+}/billing/upgrade",
        billing.upgrade_plan,
        name="billing_upgrade",
    )
    add(
        "POST",
        r"/project/{short_id:[a-zA-Z0-9]+}/billing/downgrade",
        billing.downgrade_plan,
        name="billing_downgrade",
    )
    add(
        "GET",
        r"/project/{short_id:[a-zA-Z0-9]+}/billing/result",
        billing.billing_result,
        name="billing_result",
    )
    add("POST", "/webhook/stripe", billing.stripe_webhook, name="stripe_webhook")

    add(
        "*",
        "/actions/project/{action}/{item_id}",
        projects.project_action,
        name="project_actions",
    )

    add(
        "*",
        "/actions/blog/{action}/{item_id}",
        blog.blog_actions,
        name="blog_actions",
    )

    # websockets
    add("GET", "/ws/notify", notifications.websocket_handler, name="notify")
    add(
        "GET",
        "/ws/chat/{payload}",
        chat.websocket,
        name="chat_ws",
    )
    add(
        "GET",
        "/ws/call/{project_id}",
        call.call_websocket_handler,
        name="call_ws",
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
