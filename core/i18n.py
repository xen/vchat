from contextvars import ContextVar
from pathlib import Path

from aiohttp import web
from babel.support import LazyProxy, Translations

from .settings import config
from .app_keys import CONFIG_KEY

# Context variable to store the current translations (for the active request)
current_translations: ContextVar[Translations] = ContextVar("current_translations")

# pre load all language translations
ALL_LOCALES = {
    lang: Translations.load(Path(__file__).parent / "translations", [lang])
    for lang in config["lang_supported"].keys()
}


@web.middleware
async def i18n_middleware(request, handler):
    # Skip static files
    if request.path.startswith("/static/"):
        return await handler(request)

    lang_supported = config.get("lang_supported", "en")
    lang = None

    if request.get("user"):
        lang = request["user"].language

    # Fall back to the 'language' cookie if not set by the user
    if not lang:
        lang = request.cookies.get("language")
        if lang not in lang_supported:
            lang = None  # Reset if not supported

    # Fall back to 'Accept-Language' header
    if not lang:
        accept_language = request.headers.get("Accept-Language", "")
        for part in accept_language.split(","):
            lang_candidate = part.split(";")[0].strip()
            if lang_candidate in lang_supported:
                lang = lang_candidate
                break

    if not lang:
        lang = config.get("lang_default", "en")

    # Load and store the resolved translations
    translations = ALL_LOCALES[lang]
    current_translations.set(translations)

    # Attach the translations and language to the request
    request["i18n"] = translations
    request["lang"] = lang

    # Process the request
    response = await handler(request)
    return response


async def jinja_context_processor(request) -> dict:
    return {
        "lang_supported": request.app[CONFIG_KEY]["lang_supported"],
        "gettext": gettext,
        "ngettext": ngettext,
        "pgettext": pgettext,
        "npgettext": npgettext,
        "_": gettext,
    }


# Get the current translations from the context
def get_current_translations() -> Translations:
    return current_translations.get(None)


# gettext implementation
def gettext(message: str, *args, **kwargs) -> str:
    """
    Translate a message using the current active translations.
    """
    translations = get_current_translations()
    if not translations:
        return message  # Fallback: return the message as-is
    return translations.gettext(message, *args, **kwargs)


_ = gettext


# ngettext for plural forms
def ngettext(singular: str, plural: str, n: int, *args, **kwargs) -> str:
    """
    Translate a plural message.
    """
    translations = get_current_translations()
    if not translations:
        return singular if n == 1 else plural
    return translations.ngettext(singular, plural, n, *args, **kwargs)


# pgettext implementation
def pgettext(context: str, message: str, *args, **kwargs) -> str:
    """
    Translate a message with context.
    """
    translations = get_current_translations()
    if not translations:
        return message
    return translations.pgettext(context, message, *args, **kwargs)


# npgettext implementation
def npgettext(context: str, singular: str, plural: str, n: int, *args, **kwargs) -> str:
    """
    Translate a plural message with context.
    """
    translations = get_current_translations()
    if not translations:
        return singular if n == 1 else plural
    return translations.npgettext(context, singular, plural, n, *args, **kwargs)


def lazy_gettext(*args, **kwargs) -> LazyProxy:
    return LazyProxy(gettext, *args, **kwargs)


def lazy_ngettext(*args, **kwargs) -> LazyProxy:
    return LazyProxy(ngettext, *args, **kwargs)


def lazy_pgettext(*args, **kwargs) -> LazyProxy:
    return LazyProxy(pgettext, *args, **kwargs)


def lazy_npgettext(*args, **kwargs) -> LazyProxy:
    return LazyProxy(npgettext, *args, **kwargs)
