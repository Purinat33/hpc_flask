from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, abort
from flask_login import login_required, current_user
from sqlalchemy import select, func, and_, or_
from models.base import session_scope
from models.schema import Ticket, TicketComment, User
from models.audit_store import audit
from datetime import datetime, timezone

tickets_bp = Blueprint("tickets", __name__, url_prefix="/tickets")

TICKET_TITLE_MAX = 200
TICKET_BODY_MAX = 10000
COMMENT_MAX = 5000


def _utcnow(): return datetime.now(timezone.utc)
def _is_admin() -> bool: return getattr(current_user, "role", None) == "admin"


def _audit_ok(action: str, target_type: str, target_id: int | str, **extra):
    try:
        audit(action, target_type=target_type, target_id=str(target_id),
              outcome="success", status=200, extra=extra or None)
    except Exception:
        pass


def _audit_blocked(action: str, target_type: str, target_id: int | str, status: int, error: str, **extra):
    try:
        audit(action, target_type=target_type, target_id=str(target_id),
              outcome="blocked", status=status, error_code=error, extra=extra or None)
    except Exception:
        pass


@tickets_bp.get("/")
@login_required
def list_tickets():
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = min(max(int(request.args.get("per_page", 20) or 20), 5), 100)

    # filters
    q_title = (request.args.get("q") or "").strip()
    # one of statuses or empty
    status_f = (request.args.get("status") or "").strip()
    # only show my tickets (as requester)
    mine = request.args.get("mine") in ("1", "true", "on")
    assigned_to_me = request.args.get("assigned") in (
        "1", "true", "on")  # admin: tickets I’m assigned
    # latest | priority | updated
    sort = (request.args.get("sort") or "latest").strip()

    filters = []
    # visibility: users see only their tickets, admins see all unless filter says otherwise
    if not _is_admin():
        filters.append(Ticket.requester_username == current_user.id)
    else:
        if mine:
            filters.append(Ticket.requester_username == current_user.id)
        if assigned_to_me:
            filters.append(Ticket.assignee_username == current_user.id)

    if q_title:
        filters.append(Ticket.title.ilike(f"%{q_title}%"))
    if status_f:
        filters.append(Ticket.status == status_f)

    order = [Ticket.created_at.desc()]
    if sort == "priority":
        # urgent > high > normal > low
        order = [
            func.array_position(func.array(
                ["urgent", "high", "normal", "low"]), Ticket.priority).asc(),
            Ticket.created_at.desc(),
        ]
    elif sort == "updated":
        order = [Ticket.updated_at.desc(), Ticket.created_at.desc()]

    with session_scope() as s:
        base = select(Ticket)
        if filters:
            base = base.where(and_(*filters))
        rows = s.execute(
            base.order_by(*order)
                .offset((page-1)*per_page)
                .limit(per_page)
        ).scalars().all()

        total = s.execute(
            (select(func.count()).select_from(Ticket).where(and_(*filters))
             if filters else select(func.count()).select_from(Ticket))
        ).scalar_one()

    return render_template(
        "tickets/list.html",
        tickets=rows, page=page, per_page=per_page, total=total,
        q=q_title, status_f=status_f, mine=mine, assigned_to_me=assigned_to_me, sort=sort,
        is_admin=_is_admin(),
    )


@tickets_bp.get("/new")
@login_required
def new_ticket_form():
    return render_template("tickets/new.html")


@tickets_bp.post("/new")
@login_required
def new_ticket_submit():
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not title or not body:
        _audit_blocked("ticket.create", "ticket", "new", 400,
                       "validation_missing", title_len=len(title), body_len=len(body))
        abort(400, "Title and body required")
    if len(title) > TICKET_TITLE_MAX or len(body) > TICKET_BODY_MAX:
        _audit_blocked("ticket.create", "ticket", "new", 400,
                       "validation_too_long", title_len=len(title), body_len=len(body))
        abort(400, "Text too long")

    with session_scope() as s:
        t = Ticket(
            title=title, body=body,
            requester_username=current_user.id,
            status="open", priority="normal",
        )
        s.add(t)
        s.flush()
        _audit_ok("ticket.create", "ticket", t.id, title_len=len(title))
        return redirect(url_for("tickets.view_ticket", ticket_id=t.id))


@tickets_bp.get("/<int:ticket_id>")
@login_required
def view_ticket(ticket_id: int):
    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        if not t:
            abort(404)
        # visibility
        if not _is_admin() and t.requester_username != current_user.id:
            _audit_blocked("ticket.view", "ticket",
                           ticket_id, 403, "forbidden_view")
            abort(403)
        # preload comments (internal ones hidden from users)
        if _is_admin():
            comments = list(t.comments)
        else:
            comments = [c for c in t.comments if not c.is_internal]

    return render_template(
        "tickets/view.html",
        t=t, comments=comments, is_admin=_is_admin()
    )


