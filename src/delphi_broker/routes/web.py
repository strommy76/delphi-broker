"""Web UI routes for Delphi Broker (phone-friendly approval interface)."""

from __future__ import annotations

from pathlib import Path

import markdown
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import database as db
from ..config import DB_PATH, WEB_UI_AGENT_ID

router = APIRouter(prefix="/web")

_templates_dir = str(Path(__file__).resolve().parent.parent / "templates")
templates = Jinja2Templates(directory=_templates_dir)

_md = markdown.Markdown(extensions=["tables", "fenced_code", "nl2br"])


def _conn():
    return db.get_connection(DB_PATH)


def _render_body(body: str) -> str:
    _md.reset()
    return _md.convert(body)


def _pending_count() -> int:
    conn = _conn()
    try:
        msgs = db.list_messages(conn, status="PENDING", limit=999)
        return len(msgs)
    finally:
        conn.close()


def _render(request: Request, template: str, **kwargs) -> HTMLResponse:
    kwargs["request"] = request
    kwargs.setdefault("pending_count", _pending_count())
    return templates.TemplateResponse(name=template, context=kwargs, request=request)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = _conn()
    try:
        channels = db.list_channels(conn)
        agents = db.list_agents(conn)
        pending_total = sum(ch["pending"] for ch in channels)
        approved_total = sum(ch["approved"] for ch in channels)
        return _render(
            request, "dashboard.html",
            channels=channels,
            agents=agents,
            pending_total=pending_total,
            approved_total=approved_total,
            pending_count=pending_total,
        )
    finally:
        conn.close()


@router.get("/pending", response_class=HTMLResponse)
def pending_list(request: Request):
    conn = _conn()
    try:
        messages = db.list_messages(conn, status="PENDING", limit=100)
        for msg in messages:
            msg["body_html"] = _render_body(msg["body"])
        return _render(request, "pending.html", messages=messages)
    finally:
        conn.close()


@router.post("/pending/{message_id}/approve")
def approve_from_web(message_id: str, note: str = Form("")):
    conn = _conn()
    try:
        db.approve_message(conn, message_id, WEB_UI_AGENT_ID, note)
    finally:
        conn.close()
    return RedirectResponse("/web/pending", status_code=303)


@router.post("/pending/{message_id}/reject")
def reject_from_web(message_id: str, note: str = Form("")):
    conn = _conn()
    try:
        db.reject_message(conn, message_id, WEB_UI_AGENT_ID, note)
    finally:
        conn.close()
    return RedirectResponse("/web/pending", status_code=303)


@router.get("/message/{message_id}", response_class=HTMLResponse)
def message_detail(request: Request, message_id: str):
    conn = _conn()
    try:
        msg = db.get_message(conn, message_id)
        if not msg:
            return HTMLResponse("<h1>Not found</h1>", status_code=404)
        msg["body_html"] = _render_body(msg["body"])
        return _render(request, "message_detail.html", msg=msg)
    finally:
        conn.close()


@router.get("/channel/{channel_name}", response_class=HTMLResponse)
def channel_view(request: Request, channel_name: str):
    conn = _conn()
    try:
        messages = db.list_messages(conn, channel=channel_name, limit=100)
        return _render(request, "channel.html", messages=messages, channel_name=channel_name)
    finally:
        conn.close()


@router.get("/agents", response_class=HTMLResponse)
def agents_view(request: Request):
    conn = _conn()
    try:
        agents = db.list_agents(conn)
        return _render(request, "agents.html", agents=agents)
    finally:
        conn.close()
