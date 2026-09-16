"""Persistence: statements, users and sessions in SQLite.

Replaces the in-memory `STATEMENTS` dict, which lost every parsed statement on
restart - the reason the history feature could not exist.

Why plain `sqlite3` and no ORM: `Statement.to_dict()` is already pure JSON at
roughly 2KB, so a statement is stored as one JSON blob with a few indexed columns
beside it for listing and searching. There is no relational shape to model, so an
ORM would add a dependency and a migration story for nothing. The whole corpus of
57 statements is under 6MB.

Two things to keep in mind when changing this file:

**The payload is the source of truth for a statement's contents.** The scalar
columns (`carrier`, `row_count`, `failsafe`, `exported_total`) are denormalised
copies used only so the history list can be filtered and sorted without loading
and parsing every blob. If you change what `to_dict()` produces, the payload
follows automatically; those columns do not.

**Never let a statement out without its failsafe verdict.** `failsafe` is stored
alongside so the export guard in main.py can refuse a blocked statement without
deserialising it. A row whose payload says `mismatch` must never be handed out as
exportable because the column drifted.

Passwords use `hashlib.scrypt` from the standard library rather than bcrypt or
argon2, keeping the dependency count where it is. Parameters are the interactive
defaults recommended for scrypt (n=2**15, r=8, p=1), which cost ~100ms per
verification here - slow enough to make guessing impractical, fast enough for a
login form.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "statements.db"

# Uploads used to live in tempfile.gettempdir(), which Windows clears and which a
# systemd unit with PrivateTmp=true would hide entirely. History is worthless if
# the source file behind a stored statement disappears, so they live beside the DB.
UPLOAD_DIR = DATA_DIR / "uploads"

SESSION_DAYS = 30

# Login throttling. Deliberately per-email rather than per-IP: everyone reaching
# this app arrives over the same Tailscale connection, so an IP-based limit would
# either lock out the whole team or nobody.
MAX_FAILURES = 8
LOCKOUT_MINUTES = 15

SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LEN = 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  password_hash BLOB NOT NULL,
  salt          BLOB NOT NULL,
  display_name  TEXT NOT NULL DEFAULT '',
  is_admin      INTEGER NOT NULL DEFAULT 0,
  disabled      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS statements (
  id             TEXT PRIMARY KEY,
  user_id        INTEGER REFERENCES users(id),
  filename       TEXT NOT NULL,
  carrier        TEXT NOT NULL DEFAULT '',
  uploaded_at    TEXT NOT NULL,
  row_count      INTEGER NOT NULL DEFAULT 0,
  failsafe       TEXT NOT NULL DEFAULT '',
  exported_total REAL,
  source_suffix  TEXT NOT NULL DEFAULT '',
  source_sha256  TEXT NOT NULL DEFAULT '',
  payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stmt_uploaded ON statements(uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_stmt_carrier  ON statements(carrier);
CREATE INDEX IF NOT EXISTS idx_stmt_sha      ON statements(source_sha256);

CREATE TABLE IF NOT EXISTS login_attempts (
  email        TEXT PRIMARY KEY,
  failures     INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def connect() -> sqlite3.Connection:
    """Open a connection with the pragmas this app depends on.

    WAL so a long read (listing history) does not block the writer (an upload),
    and foreign_keys so deleting a user actually clears their sessions.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# --- passwords ---------------------------------------------------------------

def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_LEN,
        maxmem=64 * 1024 * 1024,
    )
    return digest, salt


def _password_ok(password: str, digest: bytes, salt: bytes) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, digest)


# --- users -------------------------------------------------------------------

def create_user(
    email: str, password: str, display_name: str = "", is_admin: bool = False
) -> int:
    digest, salt = hash_password(password)
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, salt, display_name, is_admin,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                email.strip().lower(),
                digest,
                salt,
                display_name or email.split("@")[0],
                1 if is_admin else 0,
                _iso(_now()),
            ),
        )
        return int(cur.lastrowid)


def set_password(email: str, password: str) -> bool:
    digest, salt = hash_password(password)
    with connect() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE email = ?",
            (digest, salt, email.strip().lower()),
        )
        return cur.rowcount > 0


