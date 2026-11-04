"""Local dashboard: feature toggles, approved contacts, approved chats, audit list.

Served on 127.0.0.1 behind a password. Plain HTML forms, no JavaScript. Every POST
carries a per-session CSRF token, the session cookie is SameSite=Strict, and the Host
header must be local so a web page in the Mac's browser can't drive the dashboard.
"""
from __future__ import annotations

import hmac
import os
import secrets as pysecrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, abort, redirect, render_template_string, request, session, url_for

from .settings import CHAT_MODES, FEATURES, Notes, Settings, feature_rows
from .store import Store

LOCAL_HOSTS = {"127.0.0.1", "localhost"}

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{{ name }} bridge</title>
<style>
 body{font:15px/1.4 -apple-system,system-ui,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#1d1d1f;background:#fff}
 h1{font-size:22px} h2{font-size:17px;margin-top:32px;border-bottom:1px solid #ddd;padding-bottom:4px}
 table{border-collapse:collapse;width:100%} td,th{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}
 th{font-weight:600;color:#555} .muted{color:#777;font-size:13px} input[type=text]{width:100%;box-sizing:border-box}
 .pending{background:#fff6dc} .banner{background:#fde7e7;padding:8px 12px;border-radius:6px}
 form.inline{display:inline} button{cursor:pointer}
</style></head><body>
<form method="post" action="{{ url_for('logout') }}" style="float:right"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Sign out</button></form>
<h1>{{ name }} bridge</h1>
{% if shadow %}<p class="banner">Shadow mode is on in config.yaml: decisions are logged, nothing is sent.</p>{% endif %}

<h2>Feature toggles</h2>
<form method="post" action="{{ url_for('save_features') }}"><input type="hidden" name="csrf" value="{{ csrf }}">
<table><tr><th>Feature</th><th>On</th><th>When off</th></tr>
{% for key, label, default, off in features %}
<tr><td>{{ label }}</td><td><input type="checkbox" name="{{ key }}" {% if state[key] %}checked{% endif %}></td><td class="muted">{{ off }}</td></tr>
{% endfor %}</table>
<p><button>Save toggles</button></p></form>

<h2>Suggested notes{% if notes %} ({{ notes|length }}){% endif %}</h2>
<p class="muted">Facts {{ name }} picked up in chat. Approving adds the line to family.md (you can edit the wording first); rejecting discards it. Unreviewed suggestions are deleted after 30 days.</p>
<table><tr><th>Note</th><th>From</th><th>When</th><th></th></tr>
{% for n in notes %}
<tr><form method="post" action="{{ url_for('decide_note', note_id=n.id) }}"><input type="hidden" name="csrf" value="{{ csrf }}">
<td><input type="text" name="text" value="{{ n.text }}"></td><td class="muted">{{ n.source or '' }}</td><td class="muted">{{ fmt(n.created_ts) }}</td>
<td><button name="action" value="approve">Approve</button> <button name="action" value="reject">Reject</button></td></form></tr>
{% else %}<tr><td colspan="4" class="muted">No suggestions waiting.</td></tr>
{% endfor %}</table>

<h2>Approved contacts</h2>
<table><tr><th>Name</th><th>Handles (comma-separated)</th><th>One-on-one</th><th>Can assign tasks</th><th></th></tr>
{% for c in contacts %}
<tr><form method="post" action="{{ url_for('update_contact', contact_id=c.id) }}"><input type="hidden" name="csrf" value="{{ csrf }}">
<td><input type="text" name="name" value="{{ c.name }}" required></td>
<td><input type="text" name="handles" value="{{ c.handles|join(', ') }}" required></td>
<td><input type="checkbox" name="one_on_one" {% if c.one_on_one %}checked{% endif %}></td>
<td><input type="checkbox" name="can_assign" {% if c.can_assign %}checked{% endif %}></td>
<td><button>Save</button></form>
<form class="inline" method="post" action="{{ url_for('delete_contact', contact_id=c.id) }}"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Remove</button></form></td></tr>
{% endfor %}
<tr><form method="post" action="{{ url_for('add_contact') }}"><input type="hidden" name="csrf" value="{{ csrf }}">
<td><input type="text" name="name" placeholder="Name" required></td>
<td><input type="text" name="handles" placeholder="+13035550100, name@example.com" required></td>
<td><input type="checkbox" name="one_on_one"></td><td><input type="checkbox" name="can_assign" checked></td>
<td><button>Add</button></td></form></tr>
</table>

<h2>Approved chats</h2>
<p class="muted">Group chats that add {{ name }} appear here as Pending until you choose a mode. Respond: {{ name }} takes part. Listen only: logs context, never speaks. Off: ignored.</p>
<table><tr><th>Chat</th><th>Participants</th><th>Chat ID</th><th>Mode</th></tr>
{% for ch in chats %}
<tr class="{{ 'pending' if ch.mode == 'pending' else '' }}"><td>{{ ch.name or '(unnamed group)' }}</td>
<td class="muted">{{ ch.participants|join(', ') }}</td><td class="muted">{{ ch.chat_guid }}</td>
<td><form method="post" action="{{ url_for('set_chat_mode') }}"><input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="chat_guid" value="{{ ch.chat_guid }}">
<select name="mode">{% for m in modes %}<option value="{{ m }}" {% if m == ch.mode %}selected{% endif %}>{{ mode_labels[m] }}</option>{% endfor %}</select>
<button>Set</button></form></td></tr>
{% else %}<tr><td colspan="4" class="muted">No group chats seen yet. Send a message in the group and reload.</td></tr>
{% endfor %}</table>

<h2>Recent routing decisions</h2>
<table><tr><th>Time</th><th>Step</th><th>Route</th><th>Classifier</th><th>Confidence</th><th>Intent</th><th>Reason</th></tr>
{% for d in decisions %}<tr><td class="muted">{{ fmt(d.ts) }}</td><td>{{ d.step }}</td><td>{{ d.route }}</td>
<td>{{ '' if d.cls_route is none else ('for ' ~ name if d.cls_route else 'not for ' ~ name) }}</td>
<td>{{ '%.2f'|format(d.confidence) if d.confidence is not none else '' }}</td><td>{{ d.intent or '' }}</td>
<td>{{ d.reason }}{% if d.cls_reason and d.cls_reason != d.reason %}<br><span class="muted">Classifier: {{ d.cls_reason }}</span>{% endif %}</td></tr>
{% else %}<tr><td colspan="7" class="muted">None yet.</td></tr>{% endfor %}</table>

<h2>Audit</h2>
<table><tr><th>Time</th><th>By</th><th>What</th><th>Old</th><th>New</th></tr>
{% for a in audit %}<tr><td class="muted">{{ fmt(a.ts) }}</td><td>{{ a.actor }}</td><td>{{ a.item }}</td><td>{{ a.old or '' }}</td><td>{{ a.new or '' }}</td></tr>
{% else %}<tr><td colspan="5" class="muted">No changes yet.</td></tr>{% endfor %}</table>
</body></html>"""

LOGIN = """<!doctype html><html><head><meta charset="utf-8"><title>{{ name }} bridge</title>
<style>body{font:15px -apple-system,system-ui,sans-serif;max-width:320px;margin:80px auto;padding:0 16px;background:#fff;color:#1d1d1f}</style></head>
<body><h1>{{ name }} bridge</h1>{% if error %}<p style="color:#b00">Wrong password.</p>{% endif %}
<form method="post"><input type="password" name="password" autofocus required style="width:100%">
<p><button>Sign in</button></p></form></body></html>"""

MODE_LABELS = {"respond": "Respond", "listen": "Listen only", "off": "Off", "pending": "Pending"}


def create_app(settings: Settings, store: Store, password: str, tz: ZoneInfo, shadow: bool,
               notes: Notes | None = None, assistant: str = "Mia", quiet_hours: tuple[int, int] = (22, 7)) -> Flask:
    app = Flask("kinchat-dashboard")
    app.secret_key = os.urandom(32)
    app.config.update(SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True)

    def fmt(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz).strftime("%b %d %H:%M")

    def handles(raw: str) -> list[str]:
        return [h.strip() for h in raw.split(",") if h.strip()]

    @app.before_request
    def guard():
        if (request.host or "").rsplit(":", 1)[0] not in LOCAL_HOSTS:
            abort(403)
        if request.endpoint == "login":
            return None
        if not session.get("ok"):
            return redirect(url_for("login"))
        if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
            abort(403)
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = False
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", "").encode(), password.encode()):
                session.clear()
                session["ok"] = True
                session["csrf"] = pysecrets.token_urlsafe(32)
                return redirect(url_for("index"))
            time.sleep(1)
            error = True
        return render_template_string(LOGIN, error=error, name=assistant)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return render_template_string(
            PAGE, csrf=session["csrf"], shadow=shadow, features=feature_rows(assistant, *quiet_hours), state=settings.features(),
            contacts=settings.contacts(), chats=settings.chats(), modes=CHAT_MODES, mode_labels=MODE_LABELS,
            decisions=store.recent_decisions(30), audit=settings.audit_log(100), fmt=fmt,
            notes=notes.pending() if notes else [], name=assistant)

    @app.post("/notes/<int:note_id>")
    def decide_note(note_id: int):
        if notes is None:
            abort(404)
        action = request.form.get("action")
        if action == "approve":
            notes.approve(note_id, request.form.get("text", ""))
        elif action == "reject":
            notes.reject(note_id)
        else:
            abort(400)
        return redirect(url_for("index"))

    @app.post("/features")
    def save_features():
        for key, *_ in FEATURES:
            settings.set_feature(key, key in request.form)
        return redirect(url_for("index"))

    @app.post("/contacts")
    def add_contact():
        name, hs = request.form.get("name", "").strip(), handles(request.form.get("handles", ""))
        if name and hs:
            settings.add_contact(name, hs, "one_on_one" in request.form, "can_assign" in request.form)
        return redirect(url_for("index"))

    @app.post("/contacts/<int:contact_id>")
    def update_contact(contact_id: int):
        name, hs = request.form.get("name", "").strip(), handles(request.form.get("handles", ""))
        if name and hs:
            settings.update_contact(contact_id, name, hs, "one_on_one" in request.form, "can_assign" in request.form)
        return redirect(url_for("index"))

    @app.post("/contacts/<int:contact_id>/delete")
    def delete_contact(contact_id: int):
        settings.delete_contact(contact_id)
        return redirect(url_for("index"))

    @app.post("/chats")
    def set_chat_mode():
        mode = request.form.get("mode", "")
        if mode not in CHAT_MODES:
            abort(400)
        settings.set_chat_mode(request.form.get("chat_guid", ""), mode)
        return redirect(url_for("index"))

    return app
