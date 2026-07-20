"""Shared-SSO authentication — implemented per SHARED_AUTH.md.

Server-side sessions live in shared.app_sessions; the browser cookie carries
only an opaque sid. Users are read READ-ONLY from shared.users; login is gated
on the `is_app_user` cross-app flag; `role == 'admin'` or `is_app_admin`
counts as admin. Auth is enabled when AUTH_DB_SCHEMA is set; with it unset
(dev) the app runs without a login gate.
"""
import json
import logging
import re
import secrets
from datetime import datetime
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, session, url_for)
from flask.sessions import SessionInterface, SessionMixin
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.datastructures import CallbackDict
from werkzeug.security import check_password_hash

from app.db import get_db

log = logging.getLogger('weathersniffer.web')

bp = Blueprint('auth', __name__)

# Rate-limit /login (the family uses 15/min); in-memory storage is fine in a
# single-process app.
limiter = Limiter(key_func=get_remote_address, default_limits=[])

_SID_RE = re.compile(r'^[A-Za-z0-9_-]{20,128}$')

# How THIS app gates access: the cross-app flag (recommended for new apps).
_REQUIRE_FLAG = 'is_app_user'
_DUMMY_HASH = 'scrypt:32768:8:1$dummy$' + '0' * 64  # anti-enumeration timing


def auth_enabled():
    return bool(current_app.config.get('AUTH_DB_SCHEMA'))


# ---------------------------------------------------------------------------
# Server-side session backend (SHARED_AUTH.md §4.2)
# ---------------------------------------------------------------------------

class _DBSession(CallbackDict, SessionMixin):
    def __init__(self, initial=None, sid=None, new=False):
        def _on_update(s):
            s.modified = True
        CallbackDict.__init__(self, initial, _on_update)
        self.sid = sid
        self.new = new
        self.modified = False


class DBSessionInterface(SessionInterface):
    """Server-side sessions stored in shared.app_sessions."""

    def _new_sid(self):
        return secrets.token_urlsafe(32)

    def _load(self, sid):
        db = get_db()
        try:
            row = db.execute(
                "SELECT data, expires_at FROM app_sessions WHERE sid = %s", (sid,)
            ).fetchone()
            if not row:
                return {}, False
            expires = row['expires_at']
            if isinstance(expires, str):
                expires = datetime.fromisoformat(expires.split('.')[0].replace('Z', ''))
            if expires < datetime.utcnow():
                db.execute("DELETE FROM app_sessions WHERE sid = %s", (sid,))
                db.commit()
                return {}, False
            data = json.loads(row['data']) if row['data'] else {}
            return (data if isinstance(data, dict) else {}), True
        finally:
            db.close()

    def open_session(self, app, request):
        cookie_name = app.config.get('SESSION_COOKIE_NAME', 'session')
        sid = request.cookies.get(cookie_name)
        if not sid or not _SID_RE.match(sid):
            return _DBSession(sid=self._new_sid(), new=True)
        data, ok = self._load(sid)
        if not ok:
            # Never adopt an unknown client-supplied sid (session-fixation defense)
            return _DBSession(sid=self._new_sid(), new=True)
        return _DBSession(data, sid=sid, new=False)

    def save_session(self, app, session, response):
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)
        cookie_name = app.config.get('SESSION_COOKIE_NAME', 'session')

        if not session:                      # cleared (logout) -> drop row + cookie
            if session.modified:
                db = get_db()
                try:
                    db.execute("DELETE FROM app_sessions WHERE sid = %s", (session.sid,))
                    db.commit()
                finally:
                    db.close()
                response.delete_cookie(cookie_name, domain=domain, path=path)
            return

        if not session.modified and not session.new:
            return

        expires = datetime.utcnow() + app.permanent_session_lifetime
        data_json = json.dumps(dict(session), default=str)
        db = get_db()
        try:
            db.execute(
                "INSERT INTO app_sessions (sid, user_id, data, last_seen, expires_at) "
                "VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s) "
                "ON CONFLICT (sid) DO UPDATE SET "
                "  user_id = EXCLUDED.user_id, data = EXCLUDED.data, "
                "  last_seen = CURRENT_TIMESTAMP, expires_at = EXCLUDED.expires_at",
                (session.sid, session.get('user_id'), data_json, expires),
            )
            db.commit()
        finally:
            db.close()

        response.set_cookie(
            cookie_name, session.sid, expires=expires,
            httponly=self.get_cookie_httponly(app),
            domain=domain, path=path,
            secure=self.get_cookie_secure(app),
            samesite=self.get_cookie_samesite(app),
        )


# ---------------------------------------------------------------------------
# Users (READ-ONLY from shared.users)
# ---------------------------------------------------------------------------

_USER_COLUMNS = ("id, username, password_hash, role, display_name, "
                 "must_change_password, is_readonly, is_app_user, is_app_admin")


