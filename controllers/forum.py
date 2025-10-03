from __future__ import annotations
from models.schema import ForumThread, ForumComment, User, ForumThreadVote  # add ForumThreadVote
from models.schema import (
    ForumThread, ForumComment, ForumSolution, User,
    ForumThreadVote, ForumCommentVote
)
from sqlalchemy import select, func, and_
from models.schema import ForumSolution, ForumThread, ForumComment, User
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
        q = (
            select(ForumThread)
            .order_by(ForumThread.is_pinned.desc(), ForumThread.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        items = s.execute(q).scalars().all()

        total = s.execute(select(func.count()).select_from(
            ForumThread)).scalar_one()

        # Admin labels for authors shown in this page
        page_authors = {t.author_username for t in items}
        page_admins = set(
            s.execute(
                select(User.username).where(
                    User.username.in_(list(page_authors)),
                    User.role == "admin",
                )
            ).scalars()
        )

        # Thread scores for this page
        thread_scores = {}
        ids = [t.id for t in items]
        if ids:
            for tid, score in s.execute(
                select(ForumThreadVote.thread_id,
                       func.coalesce(func.sum(ForumThreadVote.value), 0))
                .where(ForumThreadVote.thread_id.in_(ids))
                .group_by(ForumThreadVote.thread_id)
            ):
                thread_scores[tid] = score

    return render_template(
        "forum/list.html",
        threads=items,
        page=page,
        per_page=per_page,
        total=total,
        page_admin_usernames=page_admins,
        thread_scores=thread_scores,   # <- pass to template
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

        # comments (flat)
        comments = s.execute(
            select(ForumComment)
            .where(ForumComment.thread_id == thread_id)
            .order_by(ForumComment.created_at.asc())
        ).scalars().all()

        # admin set for labels
        usernames = {t.author_username, *[c.author_username for c in comments]}
        admin_usernames = set(
            s.execute(
                select(User.username).where(User.username.in_(
                    list(usernames)), User.role == "admin")
            ).scalars()
        )

        # solutions (joined to comments)
        sols = s.execute(
            select(ForumSolution)
            .where(ForumSolution.thread_id == thread_id)
            .order_by(ForumSolution.created_at.asc())
        ).scalars().all()
        # materialize a lightweight list for templates
        solved_snippets = [
            {
                "comment_id": sol.comment_id,
                "author": sol.comment.author_username,
                "body": sol.comment.body,
                "created_at": sol.created_at,
            }
            for sol in sols
            if sol.comment and not sol.comment.is_deleted
        ]

    # after you’ve loaded `comments`
    comment_ids = [c.id for c in comments]

    with session_scope() as s2:
        # thread score + my vote
        thread_score = s2.execute(
            select(func.coalesce(func.sum(ForumThreadVote.value), 0))
            .where(ForumThreadVote.thread_id == thread_id)
        ).scalar_one()

        my_thread_vote = 0
        if current_user.is_authenticated:
            mv = s2.execute(
                select(ForumThreadVote.value)
                .where(ForumThreadVote.thread_id == thread_id, ForumThreadVote.username == current_user.id)
            ).scalar_one_or_none()
            my_thread_vote = mv or 0

        # comment scores
        comment_scores = {}
        if comment_ids:
            for cid, score in s2.execute(
                select(ForumCommentVote.comment_id,
                       func.coalesce(func.sum(ForumCommentVote.value), 0))
                .where(ForumCommentVote.comment_id.in_(comment_ids))
                .group_by(ForumCommentVote.comment_id)
            ):
                comment_scores[cid] = score

        # my votes on these comments
        my_comment_votes = {}
        if current_user.is_authenticated and comment_ids:
            for cid, val in s2.execute(
                select(ForumCommentVote.comment_id, ForumCommentVote.value)
                .where(ForumCommentVote.comment_id.in_(comment_ids),
                       ForumCommentVote.username == current_user.id)
            ):
                my_comment_votes[cid] = val

    return render_template(
        "forum/thread.html",
        thread=t,
        comments=comments,
        admin_usernames=admin_usernames,
        op_username=t.author_username,
        solved_snippets=solved_snippets,
        thread_score=thread_score,
        thread_user_vote=my_thread_vote,
        comment_scores=comment_scores,
        comment_user_votes=my_comment_votes,
    )


@forum_bp.post("/<int:thread_id>/pin")
@login_required
def thread_pin(thread_id: int):
    if not _is_admin():
        abort(403)
    with session_scope() as s:
        t = s.get(ForumThread, thread_id)
        if not t:
            abort(404)
        if t.is_deleted:
            abort(400, "Cannot pin a deleted thread")
        t.is_pinned = True
        s.add(t)
    return redirect(url_for("forum.thread_view", thread_id=thread_id))


@forum_bp.post("/<int:thread_id>/unpin")
@login_required
def thread_unpin(thread_id: int):
    if not _is_admin():
        abort(403)
    with session_scope() as s:
        t = s.get(ForumThread, thread_id)
        if not t:
            abort(404)
        t.is_pinned = False
        s.add(t)
    return redirect(url_for("forum.thread_view", thread_id=thread_id))


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
        t.is_locked = True
        t.is_pinned = False  # auto-unpin when deleted
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


@forum_bp.post("/<int:thread_id>/solve/<int:comment_id>")
@login_required
def mark_solution(thread_id: int, comment_id: int):
    with session_scope() as s:
        t = s.get(ForumThread, thread_id)
        if not t:
            abort(404)
        if t.author_username != current_user.id:
            abort(403, "Only the OP can mark solutions")
        if t.is_deleted or t.is_locked:
            abort(403, "Thread is locked or deleted")

        c = s.get(ForumComment, comment_id)
        if not c or c.thread_id != thread_id:
            abort(404)

        # disallow marking deleted comments
        if c.is_deleted:
            abort(400, "Cannot mark a deleted comment as solution")

        # enforce max 3
        count = s.execute(select(func.count()).select_from(ForumSolution).where(
            ForumSolution.thread_id == thread_id)).scalar_one()
        if count >= 3:
            abort(400, "Maximum of 3 solutions per thread")

        # skip if already marked
        exists = s.execute(
            select(ForumSolution.id).where(
                ForumSolution.thread_id == thread_id, ForumSolution.comment_id == comment_id
            )
        ).first()
        if not exists:
            s.add(ForumSolution(thread_id=thread_id,
                  comment_id=comment_id, created_by_username=current_user.id))

        # mark thread solved if first one
        t.is_solved = True
        s.add(t)

    return redirect(url_for("forum.thread_view", thread_id=thread_id) + f"#c{comment_id}")


@forum_bp.post("/<int:thread_id>/unsolve/<int:comment_id>")
@login_required
def unmark_solution(thread_id: int, comment_id: int):
    with session_scope() as s:
        t = s.get(ForumThread, thread_id)
        if not t:
            abort(404)
        if t.author_username != current_user.id:
            abort(403, "Only the OP can unmark solutions")
        if t.is_deleted or t.is_locked:
            abort(403, "Thread is locked or deleted")

        sol = s.execute(
            select(ForumSolution).where(
                ForumSolution.thread_id == thread_id, ForumSolution.comment_id == comment_id
            )
        ).scalars().first()
        if sol:
            s.delete(sol)

        # if no solutions left → unset is_solved
        remaining = s.execute(
            select(func.count()).select_from(ForumSolution).where(
                ForumSolution.thread_id == thread_id)
        ).scalar_one()
        if remaining == 0:
            t.is_solved = False
            s.add(t)

    return redirect(url_for("forum.thread_view", thread_id=thread_id) + f"#c{comment_id}")


def _clamp_vote(v: int) -> int:
    return 1 if v > 0 else (-1 if v < 0 else 0)


@forum_bp.post("/<int:thread_id>/vote")
@login_required
def thread_vote(thread_id: int):
    v = _clamp_vote(request.form.get("v", type=int) or 0)
    with session_scope() as s:
        t = s.get(ForumThread, thread_id)
        if not t:
            abort(404)
        if t.is_deleted:
            abort(400, "Cannot vote on deleted thread")

        # fetch existing
        existing = s.execute(
            select(ForumThreadVote).where(
                ForumThreadVote.thread_id == thread_id,
                ForumThreadVote.username == current_user.id
            )
        ).scalars().first()

        if v == 0:
            if existing:
                s.delete(existing)
        else:
            if existing:
                existing.value = v
                s.add(existing)
            else:
                s.add(ForumThreadVote(thread_id=thread_id,
                      username=current_user.id, value=v))
    return redirect(url_for("forum.thread_view", thread_id=thread_id))


@forum_bp.post("/comment/<int:comment_id>/vote")
@login_required
def comment_vote(comment_id: int):
    v = _clamp_vote(request.form.get("v", type=int) or 0)
    with session_scope() as s:
        c = s.get(ForumComment, comment_id)
        if not c:
            abort(404)
        # still disallow voting a deleted comment
        if c.is_deleted:
            abort(400, "Cannot vote on deleted comment")

        existing = s.execute(
            select(ForumCommentVote).where(
                ForumCommentVote.comment_id == comment_id,
                ForumCommentVote.username == current_user.id
            )
        ).scalars().first()

        if v == 0:
            if existing:
                s.delete(existing)
        else:
            if existing:
                existing.value = v
                s.add(existing)
            else:
                s.add(ForumCommentVote(comment_id=comment_id,
                      username=current_user.id, value=v))
    # bounce back to its thread
    return redirect(url_for("forum.thread_view", thread_id=c.thread_id) + f"#c{comment_id}")
