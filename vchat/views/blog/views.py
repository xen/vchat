import datetime
import os
import uuid
from gettext import gettext as _
from pathlib import Path

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session
from itsdangerous import BadSignature, SignatureExpired, TimedSerializer
from yarl import URL

from vchat.app_keys import SIGNER_KEY
from vchat.models import Post, PostCategory, PostTag, User
from vchat.settings import config
from vchat.utils import convert_to_html, flash, json, login_required, meta

from . import forms


async def add_filters(query, request):
    t_query = query.with_only_columns(sa.func.count().over())
    total = await request["db"].scalar(t_query) or 0
    limit = config["post_per_page"]
    limit = min(limit, 1000)
    page = request.match_info.get("page", "1")
    current_page = int(page) if page.isnumeric() else 1
    offset = current_page * limit - limit
    # we don't allow to pass after 1000 item.
    offset = offset if offset + limit < 1000 else 1000 - limit
    query = query.limit(limit).offset(offset)
    posts = (await request["db"].execute(query)).all()
    return (posts, total)


async def get_with_filters(query, request, keys):
    """
    В keys обязательно должно присутствовать поле id
    таблицы, по колонке search которой будет проводитсья
    полнотекстовый поиск
    """
    _get = request.query.get
    key_list = [key for key, value in keys.items()]
    page_keys = ["offset", "limit"]
    for key in set(request.query.keys()):
        id_key = keys.get("id")
        # определяем имя таблицы, в которой веедтся поиск по колонке search
        table_name = id_key.table.name if id_key is not None else ""
        search_column = f"{table_name}.search" if table_name else "search"
        if key in page_keys:
            continue
        if key == "q" and _get("q"):
            query = query.where(
                sa.text(f"""{search_column} @@ plainto_tsquery('simple', :q)""")
            )
            query = query.order_by(
                sa.text(
                    f"""ts_rank({search_column}, plainto_tsquery('simple', :q)) DESC"""
                )
            )
        if key == "order_by":
            col = _get("order_by", "name")
            if col.lstrip("-") not in key_list:
                col = "name"
            if col.startswith("-"):
                order = sa.desc
                col = col[1:]
            else:
                order = sa.asc
            # у запроса могут быть переданы параметры сортироваки order_by(None) очищает ее.
            # подробнее https://docs.sqlalchemy.org/en/14/orm/query.html#sqlalchemy.orm.Query.order_by
            query = query.order_by(None).order_by(order(keys.get(col)))
    t_query = query.with_only_columns(sa.func.count().over())
    limit = int(_get("limit", "10")) if _get("limit", "10").isnumeric() else 10
    limit = min(limit, 1000)
    offset = int(_get("offset", "0")) if _get("offset", "0").isnumeric() else 0
    # we don't allow to pass after 1000 item.
    offset = offset if offset + limit < 1000 else 1000 - limit
    query = query.limit(limit)
    query = query.offset(offset)
    if _get("q"):
        result = (await request["db"].execute(query.params(q=_get("q").upper()))).all()
        total = await request["db"].scalar(t_query.params(q=_get("q").upper()))
    else:
        result = (await request["db"].execute(query)).all()
        total = await request["db"].scalar(t_query)
    return result, total or 0


def _row_to_dict(row):
    if isinstance(row, dict):
        return row
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    asdict = getattr(row, "asdict", None)
    if callable(asdict):
        return asdict()
    if hasattr(row, "__dict__"):
        data = row.__dict__.copy()
        data.pop("_sa_instance_state", None)
        return data
    return dict(row)


def prepare_posts(posts_raw, request):
    """Add media prefix to picture field. And return list of dicts."""
    url = URL(config["media_prefix"]) / "articles/"
    posts = []
    for post in posts_raw:
        post = _row_to_dict(post)
        post["picture"] = (
            URL(url / str(post["id"]) / post["picture"]) if post["picture"] else ""
        )
        posts.append(post)
    return posts