def list_users() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, email, display_name, is_admin, disabled, created_at"
            " FROM users ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def user_count() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def get_user(user_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, email, display_name, is_admin, disabled FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


# --- login throttling --------------------------------------------------------

def lock_remaining(email: str) -> int:
    """Seconds left on a lockout, or 0. Checked before the password is verified."""
    with connect() as conn:
        row = conn.execute(
            "SELECT locked_until FROM login_attempts WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
    if not row or not row["locked_until"]:
        return 0
    until = datetime.fromisoformat(row["locked_until"])
    remaining = (until - _now()).total_seconds()
    return int(remaining) if remaining > 0 else 0


def _record_failure(conn: sqlite3.Connection, email: str) -> None:
    row = conn.execute(
        "SELECT failures FROM login_attempts WHERE email = ?", (email,)
    ).fetchone()
    failures = (row["failures"] if row else 0) + 1
    locked = (
        _iso(_now() + timedelta(minutes=LOCKOUT_MINUTES))
        if failures >= MAX_FAILURES
        else None
    )
    conn.execute(
        "INSERT INTO login_attempts (email, failures, locked_until) VALUES (?, ?, ?)"
        " ON CONFLICT(email) DO UPDATE SET failures = ?, locked_until = ?",
        (email, failures, locked, failures, locked),
    )


def verify_user(email: str, password: str) -> dict | None:
    """Check credentials. Returns the user or None.

    A missing user still costs a scrypt hash, so a wrong address and a wrong
    password take the same time and the response cannot be used to enumerate who
    has an account.
    """
    email = email.strip().lower()
    with connect() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, salt, display_name, is_admin, disabled"
            " FROM users WHERE email = ?",
            (email,),
        ).fetchone()

        if row is None:
            hash_password(password)          # equalise timing
            _record_failure(conn, email)
            return None
        if row["disabled"]:
            _record_failure(conn, email)
            return None
        if not _password_ok(password, row["password_hash"], row["salt"]):
            _record_failure(conn, email)
            return None

        conn.execute("DELETE FROM login_attempts WHERE email = ?", (email,))
        return {
            "id": row["id"],
            "email": row["email"],
            "display_name": row["display_name"],
            "is_admin": bool(row["is_admin"]),
        }


# --- sessions ----------------------------------------------------------------

def create_session(user_id: int, user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at, user_agent)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                token,
                user_id,
                _iso(now),
                _iso(now + timedelta(days=SESSION_DAYS)),
                user_agent[:300],
            ),
        )
    return token


def session_user(token: str) -> dict | None:
    """Resolve a session cookie to a user, or None if unknown/expired/disabled.

    Server-side rather than a signed cookie so that logging out, disabling a user
    or deleting a session takes effect immediately and survives a restart.
    """
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT s.expires_at, u.id, u.email, u.display_name, u.is_admin, u.disabled"
            " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= _now():
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
        if row["disabled"]:
            return None
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "is_admin": bool(row["is_admin"]),
    }


def delete_session(token: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions() -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(_now()),))
        return cur.rowcount


# --- statements --------------------------------------------------------------

def save_statement(
    sid: str,
    payload: dict,
    user_id: int | None,
    source_suffix: str,
    source_sha256: str = "",
) -> dict:
    """Persist one parsed statement. Returns the payload with id/uploaded_at set."""
    uploaded_at = _iso(_now())
    payout = payload.get("payout") or {}
    payload = {**payload, "id": sid, "uploaded_at": uploaded_at}

    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO statements (id, user_id, filename, carrier,"
            " uploaded_at, row_count, failsafe, exported_total, source_suffix,"
            " source_sha256, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                user_id,
                payload.get("filename", ""),
                payload.get("template", ""),
                uploaded_at,
                len(payload.get("rows") or []),
                payout.get("status", ""),
                payout.get("exported_total"),
                source_suffix,
                source_sha256,
                json.dumps(payload),
            ),
        )
    return payload


