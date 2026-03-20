import logging
import aiohttp
from aiohttp import web
from aiohttp_session import get_session
from yarl import URL

from vchat.settings import config
from vchat.utils import flash, login_required

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_API_URL = "https://www.googleapis.com/drive/v3"


@login_required()
async def login(request):
    """Initiate Google OAuth2 flow."""
    if not config.get("google_client_id"):
        await flash(request, "Google Drive integration is not configured.", "error")
        return web.HTTPFound(request.headers.get("Referer", "/"))

    session = await get_session(request)

    # Store the project_id if present in query params to redirect back correctly
    project_id = request.query.get("project_id")
    if project_id:
        session["google_auth_project_id"] = project_id

    redirect_uri = request.app.router["google_callback"].url_for()
    # Ensure absolute URL
    redirect_uri = f"{request.scheme}://{request.host}{redirect_uri}"

    params = {
        "client_id": config["google_client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/drive.readonly",
        "access_type": "offline",
        "prompt": "consent",
    }

    auth_url = URL(GOOGLE_AUTH_URL).with_query(params)
    return web.HTTPFound(auth_url)


@login_required()
async def callback(request):
    """Handle Google OAuth2 callback."""
    code = request.query.get("code")
    error = request.query.get("error")

    if error:
        await flash(request, f"Google Auth Error: {error}", "error")
        return web.HTTPFound("/")

    if not code:
        await flash(request, "No code provided", "error")
        return web.HTTPFound("/")

    redirect_uri = request.app.router["google_callback"].url_for()
    redirect_uri = f"{request.scheme}://{request.host}{redirect_uri}"

    data = {
        "code": code,
        "client_id": config["google_client_id"],
        "client_secret": config["google_client_secret"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    async with aiohttp.ClientSession() as client:
        async with client.post(GOOGLE_TOKEN_URL, data=data) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"Failed to get token: {text}")
                await flash(request, "Failed to authenticate with Google", "error")
                return web.HTTPFound("/")

            token_data = await resp.json()

    session = await get_session(request)

    # Store tokens in session (temporarily) or return them to the frontend
    # For this flow, we likely want to store them in the session so the user can then
    # select a folder, and THEN we save the source with the refresh token.

    session["google_access_token"] = token_data.get("access_token")
    session["google_refresh_token"] = token_data.get("refresh_token")

    project_id = session.pop("google_auth_project_id", None)

    await flash(request, "Successfully authenticated with Google Drive", "success")

    if project_id:
        # Redirect back to source creation/edit page
        # We need to pass a flag or something to open the modal or show the folder selector
        target_url = request.app.router["project_edit_sources"].url_for(
            project_id=project_id
        )
        return web.HTTPFound(f"{target_url}?google_auth_success=true")

    return web.HTTPFound("/")


@login_required()
async def list_folders(request):
    """List Google Drive folders."""
    session = await get_session(request)
    access_token = session.get("google_access_token")

    if not access_token:
        return web.json_response({"error": "Not authenticated"}, status=401)

    q = "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    params = {"q": q, "fields": "files(id, name)", "pageSize": 100}

    headers = {"Authorization": f"Bearer {access_token}"}

    async with aiohttp.ClientSession() as client:
        async with client.get(
            f"{GOOGLE_DRIVE_API_URL}/files", params=params, headers=headers
        ) as resp:
            if resp.status != 200:
                return web.json_response(
                    {"error": "Failed to fetch folders"}, status=resp.status
                )

            data = await resp.json()

    return web.json_response({"folders": data.get("files", [])})