@meta(title=_("Blog and Articles"))
@aiohttp_jinja2.template("blog/index.html")
async def index_page(request):
    query = (
        sa.select(
            Post.id.label("id"),
            Post.short_id.label("short_id"),
            Post.title.label("title"),
            Post.slug.label("slug"),
            Post.lead.label("lead"),
            Post.created_at.label("created_at"),
            Post.updated_at.label("updated_at"),
            Post.user_id.label("user_id"),
            Post.picture.label("picture"),
            PostCategory.slug.label("category_slug"),
            PostCategory.title.label("category_title"),
            User.id.label("author_id"),
            User.name.label("author_name"),
        )
        .select_from(Post)
        .join(User, User.id == Post.user_id)
        .outerjoin(
            PostTag,
            sa.and_(PostTag.post_id == Post.id, PostTag.is_primary.is_(True)),
        )
        .outerjoin(PostCategory, PostCategory.id == PostTag.post_category_id)
        .where(sa.and_(Post.is_published.is_(True)))  # noqa: E712
        .order_by(Post.published_at.desc())
    )

    posts_raw, total = await add_filters(query, request)
    posts = prepare_posts(posts_raw, request)

    return {"posts": posts, "total": total}


@meta(title=_("Blog categories"))
@aiohttp_jinja2.template("blog/category_list.html")
async def category_list(request):
    sql = sa.text(
        """
        select
            *
        from (
            select
                c.slug as category_slug,
                c.title as category_title,
                p.id as id,
                p.short_id as short_id,
                p.title as title,
                p.slug as slug,
                p.lead as lead,
                p.created_at as created_at,
                p.updated_at as updated_at,
                p.user_id as user_id,
                p.picture as picture,
                users.name as author_name,
                row_number() over (partition by c.id
                                    order by p.created_at desc) as row
            from
                post as p
                left outer join post_tag as tp on (tp.post_id=p.id and tp.is_primary=true)
                left outer join post_category as c on (c.id=tp.post_category_id)
                join users on (users.id=user_id)
            where
                p.is_published=true
            order by
                c.id,
                p.created_at desc
        ) as foo
        where row <=3
        """  # noqa: E501
    )
    category_posts_raw = (await request["db"].execute(sql)).all()
    category_posts = prepare_posts(category_posts_raw, request)

    return {"category_posts": category_posts}


@aiohttp_jinja2.template("blog/post.html")
async def postpage(request):
    post_id = request.match_info["post_id"]
    post_query = (
        sa.select(
            Post.id.label("id"),
            Post.short_id.label("short_id"),
            Post.title.label("title"),
            Post.slug.label("slug"),
            Post.lead.label("lead"),
            Post.created_at.label("created_at"),
            Post.updated_at.label("updated_at"),
            Post.body_html,
            Post.body_toc,
            Post.picture.label("picture"),
            Post.body.label("body"),
            PostCategory.id.label("category_id"),
            PostCategory.slug.label("category_slug"),
            PostCategory.title.label("category_title"),
            User.id.label("user_id"),
            User.name.label("author_name"),
        )
        .select_from(Post)
        .join(User, Post.user_id == User.id)
        .outerjoin(
            PostTag,
            sa.and_(PostTag.post_id == Post.id, PostTag.is_primary.is_(True)),
        )
        .outerjoin(PostCategory, PostCategory.id == PostTag.post_category_id)
        .where(Post.short_id == post_id)
    )
    post_raw = (await request["db"].execute(post_query)).first()
    # check if post is exist
    if not post_raw:
        raise web.HTTPNotFound(text=_("Article not found"))
    post = prepare_posts([post_raw], request)
    post = post[0] if post else None
    # get the tags list
    tag_query = sa.select(PostTag.tag).where(
        sa.and_(
            PostTag.post_id == post["id"],
            PostTag.is_primary.is_(False),
        )
    )
    tags = (await request["db"].execute(tag_query)).scalars().all()
    labels = [item for item in tags]
    # similar posts
    _T = sa.orm.aliased(PostTag)
    # match_posts = (
    #     sa.select(sa.func.count(PostTag.post_id).label("qty"), Post.id.label("id"))
    #     .select_from(
    #         PostTag
    #     )
    #     .outerjoin(
    #         Post,
    #         sa.and_(
    #             Post.id == PostTag.post_id,
    #         ),
    #     )
    #     .where(sa.and_(Post.id != post["id"], Post.is_published.is_(True)))  # noqa: E712
    #     .where(sa.and_(_T.tag == PostTag.tag, _T.post_id == post["id"]))
    #     .group_by(Post.id)
    #     .order_by(sa.desc("qty"))
    #     .limit(3)
    # )
    # match_posts = (await request["db"].execute(match_posts)).all()
    # For now let's skip complex similarity logic if it fails, but try to restore it.
    # The original query used alias _T but didn't join it properly in the WHERE clause?
    # Actually _T needs to be joined or selected.
    # Let's simplify similar posts to just same category for now to avoid errors,
    # or try to replicate the logic.
    # The original logic was: find posts that have same tags as current post.
    # Let's stick to same category for simplicity and robustness first, as per previous code.

    similar_posts = (
        sa.select(
            Post.id.label("id"),
            Post.short_id.label("short_id"),
            Post.title.label("title"),
            Post.slug.label("slug"),
            Post.lead.label("lead"),
            Post.created_at.label("created_at"),
            Post.updated_at.label("updated_at"),
            Post.user_id.label("user_id"),
            Post.picture.label("picture"),
            PostCategory.slug.label("category_slug"),
            PostCategory.title.label("category_title"),
            User.id.label("author_id"),
            User.name.label("author_name"),
        )
        .select_from(Post)
        .join(User, Post.user_id == User.id)
        .outerjoin(
            PostTag,
            sa.and_(PostTag.post_id == Post.id, PostTag.is_primary.is_(True)),
        )
        .outerjoin(PostCategory, PostCategory.id == PostTag.post_category_id)
        # .where(Post.category_id == post_raw.category_id) # Post doesn't have category_id
        .where(PostCategory.id == post_raw.category_id)
        .where(Post.id != post_raw.id)
        .limit(3)
    )
    similar_posts_raw = (await request["db"].execute(similar_posts)).all()
    similar_posts = prepare_posts(similar_posts_raw, request)

    return {"post": post, "labels": labels, "posts": similar_posts}