def get_statement(sid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT s.payload, s.uploaded_at, u.display_name, u.email"
            " FROM statements s LEFT JOIN users u ON u.id = s.user_id"
            " WHERE s.id = ?",
            (sid,),
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    payload["uploaded_at"] = row["uploaded_at"]
    payload["uploaded_by"] = row["display_name"] or row["email"] or ""
    return payload


def source_path(sid: str) -> Path | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT source_suffix FROM statements WHERE id = ?", (sid,)
        ).fetchone()
    if row is None:
        return None
    path = UPLOAD_DIR / f"{sid}{row['source_suffix']}"
    return path if path.exists() else None


def duplicate_of(source_sha256: str) -> dict | None:
    """An earlier statement with identical file contents, if any.

    History is shared across the team, so the same statement being uploaded twice
    by two people is likely. Surfaced rather than blocked - the second upload may
    be deliberate.
    """
    if not source_sha256:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT s.id, s.filename, s.uploaded_at, u.display_name, u.email"
            " FROM statements s LEFT JOIN users u ON u.id = s.user_id"
            " WHERE s.source_sha256 = ? ORDER BY s.uploaded_at LIMIT 1",
            (source_sha256,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "filename": row["filename"],
        "uploaded_at": row["uploaded_at"],
        "uploaded_by": row["display_name"] or row["email"] or "",
    }


def list_statements(
    limit: int = 50,
    offset: int = 0,
    search: str = "",
    failsafe: str = "",
    uploader: int | None = None,
    since: str = "",
) -> tuple[list[dict], int]:
    """Summaries for the history view, newest first, plus the total match count.

    Summaries only - never the payload. The frontend used to fetch every
    statement in full on boot, which is fine for the handful held in memory and
    unusable against a history of thousands.
    """
    where, params = [], []
    if search:
        where.append("(s.filename LIKE ? OR s.carrier LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if failsafe:
        where.append("s.failsafe = ?")
        params.append(failsafe)
    if uploader is not None:
        where.append("s.user_id = ?")
        params.append(uploader)
    if since:
        where.append("s.uploaded_at >= ?")
        params.append(since)
    clause = f" WHERE {' AND '.join(where)}" if where else ""

    with connect() as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM statements s{clause}", params
            ).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT s.id, s.filename, s.carrier, s.uploaded_at, s.row_count,"
            " s.failsafe, s.exported_total, u.display_name, u.email"
            " FROM statements s LEFT JOIN users u ON u.id = s.user_id"
            f"{clause} ORDER BY s.uploaded_at DESC, s.id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()

    return (
        [
            {
                "id": r["id"],
                "filename": r["filename"],
                "carrier": r["carrier"],
                "uploaded_at": r["uploaded_at"],
                "row_count": r["row_count"],
                "failsafe": r["failsafe"],
                "exported_total": r["exported_total"],
                "uploaded_by": r["display_name"] or r["email"] or "",
            }
            for r in rows
        ],
        total,
    )


def recent_ids(user_id: int | None, hours: int = 24) -> list[str]:
    """A caller's recent uploads - the default scope for 'export all'.

    Without this, persistence would silently turn "Export all to Excel" from
    "today's uploads" into "every statement ever uploaded by anyone".
    """
    cutoff = _iso(_now() - timedelta(hours=hours))
    with connect() as conn:
        if user_id is None:
            rows = conn.execute(
                "SELECT id FROM statements WHERE uploaded_at >= ? AND user_id IS NULL"
                " ORDER BY uploaded_at",
                (cutoff,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM statements WHERE uploaded_at >= ? AND user_id = ?"
                " ORDER BY uploaded_at",
                (cutoff, user_id),
            ).fetchall()
    return [r["id"] for r in rows]


def delete_statement(sid: str) -> bool:
    path = source_path(sid)
    with connect() as conn:
        cur = conn.execute("DELETE FROM statements WHERE id = ?", (sid,))
        deleted = cur.rowcount > 0
    if path:
        path.unlink(missing_ok=True)
    return deleted


def carriers() -> list[str]:
    """Distinct carriers present in history, for the history filter."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT carrier FROM statements WHERE carrier <> ''"
            " ORDER BY carrier"
        ).fetchall()
    return [r["carrier"] for r in rows]