@tickets_bp.post("/<int:ticket_id>/comment")
@login_required
def comment_ticket(ticket_id: int):
    body = (request.form.get("body") or "").strip()
    is_internal = (request.form.get("is_internal") == "1")
    if not body:
        _audit_blocked("ticket.comment", "ticket", ticket_id,
                       400, "validation_missing_body")
        abort(400, "Body required")
    if len(body) > COMMENT_MAX:
        _audit_blocked("ticket.comment", "ticket", ticket_id,
                       400, "validation_too_long", body_len=len(body))
        abort(400, "Comment too long")

    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        if not t:
            _audit_blocked("ticket.comment", "ticket",
                           ticket_id, 404, "not_found")
            abort(404)
        # visibility
        if not _is_admin() and t.requester_username != current_user.id:
            _audit_blocked("ticket.comment", "ticket",
                           ticket_id, 403, "forbidden_comment")
            abort(403)
        # users cannot write internal notes
        if is_internal and not _is_admin():
            is_internal = False

        c = TicketComment(ticket_id=ticket_id, author_username=current_user.id,
                          body=body, is_internal=is_internal)
        t.updated_at = _utcnow()
        s.add(c)
        s.add(t)
        s.flush()
        _audit_ok("ticket.comment", "ticket", ticket_id,
                  is_internal=bool(is_internal), comment_id=c.id)

    return redirect(url_for("tickets.view_ticket", ticket_id=ticket_id))

# --- Admin / Owner actions ---


@tickets_bp.post("/<int:ticket_id>/assign")
@login_required
def assign_ticket(ticket_id: int):
    if not _is_admin():
        _audit_blocked("ticket.assign", "ticket", ticket_id,
                       403, "forbidden_not_admin")
        abort(403)
    assignee = (request.form.get("assignee") or "").strip() or None
    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        if not t:
            abort(404)
        # allow blank to unassign
        t.assignee_username = assignee
        t.updated_at = _utcnow()
        s.add(t)
        _audit_ok("ticket.assign", "ticket",
                  ticket_id, assignee=assignee or "")
    return redirect(url_for("tickets.view_ticket", ticket_id=ticket_id))


@tickets_bp.post("/<int:ticket_id>/status")
@login_required
def update_status(ticket_id: int):
    new_status = (request.form.get("status") or "").strip()
    allowed = ("open", "in_progress", "pending_user", "resolved", "closed")
    if new_status not in allowed:
        _audit_blocked("ticket.status", "ticket", ticket_id,
                       400, "invalid_status", status=new_status)
        abort(400, "Invalid status")

    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        if not t:
            abort(404)

        # who can change:
        # - admin can always change
        # - requester can set pending_user or closed on own ticket
        if not _is_admin():
            if t.requester_username != current_user.id:
                _audit_blocked("ticket.status", "ticket",
                               ticket_id, 403, "forbidden_not_owner")
                abort(403)
            if new_status not in ("pending_user", "closed"):
                _audit_blocked("ticket.status", "ticket", ticket_id,
                               403, "forbidden_status_for_user", status=new_status)
                abort(403)

        t.status = new_status
        if new_status in ("resolved", "closed"):
            t.closed_at = _utcnow()
        else:
            t.closed_at = None
        t.updated_at = _utcnow()
        s.add(t)
        _audit_ok("ticket.status", "ticket", ticket_id, status=new_status)
    return redirect(url_for("tickets.view_ticket", ticket_id=ticket_id))


@tickets_bp.post("/<int:ticket_id>/priority")
@login_required
def update_priority(ticket_id: int):
    if not _is_admin():
        _audit_blocked("ticket.priority", "ticket",
                       ticket_id, 403, "forbidden_not_admin")
        abort(403)
    new_priority = (request.form.get("priority") or "").strip()
    if new_priority not in ("low", "normal", "high", "urgent"):
        _audit_blocked("ticket.priority", "ticket", ticket_id,
                       400, "invalid_priority", priority=new_priority)
        abort(400, "Invalid priority")

    with session_scope() as s:
        t = s.get(Ticket, ticket_id)
        if not t:
            abort(404)
        t.priority = new_priority
        t.updated_at = _utcnow()
        s.add(t)
        _audit_ok("ticket.priority", "ticket",
                  ticket_id, priority=new_priority)
    return redirect(url_for("tickets.view_ticket", ticket_id=ticket_id))