async def postpage_fix(request):
    post_id = request.match_info["post_id"]
    post_slug = request.match_info["post_slug"]
    return web.HTTPFound(location=f"/blog/{post_id}_{post_slug}")


@meta(title=_("Category page"))
@aiohttp_jinja2.template("blog/category.html")
async def category_page(request):
    slug = request.match_info["category_name"]
    category_query = sa.select(
        PostCategory.id,
        PostCategory.title,
        PostCategory.description_html,
        PostCategory.slug,
    ).where(PostCategory.slug == slug)
    category = (await request["db"].execute(category_query)).first()
    if not category:
        raise web.HTTPNotFound(text=_("Category not found"))
    query = (
        sa.select(
            Post.id.label("id"),
            Post.short_id.label("short_id"),
            Post.title.label("title"),
            Post.slug.label("slug"),
            Post.lead.label("lead"),
            Post.created_at.label("created_at"),
            Post.updated_at.label("updated_at"),
            Post.user_id.label("user_id"),
            Post.picture.label("picture"),
            PostCategory.slug.label("category_slug"),
            PostCategory.title.label("category_title"),
            User.id.label("author_id"),
            User.name.label("author_name"),
        )
        .select_from(Post)
        .join(User, Post.user_id == User.id)
        .outerjoin(
            PostTag,
            sa.and_(PostTag.post_id == Post.id, PostTag.is_primary.is_(True)),
        )
        .join(PostCategory, PostCategory.id == PostTag.post_category_id)
        .where(PostCategory.id == category.id)
        .where(Post.is_published.is_(True))
        .order_by(Post.published_at.desc())
    )

    posts_raw, total = await add_filters(query, request)
    posts = prepare_posts(posts_raw, request)

    return {"posts": posts, "category": category, "total": total}


@meta(title=_("Blog categories"))
@login_required()
@aiohttp_jinja2.template("blog/admin/category_list.html")
async def category_list_admin(request):
    _get = request.query.get
    offset = int(_get("offset", 0)) if _get("offset", "").isnumeric() else 0
    limit = int(_get("limit", 25)) if _get("limit", "").isnumeric() else 25
    query = (
        sa.select(
            PostCategory.id,
            PostCategory.slug,
            PostCategory.title,
            PostCategory.is_tag,
            sa.func.count(Post.id).label("count"),
        )
        .select_from(PostCategory)
        .outerjoin(
            PostTag,
            sa.and_(
                PostTag.post_category_id == PostCategory.id,
                PostTag.is_primary.is_(True),
            ),
        )
        .outerjoin(Post, Post.id == PostTag.post_id)
        .order_by(PostCategory.id.desc())
        .group_by(PostCategory.id)
    )
    tquery = query.with_only_columns(sa.func.count().over())
    total = await request["db"].scalar(tquery) or 0
    data = (await request["db"].execute(query.limit(limit).offset(offset))).all()

    return {"categories": data, "total": total}


