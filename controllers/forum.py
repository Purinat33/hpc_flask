from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, abort, current_app
from flask_login import login_required, current_user
from sqlalchemy import select
from models.base import session_scope
from models.schema import ForumThread, ForumComment
from controllers.auth import admin_required  # you already define this
from sqlalchemy import select, func
from datetime import datetime, timezone

forum_bp = Blueprint("forum", __name__, url_prefix="/forum")
COMMENT_MAX = 2000
THREAD_TITLE_MAX = 200
THREAD_BODY_MAX = 10000
# ------------------- Threads -----------------------------------------


def _utcnow():
    return datetime.now(timezone.utc)


def _is_admin() -> bool:
    return getattr(current_user, "role", None) == "admin"

@forum_bp.get("/")
def thread_list():
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = min(max(int(request.args.get("per_page", 20) or 20), 5), 100)

    with session_scope() as s:
        # page items
        q = (
            select(ForumThread)
            .order_by(ForumThread.is_pinned.desc(), ForumThread.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        items = s.execute(q).scalars().all()

        # total rows (SQL COUNT(*))
        total = s.execute(select(func.count()).select_from(
            ForumThread)).scalar_one()

    return render_template(
        "forum/list.html",
        threads=items,
        page=page,
        per_page=per_page,
        total=total,
    )


@forum_bp.get("/new")
@login_required
def thread_new_form():
    return render_template("forum/new.html")


@forum_bp.post("/<int:thread_id>/lock")
@login_required
def thread_lock(thread_id: int):
    if not _is_admin():
        abort(403)
    with session_scope() as s:
        t = s.get(ForumThread, thread_id)
        if not t:
            abort(404)
        t.is_locked = True
        s.add(t)
    return redirect(url_for("forum.thread_view", thread_id=thread_id))


@forum_bp.post("/<int:thread_id>/unlock")
@login_required
def thread_unlock(thread_id: int):
    if not _is_admin():
        abort(403)
    with session_scope() as s:
        t = s.get(ForumThread, thread_id)
        if not t:
            abort(404)
        # Only unlock if not soft-deleted
        if t.is_deleted:
            abort(400, "Cannot unlock a deleted thread")
        t.is_locked = False
        s.add(t)
    return redirect(url_for("forum.thread_view", thread_id=thread_id))


@forum_bp.post("/new")
@login_required
def thread_new_submit():
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not title or not body:
        abort(400, "Title and body required")
    if len(title) > THREAD_TITLE_MAX or len(body) > THREAD_BODY_MAX:
        abort(400, "Post too long")

    with session_scope() as s:
        t = ForumThread(title=title, body=body,
                        author_username=current_user.id)
        s.add(t)
        s.flush()
        return redirect(url_for("forum.thread_view", thread_id=t.id))

@forum_bp.get("/<int:thread_id>")
def thread_view(thread_id: int):
    with session_scope() as s:
        t = s.get(ForumThread, thread_id)
        if not t:
            abort(404)
        # eager load comments (flat), we'll render as a tree in template
        comments = s.execute(
            select(ForumComment).where(ForumComment.thread_id ==
                                       thread_id).order_by(ForumComment.created_at.asc())
        ).scalars().all()
    return render_template("forum/thread.html", thread=t, comments=comments)


@forum_bp.post("/<int:thread_id>/delete")
@login_required
def thread_delete(thread_id: int):
    """Soft-delete a thread.
       - When unlocked: owner or admin may delete.
       - When locked: ONLY admin may delete (per requirement)."""
    with session_scope() as s:
        t = s.get(ForumThread, thread_id)
        if not t:
            abort(404)
        is_owner = (t.author_username == current_user.id)
        if t.is_locked:
            if not _is_admin():
                abort(403, "Thread is locked")
        else:
            if not (is_owner or _is_admin()):
                abort(403, "Not allowed")

        t.is_deleted = True
        t.deleted_by_admin = _is_admin() and not is_owner
        t.deleted_at = _utcnow()
        t.deleted_by_username = current_user.id
        t.is_locked = True  # deleted threads remain locked
        s.add(t)
    return redirect(url_for("forum.thread_view", thread_id=thread_id))


@forum_bp.post("/<int:thread_id>/comment")
@login_required
def comment_create(thread_id: int):
    body = (request.form.get("body") or "").strip()
    parent_id = request.form.get("parent_id", type=int)
    if not body:
        abort(400, "Body required")
    if len(body) > COMMENT_MAX:
        abort(400, "Comment too long")

    with session_scope() as s:
        t = s.get(ForumThread, thread_id)
        if not t:
            abort(404)
        if t.is_locked:
            abort(403, "Thread is locked")

        c = ForumComment(
            thread_id=thread_id,
            parent_id=parent_id,
            body=body,
            author_username=current_user.id,
        )
        s.add(c)
        s.flush()
        cid = c.id
    return redirect(url_for("forum.thread_view", thread_id=thread_id) + f"#c{cid}")


@forum_bp.post("/comment/<int:comment_id>/delete")
@login_required
def comment_delete(comment_id: int):
    """Soft-delete a comment.
       - When thread is locked: nobody can delete comments (even admin).
       - When unlocked: owner or admin may delete."""
    with session_scope() as s:
        c = s.get(ForumComment, comment_id)
        if not c:
            abort(404)
        t = s.get(ForumThread, c.thread_id)
        if t is None:
            abort(404)
        if t.is_locked:
            # disallow comment deletion post-lock
            abort(403, "Thread is locked")

        is_owner = (c.author_username == current_user.id)
        if not (is_owner or _is_admin()):
            abort(403, "Not allowed")

        c.is_deleted = True
        c.deleted_by_admin = _is_admin() and not is_owner
        c.deleted_at = _utcnow()
        c.deleted_by_username = current_user.id
        s.add(c)
        tid = c.thread_id
    return redirect(url_for("forum.thread_view", thread_id=tid))
