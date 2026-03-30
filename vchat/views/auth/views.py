import asyncio
import logging

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session, new_session
from itsdangerous import (
    BadSignature,
    SignatureExpired,
    URLSafeSerializer,
    URLSafeTimedSerializer,
)
from passlib.hash import pbkdf2_sha512

from vchat.app_keys import CONFIG_KEY
from vchat.i18n import _
from vchat.models import User
from vchat.settings import config
from vchat.utils import (
    DELAY_PROTECTION,
    flash,
    login_required,
    meta,
    register_user,
    sendmessage,
    turnstile_validator,
)

from . import forms

backend_db_uri = config["database_uri"]

serializer = URLSafeTimedSerializer(config["secret_key"])

__all__ = [
    "login",
    "logout",
    "register",
    "confirm",
    "recover",
    "resend_code",
    "reset",
]


@meta(title=_("Login to vchat"))
@aiohttp_jinja2.template("auth/login.html")
async def login(request):
    session = await get_session(request)
    data = await request.post()
    form = forms.LoginForm(data, meta={"csrf_context": session})

    if request.method == "POST" and form.validate():
        record = await request["db"].execute(
            sa.select(User).where(User.email == form.email.data.lower())
        )
        user = record.scalar()
        if not user:
            form.email.errors.append(_("Email or password is incorrect"))
            return {"form": form}
        if user.is_active is False:
            form.email.errors.append(
                _(
                    "You have not confirmed your email. Check your email and Spam "
                    "folder for the activation link, then try again."
                )
            )
            return {"form": form}
        if not pbkdf2_sha512.verify(form.password.data, user.password):
            # Delay brute force
            await asyncio.sleep(DELAY_PROTECTION)
            form.email.errors.append(_("Wrong email or password"))
            return {"form": form}
        if not await turnstile_validator(
            code=data.get("cf-turnstile-response", False), ip=request.remote
        ):
            form.email.errors.append(_("CAPTCHA error"))
            return {"form": form}

        # Save to aoihttp session
        # Warning
        # Always use new_session() instead of get_session() in your login views
        # to guard against Session Fixation attacks!

        session = await new_session(request)
        session["staff_id"] = user.id
        session["role"] = user.role.value

        target = "dashboard"
        if request.rel_url.query.get("next"):
            target = request.rel_url.query["next"]
        return web.HTTPFound(location=request.app.router[target].url_for())

    return {"form": form}


@meta(title=_("Logout from vchat"))
@login_required()
async def logout(request):
    session = await get_session(request)
    session.invalidate()
    return web.HTTPFound(location=request.app.router["login"].url_for())


@meta(title=_("Register on vchat"))
@aiohttp_jinja2.template("auth/register.html")
async def register(request):
    session = await get_session(request)
    data = await request.post()
    form = forms.RegisterForm(data, meta={"csrf_context": session})

    if request.method == "POST" and form.validate():
        record = await request["db"].execute(
            sa.select(User).where(User.email == form.email.data.lower())
        )
        user = record.first()
        if user:
            form.email.errors.append(
                _("This email is already in use, log in to the site using your email")
            )
            return {"form": form}
        if not await turnstile_validator(
            code=data.get("cf-turnstile-response", False), ip=request.remote
        ):
            form.email.errors.append(_("CAPTCHA error"))
            return {"form": form}

        await register_user(
            request=request,
            name=form.name.data,
            email=form.email.data.lower(),
            encrypted_password=pbkdf2_sha512.encrypt(form.password.data),
            active=False,
        )
        return {"done": True, "form": form}

    return {"form": form}


@meta(title=_("Confirm registration"))
@aiohttp_jinja2.template("auth/confirm.html")
async def confirm(request):
    code = request.match_info["code"]
    # we allow infinite time for activation
    s = URLSafeSerializer(config["secret_key"])
    try:
        email = s.loads(code)
    except (ValueError, BadSignature, SignatureExpired) as e:
        logging.error("Error activation code %s: %s", code, e)
        return {"done": False}
    except Exception as e:
        logging.error("Unexpected error with activation code %s: %s", code, e)
        return {"done": False}

    record = await request["db"].scalar(
        sa.select(User).where(User.email == email.lower())
    )
    if not record:
        return {"done": False}
    logging.info("Activate email: %s", email)
    await request["db"].execute(
        sa.update(User).values(is_active=True).where(User.email == email.lower())
    )
    await request["db"].commit()

    session = await get_session(request)
    session["staff_id"] = record.id
    session["role"] = record.role.value
    return {"done": True}