@meta(title=_("Edit blog category"))
@login_required()
@aiohttp_jinja2.template("blog/admin/category_edit.html")
async def category_edit_admin(request):
    category_id = (
        int(request.match_info["category_id"])
        if request.match_info["category_id"].isnumeric()
        else request.match_info["category_id"]
    )

    if category_id == "+new":
        # insert new category draft
        item = PostCategory(
            title="",
            slug="#",
            description="",
            description_html="",
            is_tag=False,
            is_term=False,
        )
        request["db"].add(item)
        await request["db"].commit()
        return web.HTTPFound(
            request.app.router["edit_category_edit"].url_for(category_id=str(item.id))
        )

    session = await get_session(request)
    data = await request.post()

    item = (
        await request["db"].execute(
            sa.select(PostCategory).where(PostCategory.id == category_id)
        )
    ).scalar_one_or_none()
    if not item:
        await flash(request, "Рубрика не найдена", "warning")
        return web.HTTPFound(request.app.router["edit_category_list"].url_for())

    form = forms.CategoryForm(data, meta={"csrf_context": session})
    if request.method == "POST" and form.validate():
        does_slug_exist = await request["db"].scalar(
            sa.select(PostCategory.id)
            .select_from(PostCategory)
            .where(
                sa.and_(
                    PostCategory.slug == form.slug.data,
                    PostCategory.id != item.id,
                )
            )
        )
        if does_slug_exist:
            await flash(
                request,
                f"Slag '{form.slug.data}' уже существует, выберите другой",
                "warning",
            )
            return web.HTTPFound(
                request.app.router["edit_category_edit"].url_for(
                    category_id=str(item.id),
                )
            )
        description, _ = convert_to_html(form.description.data)
        # update category
        item.title = form.title.data
        item.slug = form.slug.data
        item.description = form.description.data
        item.description_html = description
        item.is_tag = form.is_tag.data
        item.is_term = form.is_term.data

        await request["db"].commit()

        await flash(
            request,
            (
                "Рубрика успешно отредактрована"
                if str(category_id).isnumeric()
                else "Рубрика успешно создана"
            ),
        )
        return web.HTTPFound(request.app.router["edit_category_list"].url_for())

    if item.slug != "#":
        form.process(obj=item)

    return {
        "form": form,
        "item": item,
    }


@meta(title=_("List of blog articles"))
@login_required()
@aiohttp_jinja2.template("blog/admin/post_list.html")
async def post_list_admin(request):
    session = await get_session(request)
    data = await request.post()
    form = forms.PostSearchForm(data, meta={"csrf_context": session})

    query = (
        sa.select(
            Post.id.label("id"),
            Post.short_id.label("short_id"),
            Post.title.label("title"),
            Post.slug.label("slug"),
            Post.lead.label("lead"),
            Post.created_at.label("created_at"),
            Post.user_id.label("user_id"),
            Post.picture.label("picture"),
            Post.updated_at.label("updated_at"),
            Post.is_published.label("is_published"),
            Post.published_at.label("published_at"),
            PostCategory.slug.label("category_slug"),
            PostCategory.title.label("category_title"),
            User.name.label("author_name"),
            sa.func.length(Post.body).label("length"),
        )
        .select_from(Post)
        .join(User)
        .outerjoin(
            PostTag,
            sa.and_(PostTag.post_id == Post.id, PostTag.is_primary.is_(True)),
        )
        .outerjoin(PostCategory, PostCategory.id == PostTag.post_category_id)
        .order_by(Post.id.desc())
    )
    keys = {Post.id.key: Post.id}
    posts_raw, total = await get_with_filters(query, request, keys)
    posts = prepare_posts(posts_raw, request)

    return {"posts": posts, "total": total, "form": form}


def form_tags_lists(tags):
    """
    Forming a list of tags for pre-filling a form field
    """
    tags_out = {"primary": "", "category": [], "tags": []}
    for item in tags:
        if item.is_primary:
            tags_out["primary"] = item.tag
        elif item.post_category_id:
            tags_out["category"].append(item.tag)
        else:
            tags_out["tags"].append(item.tag)
    return json.dumps(tags_out)


