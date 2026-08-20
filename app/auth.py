"""Login, sessions and the gate in front of everything else.

The app holds real client names, policy numbers and payment amounts, and it is now
reachable by more than one person over Tailscale. Tailscale controls *who can
reach* the app; this module controls *who can use it* and records which person did
what, which device-level access cannot.

Design notes worth keeping:

**The gate is middleware, not a per-route dependency.** main.py mounts the
frontend as a catch-all at "/", so a route-by-route guard would leave every static
asset public - the UI would still load and only the JSON would 401, which looks
protected without being protected. Middleware runs before routing, so the
allowlist below is the complete set of things an anonymous caller can fetch.

**Sessions are server-side rows, not signed cookies.** A signed cookie cannot be
revoked: logging out, disabling a user or deleting a session would all keep
working until expiry. See store.session_user().

**No signup route exists.** Accounts are created with scripts/adduser.py. This is
a known, small team, so a registration form would be attack surface with no user.

**Browsers get a redirect, API callers get 401.** Same check, two response shapes,
decided by the path prefix - a fetch() that receives an HTML login page instead of
JSON fails in a confusing way.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import store

COOKIE_NAME = "session"

# Everything an unauthenticated caller may fetch. Keep this list minimal and
# explicit: anything not here requires a session, including the SPA itself.
PUBLIC_PATHS = frozenset(
    {
        "/login",
        "/api/login",
        "/api/logout",
        "/style.css",
        "/favicon.ico",
        "/api/health",
    }
)


def _wants_json(path: str) -> bool:
    return path.startswith("/api/")


def current_user(request: Request) -> dict | None:
    """The signed-in user for this request, or None. Set by the middleware."""
    return getattr(request.state, "user", None)


async def gate(request: Request, call_next):
    """Reject anonymous requests to anything outside PUBLIC_PATHS."""
    path = request.url.path
    token = request.cookies.get(COOKIE_NAME, "")
    request.state.user = store.session_user(token) if token else None

    if request.state.user is None and path not in PUBLIC_PATHS:
        if _wants_json(path):
            return JSONResponse({"detail": "Not signed in."}, status_code=401)
        # 303 so the browser issues a GET for the login page even if this was a
        # POST that lost its session mid-flight.
        return RedirectResponse("/login", status_code=303)

    return await call_next(request)


def set_session_cookie(response, token: str, secure: bool) -> None:
    """Attach the session cookie.

    `secure` is derived from the request scheme rather than hardcoded: over
    Tailscale this app is served on real HTTPS and the flag must be set, but a
    Secure cookie is never returned over plain http://localhost, which would make
    local development impossible to log into.
    """
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=store.SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")