@meta(title=_("Recover password"))
@aiohttp_jinja2.template("auth/recover.html")
async def recover(request):
    data = await request.post()
    session = await get_session(request)
    form = forms.RecoverForm(data, meta={"csrf_context": session})
    if request.method == "POST" and form.validate():
        record = await request["db"].scalar(
            sa.select(User).where(User.email == form.email.data.lower())
        )
        if not record:
            form.email.errors.append(_("User not found"))
            return {"form": form}
        if not await turnstile_validator(
            code=data.get("cf-turnstile-response", False), ip=request.remote
        ):
            form.email.errors.append(_("CAPTCHA error"))
            return {"form": form}

        to = form.email.data.lower()
        context = {
            "url": f"https://{request.host}"
            + str(
                request.app.router["reset"].url_for(
                    code=URLSafeTimedSerializer(
                        request.app[CONFIG_KEY]["secret_key"]
                    ).dumps(record.id)
                )
            )
        }
        await sendmessage(
            to=to,
            subject=_("Password reset"),
            template="mail/password-reset.html",
            request=request,
            context=context,
        )
        return {"done": True}

    return {"form": form}


@meta(title=_("Resend registration code"))
@aiohttp_jinja2.template("auth/resend_code.html")
async def resend_code(request):
    data = await request.post()
    session = await get_session(request)
    form = forms.RecoverForm(data, meta={"csrf_context": session})
    if request.rel_url.query.get("email", ""):
        form.email.data = request.rel_url.query["email"]

    if request.method == "POST" and form.validate():
        record = await request["db"].execute(
            sa.select(User).where(User.email == form.email.data.lower())
        )
        user = record.scalar()
        if not user:
            form.email.errors.append(_("User not found"))
            return {"form": form}
        if user.is_active is True:
            form.email.errors.append(
                _("Email already confirmed. Log in to the site or recover password")
            )
            return {"form": form}

        to = form.email.data.lower()
        context = {
            "url": f"https://{request.host}"
            + str(
                request.app.router["confirm"].url_for(
                    code=URLSafeSerializer(config["secret_key"]).dumps(to)
                )
            )
        }
        await sendmessage(
            to=to,
            subject=_("Repeat registration confirmation"),
            template="mail/register-email.html",
            request=request,
            context=context,
        )
        return {"done": True}

    return {"form": form}


@meta(title=_("Reset password"))
@aiohttp_jinja2.template("auth/reset.html")
async def reset(request):
    code = request.match_info["code"]
    s = URLSafeTimedSerializer(request.app[CONFIG_KEY]["secret_key"])

    data = await request.post()
    session = await get_session(request)
    form = forms.ResetForm(data, meta={"csrf_context": session})

    try:
        user_id = int(s.loads(code, max_age=60 * 60 * 4))  # 4 hours seconds
    except SignatureExpired:
        logging.error("Activation code expired for user %s", s.loads_unsafe(code)[1])
        msg = _("Activation code expired")
        return {"form": form, "code": code, "done": False, "msg": msg}
    except BadSignature as ex:
        logging.error("Error activation code %s: %s", code, ex)
        msg = _("Activation code error, seems broken")
        return {"form": form, "code": code, "done": False, "msg": msg}
    except Exception as ex:
        logging.error("Unknown error, activation code %s: %s", code, ex)
        msg = _("Unknown error")
        return {"form": form, "code": code, "done": False, "msg": msg}

    record = record = await request["db"].execute(
        sa.select(User).where(User.id == user_id)
    )
    user = record.first()
    if not user:
        msg = _("User not found")
        return {"code": code, "form": form, "msg": msg}

    if request.method == "POST" and form.validate():
        await request["db"].execute(
            sa.update(User)
            .values(password=pbkdf2_sha512.encrypt(form.password.data))
            .where(User.id == user_id)
        )
        await request["db"].commit()
        await flash(request, _("Password changed"))

        return {"code": code, "form": form, "done": True}

    return {"code": code, "form": form}


# @login_required()
# @meta(title="Сменить пароль")
# @aiohttp_jinja2.template("auth/password.html")
# async def password(request):
#     session = await get_session(request)
#     data = await request.post()
#     user = request["user"]
#     form = forms.PasswordForm(data, meta={"csrf_context": session})
#     if request.method == "POST" and form.validate():
#         if not await check_pass(form.password_old.data, user.password):
#             form.password_old.errors.append("Пароль не совпадает с текущим")
#             return {"form": form}

#         record = await User.get(request["user"].id)
#         await record.update(password=pbkdf2_sha512.encrypt(form.password.data)).apply()
#         await sendmessage(
#             to=user.email,
#             subject="Вы успешно изменили пароль",
#             template="mail/confirm-password.html",
#             request=request,
#             context={"email": user.email},
#         )
#         await notify(request, "Настройки сохранены")
#         return web.HTTPFound(request.app.router["settings_password"].url_for())