def get_user_by_username(username):
    db = get_db()
    try:
        return db.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE username = %s LIMIT 1",
            (username,),
        ).fetchone()
    finally:
        db.close()


def get_user_by_id(user_id):
    db = get_db()
    try:
        return db.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE id = %s LIMIT 1",
            (user_id,),
        ).fetchone()
    finally:
        db.close()


def _populate_session(sess, user):
    """Write the session keys the family relies on."""
    sess.clear()                                  # session-fixation defense
    sess['user_id'] = user['id']
    sess['username'] = user['username']
    sess['display_name'] = user['display_name'] or user['username']
    sess['user_role'] = user['role']              # 321Theater reads this key
    sess['role'] = user['role']                   # Leash reads this key — set BOTH
    sess['is_readonly'] = bool(user.get('is_readonly', 0))
    sess['is_app_user'] = bool(user.get('is_app_user', 0))
    sess['is_app_admin'] = bool(user.get('is_app_admin', 0))
    sess['_role_checked_at'] = datetime.utcnow().timestamp()


def is_admin():
    return session.get('user_role') == 'admin' or bool(session.get('is_app_admin'))


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if auth_enabled() and 'user_id' not in session:
            return redirect(url_for('auth.login', next=request.path))
        return f(*a, **k)
    return w


def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if auth_enabled():
            if 'user_id' not in session:
                return redirect(url_for('auth.login', next=request.path))
            if not is_admin():
                abort(403)
        return f(*a, **k)
    return w


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('15 per minute', methods=['POST'])
def login():
    if not auth_enabled():
        return redirect(url_for('main.index'))
    if 'user_id' in session:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        try:
            user = get_user_by_username(username)
        except Exception as exc:
            log.error('Auth database unreachable during login: %s', exc)
            flash('Authentication service unavailable. Try again shortly.', 'error')
            return render_template('login.html', next=request.form.get('next', '')), 503
        if not user:
            check_password_hash(_DUMMY_HASH, password)        # constant-ish time
            log.warning('Failed login for unknown user %r actor=%s via=web', username, username)
            flash('Invalid username or password.', 'error')
        elif not check_password_hash(user['password_hash'], password):
            log.warning('Failed login (bad password) actor=%s via=web', username)
            flash('Invalid username or password.', 'error')
        elif not user.get(_REQUIRE_FLAG):
            log.warning('Login denied (no %s flag) actor=%s via=web', _REQUIRE_FLAG, username)
            flash('Your account does not have access to this app.', 'error')
        else:
            # Mint a fresh sid so the post-login cookie differs from any pre-login one
            session.sid = secrets.token_urlsafe(32)
            session.new = True
            _populate_session(session, user)
            session.permanent = True
            log.info('Login actor=%s via=web', username)
            nxt = request.form.get('next') or ''
            if not nxt.startswith('/') or nxt.startswith('//'):
                nxt = url_for('main.index')
            return redirect(nxt)
    return render_template('login.html', next=request.args.get('next', ''))


@bp.route('/logout')
def logout():
    actor = session.get('username')
    session.clear()        # deletes the shared row -> logs out of ALL apps
    if actor:
        log.info('Logout actor=%s via=web', actor)
    return redirect(url_for('auth.login') if auth_enabled() else url_for('main.index'))


# ---------------------------------------------------------------------------
# before_request: gate + periodic role re-check (SHARED_AUTH.md §4.7)
# ---------------------------------------------------------------------------

# Endpoints reachable without a session. The external read API (/api/v1) and
# /api/version carry their own (optional) API-key gate.
_EXEMPT_ENDPOINTS = {'auth.login', 'auth.logout', 'static'}
_EXEMPT_PREFIXES = ('/api/v1/', '/api/version')


def install_gate(app):
    @app.before_request
    def _auth_gate():
        if not auth_enabled():
            return
        if request.endpoint in _EXEMPT_ENDPOINTS:
            return
        if any(request.path.startswith(p) for p in _EXEMPT_PREFIXES):
            return
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return {'error': 'authentication required'}, 401
            return redirect(url_for('auth.login', next=request.path))

        # Periodic re-check so a demotion/revocation takes effect mid-session.
        if datetime.utcnow().timestamp() - session.get('_role_checked_at', 0) < 300:
            return
        try:
            user = get_user_by_id(session['user_id'])
        except Exception as exc:
            # Fail closed: shared DB unreachable while auth is enabled.
            log.error('Auth database unreachable during role re-check: %s', exc)
            abort(503)
        if not user or not user.get(_REQUIRE_FLAG):
            session.clear()
            return redirect(url_for('auth.login'))
        session['user_role'] = user['role']
        session['role'] = user['role']
        session['is_app_user'] = bool(user.get('is_app_user', 0))
        session['is_app_admin'] = bool(user.get('is_app_admin', 0))
        session['_role_checked_at'] = datetime.utcnow().timestamp()