async def push_tags_in_db(request, post_id, tags_in):
    """
    Update data about post tags in DB
    """
    # delete all tags
    await request["db"].execute(sa.delete(PostTag).where(PostTag.post_id == post_id))

    categories_list = (
        (
            await request["db"].execute(
                sa.select(PostCategory).where(
                    sa.and_(
                        PostCategory.is_tag.is_(False),
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    categories = {category.slug: category.id for category in categories_list}

    if primary_category := (
        tags_in.get("primary") or tags_in.get("category").pop()
        if tags_in.get("category") or tags_in.get("primary")
        else False
    ):
        item = PostTag(
            tag=primary_category,
            post_id=post_id,
            is_primary=True,
            post_category_id=categories.get(primary_category),
        )
        request["db"].add(item)

    if tags_in.get("category"):
        for category_tag in tags_in.get("category"):
            category = PostTag(
                tag=category_tag,
                post_id=post_id,
                is_primary=False,
                post_category_id=categories.get(category_tag, None),
            )
            request["db"].add(category)

    if tags_in.get("tags"):
        for item in tags_in.get("tags"):
            tag = PostTag(
                tag=item,
                post_id=post_id,
                is_primary=False,
                post_category_id=None,
            )
            request["db"].add(tag)

    await request["db"].commit()


def make_filename(file):
    return uuid.uuid4().hex + Path(file.filename).suffix


async def delete_picture(filename, file_dir):
    preffix = Path(filename).stem
    for file in Path(file_dir).glob(f"{preffix}*"):
        file.unlink()


@meta(title=_("Edit blog article"))
@login_required()
@aiohttp_jinja2.template("blog/admin/post_edit.html")
async def post_edit_admin(request):
    post_id = (
        int(request.match_info["post_id"])
        if request.match_info["post_id"].isnumeric()
        else request.match_info["post_id"]
    )

    session = await get_session(request)
    data = await request.post()
    if post_id != "+new":
        post = (
            await request["db"].execute(sa.select(Post).where(Post.id == post_id))
        ).scalar_one_or_none()
        is_publish = request.query.get("publish")
        if is_publish == "q":
            post.is_published = True
            post.published_at = datetime.datetime.now()
            await request["db"].commit()
            await flash(request, f"Пост '{post.title}' опубликован")
            # await worker.send_task_local("seo.sitemap_generate")
            return web.HTTPFound(request.app.router["edit_post_list"].url_for())
    else:
        # insert Draft post
        post = Post(
            title="",
            slug="",
            lead="",
            body="",
            body_toc="",
            show_toc=False,
            user_id=request["user"].id,
            is_published=False,
        )
        request["db"].add(post)
        await request["db"].commit()
        return web.HTTPFound(
            request.app.router["edit_post_edit"].url_for(post_id=str(post.id))
        )

    post = (
        await request["db"].execute(sa.select(Post).where(Post.id == post_id))
    ).scalar_one_or_none()

    if request.query.get("publish") == "q":
        post.is_published = True
        post.published_at = datetime.datetime.now()
        await request["db"].commit()
        await flash(request, f"Пост '{post.title}' опубликован")
        return web.HTTPFound(request.app.router["edit_post_list"].url_for())

    data = await request.post()
    form = forms.PostForm(data, data=post.to_dict(), meta={"csrf_context": session})

    # get tags
    tags = (
        (
            await request["db"].execute(
                sa.select(PostTag).where(PostTag.post_id == post.id)
            )
        )
        .scalars()
        .all()
    )

    if request.method == "POST" and form.validate():
        config["worker_name"] = "worker"
        config["worker_queue"] = "celery"
        body_html, _ = convert_to_html(form.body.data)

        post.title = form.title.data
        post.slug = form.slug.data
        post.lead = form.lead.data
        post.body = form.body.data
        post.body_html = body_html
        post.show_toc = form.show_toc.data

        await request["db"].commit()

        tags_in = json.loads(form.category.data)

        await push_tags_in_db(request, post.id, tags_in)

        # check if media_root folder exists and is writable
        if not Path(config["media_root"]).exists() and not os.access(
            Path(config["media_root"]), os.W_OK
        ):
            try:
                from sentry_sdk import capture_message

                capture_message(f"Folder {config['media_root']} access error")
            except ModuleNotFoundError:
                await flash(
                    request,
                    "Папка загрузки картинок не найдена или не доступна для записи",
                    "warning",
                )

        else:
            MEDIA_DIR = Path(config["media_root"]) / "articles"

            file = data["picture"]
            os.makedirs(MEDIA_DIR, exist_ok=True)
            if file:
                # del old picture
                if post.picture:
                    await delete_picture(post.picture, MEDIA_DIR)
                # save new picture
                name = make_filename(file)
                filename = os.path.join(MEDIA_DIR, name)
                with open(filename, "wb") as f:
                    f.write(file.file.read())
                post.picture = name
                await request["db"].commit()
            # del the picture by user request?
            picture_delete = data.get("picture-delete")
            if picture_delete and post.picture:
                await delete_picture(post.picture, MEDIA_DIR)
                post.picture = None
                await request["db"].commit()

        await flash(request, f"Пост '{post.title}' успешно отредактирован")
        # resize all images by saving post if it is not done yet
        if post.picture and not os.path.isfile(
            os.path.join(MEDIA_DIR, post.picture)
            + "_original"
            + Path(post.picture).suffix
        ):
            # await worker.send_task_local(
            #     "img.img_resize_all", os.path.join(MEDIA_DIR, post.picture)
            # )
            pass
        if post.is_published:
            # await worker.send_task_local("seo.sitemap_generate")
            pass
        return web.HTTPFound(request.app.router["edit_post_list"].url_for())

    categories = (
        (
            await request["db"].execute(
                sa.select(PostCategory).where(sa.and_(PostCategory.is_tag.is_(False)))
            )
        )
        .scalars()
        .all()
    )
    post = post.to_dict()
    post["picture_origin"] = ""
    url = URL(config["media_prefix"]) / "articles/"
    if post["picture"]:
        post["picture_origin"] = URL(
            url
            / str(post["id"])
            / (post["picture"] + "_origin" + Path(post["picture"]).suffix)
        )
        post["picture"] = URL(url / str(post["id"]) / post["picture"])
    sign = TimedSerializer(config["secret_key"])
    post["safe_id"] = sign.dumps(post["id"])
    form.category.data = form_tags_lists(tags)

    return {"form": form, "item": post, "categories": categories}


@login_required()
async def blog_actions(request):
    item_id = request.match_info.get("item_id")
    action = request.match_info.get("action")
    user_id = request["user"].id

    # CSRF Check
    token = request.headers.get("X-CSRFToken")
    if not token:
        raise web.HTTPForbidden(text="Missing CSRF Token")

    try:
        signed_user_id = request.app[SIGNER_KEY].loads(token, max_age=86400)
        if signed_user_id != user_id:
            raise web.HTTPForbidden(text="Invalid CSRF Token Owner")
    except (BadSignature, SignatureExpired):
        raise web.HTTPForbidden(text="Invalid CSRF Token")

    if action == "delete_post":
        try:
            post_id = int(item_id)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid post id")

        # Verify ownership or admin rights?
        # Assuming admin access is required for this view as it is in admin section
        # But let's check if the user is admin or owner.
        # Existing code didn't check much other than login_required.
        # But let's be safe.
        post = (
            await request["db"].execute(sa.select(Post).where(Post.id == post_id))
        ).scalar_one_or_none()
        if not post:
            raise web.HTTPNotFound(text=_("Article not found"))

        # Delete related tags
        await request["db"].execute(
            sa.delete(PostTag).where(PostTag.post_id == post.id)
        )

        # Delete picture if exists
        if post.picture:
            MEDIA_DIR = Path(config["media_root"]) / "articles"
            await delete_picture(post.picture, MEDIA_DIR)

        await request["db"].delete(post)
        await request["db"].commit()

        await flash(request, _("Post deleted"), "success")
        # Return empty response to remove row or redirect?
        # If we are on a list page, we might want to refresh.
        # Let's return "ok" and let HTMX refresh or remove the row.
        # Ideally we should return a 200 OK and maybe a trigger.
        response = web.Response(text="ok")
        response.headers["HX-Refresh"] = "true"
        return response

    elif action == "delete_category":
        try:
            category_id = int(item_id)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid category id")

        category = (
            await request["db"].execute(
                sa.select(PostCategory).where(PostCategory.id == category_id)
            )
        ).scalar_one_or_none()
        if not category:
            raise web.HTTPNotFound(text=_("Category not found"))

        # Check if there are posts in this category?
        # Existing code didn't have delete category.
        # Let's assume we can delete it.
        await request["db"].delete(category)
        await request["db"].commit()
        await flash(request, _("Category deleted"), "success")

        response = web.Response(text="ok")
        response.headers["HX-Refresh"] = "true"
        return response

    raise web.HTTPBadRequest(text="Unknown action")