#     return {"form": form}


# @login_required()
# @meta(title="Сменить email")
# @aiohttp_jinja2.template("auth/email.html")
# async def change_email(request):
#     session = await get_session(request)
#     data = await request.post()
#     user = request["user"]
#     form = forms.UpdateEmailForm(data, meta={"csrf_context": session})
#     if request.method == "POST" and form.validate():
#         if not await check_pass(form.password.data, user.password):
#             form.password_old.errors.append("Пароль не совпадает с текущим")
#             return {"form": form}

#         if form.email.data == user.email:
#             form.email.errors.append("Введите адрес почты отличающийся от текущего")
#             return {"form": form}

#         email_exist = await User.query.where(
#             User.email == form.email.data.lower()
#         ).gino.first()
#         if email_exist:
#             form.email.errors.append(
#                 "Этот email уже используется на сайте. "
#                 "Вы можете воспользоваться им для <a href='/login/'>входа</a> на сайт. "
#                 "Если Вы забыли пароль, то можете его <a href='/recover/'>восстановить</a>."
#             )
#             return {"form": form}

#         record = await User.get(request["user"].id)
#         await record.update(email=form.email.data).apply()
#         await sendmessage(
#             to=[user.email, form.email.data],
#             subject="📨 Почта изменена",
#             template="mail/confirm-email.html",
#             request=request,
#             context={"email": user.email, "email_new": form.email.data},
#         )
#         await notify(
#             request, "Настройки сохранены, вам направлено письмо с уведомлением."
#         )
#         return web.HTTPFound(request.app.router["settings_email"].url_for())

#     return {"form": form}


# async def oauth(request):
#     provider = request.match_info.get("provider")
#     if provider not in clients:
#         raise web.HTTPNotFound(reason="Unknown provider")

#     # Create OAuth1/2 client
#     Client = clients[provider]["class"]
#     params = clients[provider]["init"]
#     client = Client(**params)
#     client.params[
#         "oauth_callback" if issubclass(Client, OAuth1Client) else "redirect_uri"
#     ] = f"http://{request.host}{request.path}"

#     # Check if is not redirect from provider
#     if client.shared_key not in request.query:
#         # For oauth1 we need more work
#         if isinstance(client, OAuth1Client):
#             token, secret, _ = await client.get_request_token()

#             # Dirty save a token_secret
#             # Dont do it in production
#             request.app.secret = secret
#             request.app.token = token

#         # Redirect client to provider
#         return web.HTTPFound(client.get_authorize_url(access_type="offline"))

#     # For oauth1 we need more work
#     if isinstance(client, OAuth1Client):
#         client.oauth_token_secret = request.app.secret
#         client.oauth_token = request.app.token

#     if provider == "twitter:":
#         r = await client.get_access_token(request.query, include_email="true")
#     else:
#         r = await client.get_access_token(request.query)
#     *_, meta = r
#     user, info = await client.user_info()
#     emails = []
#     if provider == "github":
#         emails = await client.request("GET", "/user/emails")
#         print(emails)

#     # async with request.app['db'].acquire() as conn:
#     #     user_object = await check_user(conn, user=user)

#     text = f"""
#         <a href='/'>back</a><br/><br/>
#         <ul>
#         <li>ID: {user.id}</li>
#         <li>Username: {user.username}</li>
#         <li>First, last name: {user.first_name}, {user.last_name}</li>
#         <li>Gender: {user.gender}</li>
#         <li>Email: {user.email}</li>
#         <li>Link: {user.link}</li>
#         <li>Picture: {user.picture}</li>
#         <li>Country, city: {user.country}, {user.city}</li>
#         </ul>
#         <p>{info}</p>
#         <p>{meta}</p>
#         <p>{emails}</p>
#     """
#     return web.Response(text=text, content_type="text/html")


# async def get_token(request):
#     """Fast and easy OAuth2 implementation"""
#     data = await request.post()
#     scope = data.get("scope", "").split(" ")
#     # Поле username содержит email
#     record = await User.query.where(User.email == data.get("username", "")).gino.first()
#     if not record or not await check_pass(data.get("password"), record.password):
#         return web.json_response({"error": "Wrong login/password "}, status=403)

#     scope_str, refresh, token = create_tokens(
#         request.app[CONFIG_KEY]["secret_key"], record, scope=scope
#     )
#     resp = {
#         "access_token": token,
#         "token_type": "bearer",
#         "expires_in": 3600,
#         "refresh_token": refresh,
#         "scope": scope_str,
#     }
#     return web.json_response(resp, status=200)


# async def refresh_token(request):
#     # Код для рефреша токена для #212
#     return web.json_response({"error": "Not implemented"}, status=200)
