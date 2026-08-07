"""
App-owned state: mailbox access grants, department mailboxes, admins,
review tracking, login rate-limiting. Lives entirely in SQLite — never
touches 3CX's database.
"""
import json
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_mailbox_grant (
    viewer_extension TEXT NOT NULL,
    mailbox_extension TEXT NOT NULL,
    hide_review_status INTEGER NOT NULL DEFAULT 0,
    mwi_suppress INTEGER NOT NULL DEFAULT 0,
    notify_suppress INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (viewer_extension, mailbox_extension)
);

CREATE TABLE IF NOT EXISTS department_mailbox (
    department TEXT NOT NULL,
    mailbox_extension TEXT NOT NULL,
    PRIMARY KEY (department, mailbox_extension)
);

CREATE TABLE IF NOT EXISTS department_mailbox_exclusion (
    department TEXT NOT NULL,
    mailbox_extension TEXT NOT NULL,
    member_extension TEXT NOT NULL,
    PRIMARY KEY (department, mailbox_extension, member_extension)
);

CREATE TABLE IF NOT EXISTS department_mailbox_review_hide (
    department TEXT NOT NULL,
    mailbox_extension TEXT NOT NULL,
    member_extension TEXT NOT NULL,
    PRIMARY KEY (department, mailbox_extension, member_extension)
);

CREATE TABLE IF NOT EXISTS department_mailbox_mwi_suppress (
    department TEXT NOT NULL,
    mailbox_extension TEXT NOT NULL,
    member_extension TEXT NOT NULL,
    PRIMARY KEY (department, mailbox_extension, member_extension)
);

CREATE TABLE IF NOT EXISTS department_mailbox_notify_suppress (
    department TEXT NOT NULL,
    mailbox_extension TEXT NOT NULL,
    member_extension TEXT NOT NULL,
    PRIMARY KEY (department, mailbox_extension, member_extension)
);

CREATE TABLE IF NOT EXISTS app_admin (
    extension TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS hidden_extension (
    extension TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS message_review (
    message_id INTEGER NOT NULL,
    extension TEXT,
    reviewed_by TEXT NOT NULL,
    reviewed_at TEXT,
    note TEXT,
    PRIMARY KEY (message_id, reviewed_by)
);

CREATE TABLE IF NOT EXISTS heard_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    extension TEXT NOT NULL,
    set_to INTEGER NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extension TEXT NOT NULL,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL
);

-- Single-row table (id is always 1) for the Settings > Transcription tab.
-- engine is 'local' (faster-whisper, in-process on this server) or 'openai'
-- (sends the recording to OpenAI's API) -- see app/transcription.py.
CREATE TABLE IF NOT EXISTS transcription_setting (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    engine TEXT NOT NULL DEFAULT 'local'
);

-- Word-level timestamps for the karaoke-style transcript (click a word to
-- seek the player, highlight the word as it plays). Lives here, not on
-- 3CX's s_voicemail row -- that table only has a plain-text transcription
-- column, and this is app-only display data 3CX has no use for. One row
-- per message that got word timestamps -- not every transcription has them
-- (see transcription.py: OpenAI only provides them for the "whisper-1"
-- model), so a missing row just means the frontend falls back to plain,
-- unclickable text.
CREATE TABLE IF NOT EXISTS transcription_words (
    message_id INTEGER PRIMARY KEY,
    words_json TEXT NOT NULL
);

-- This app's own notion of "user" (keyed by extension, like everything else
-- here) -- currently just an email address, seeded from 3CX's own
-- voicemail.email (see threecx_db.mailbox_email) but overridable by an
-- admin. email_source distinguishes the two so a later 3CX-side sync never
-- clobbers a deliberate manual override. Doubles as the future Entra SSO
-- extension<->signin match key -- email is the one identifier both sides
-- are expected to agree on.
CREATE TABLE IF NOT EXISTS app_user (
    extension TEXT PRIMARY KEY,
    email TEXT,
    email_source TEXT NOT NULL DEFAULT '3cx',
    updated_at TEXT NOT NULL
);

-- Single-row table (id is always 1) for the Settings > Notifications tab.
CREATE TABLE IF NOT EXISTS notification_setting (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    smtp_enabled INTEGER NOT NULL DEFAULT 0,
    pwa_enabled INTEGER NOT NULL DEFAULT 0
);

-- One row per browser/device a staff member has enabled push notifications
-- on -- a person can have several (phone + desktop), so this isn't keyed
-- by extension alone.
CREATE TABLE IF NOT EXISTS push_subscription (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extension TEXT NOT NULL,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Single-row table tracking the voicemail watcher's high-water mark, so a
-- restart resumes from where it left off instead of re-dispatching every
-- historical message as "new" (see voicemail_watcher.py).
CREATE TABLE IF NOT EXISTS watcher_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_seen_id INTEGER NOT NULL DEFAULT 0
);

-- One row per registered scheduler.py job, tracking the local calendar date
-- it last ran on -- so a restart (or the scheduler loop just waking up late)
-- never double-fires a job that already ran today, and a missed tick (e.g.
-- the service was down at 07:00) still catches up the same day it comes
-- back, rather than silently skipping to tomorrow.
CREATE TABLE IF NOT EXISTS scheduler_job_state (
    job_name TEXT PRIMARY KEY,
    last_run_date TEXT
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.APP_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        _migrate_message_review(conn)
        conn.executescript(SCHEMA)
        _migrate_user_mailbox_grant(conn)
        for ext in config.ADMIN_EXTENSIONS:
            conn.execute(
                "INSERT OR IGNORE INTO app_admin (extension) VALUES (?)", (ext,)
            )
        # Starts disabled -- an admin has to explicitly opt in via the
        # Settings > Transcription tab (see routes/admin.py), so a fresh
        # install never silently starts running CPU-heavy local transcription
        # or sending call audio to a third party.
        conn.execute(
            "INSERT OR IGNORE INTO transcription_setting (id, enabled, engine) VALUES (1, 0, 'local')"
        )
        # Both start disabled -- same reasoning as transcription_setting
        # above: a fresh install should never silently start emailing or
        # push-notifying people until an admin opts in.
        conn.execute(
            "INSERT OR IGNORE INTO notification_setting (id, smtp_enabled, pwa_enabled) VALUES (1, 0, 0)"
        )
        # last_seen_id starts at 0, which voicemail_watcher.py treats as
        # "never initialized" and fast-forwards to 3CX's current max id --
        # see that module for why this can't be seeded here instead.
        conn.execute(
            "INSERT OR IGNORE INTO watcher_state (id, last_seen_id) VALUES (1, 0)"
        )


def _migrate_message_review(conn):
    """message_review used to key on message_id alone (first-reviewer-wins:
    only whoever reviewed a message first was ever recorded). Now that every
    viewer with mailbox access gets their own review row, the primary key
    becomes (message_id, reviewed_by). Rebuild the table in place if the old
    single-column PK is still present, preserving existing rows."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='message_review'"
    ).fetchone()
    if row is None or "PRIMARY KEY (message_id, reviewed_by)" in row["sql"]:
        return
    conn.executescript(
        """
        ALTER TABLE message_review RENAME TO message_review_old;
        CREATE TABLE message_review (
            message_id INTEGER NOT NULL,
            extension TEXT,
            reviewed_by TEXT NOT NULL,
            reviewed_at TEXT,
            note TEXT,
            PRIMARY KEY (message_id, reviewed_by)
        );
        INSERT INTO message_review (message_id, extension, reviewed_by, reviewed_at, note)
            SELECT message_id, extension, reviewed_by, reviewed_at, note
            FROM message_review_old
            WHERE reviewed_by IS NOT NULL;
        DROP TABLE message_review_old;
        """
    )


def _migrate_user_mailbox_grant(conn):
    """user_mailbox_grant predates hide_review_status/mwi_suppress/
    notify_suppress — add the columns for DB files created before they
    existed. New DBs get them from SCHEMA above, so this is a no-op for
    them."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(user_mailbox_grant)")}
    if "hide_review_status" not in cols:
        conn.execute(
            "ALTER TABLE user_mailbox_grant ADD COLUMN hide_review_status INTEGER NOT NULL DEFAULT 0"
        )
    if "mwi_suppress" not in cols:
        conn.execute(
            "ALTER TABLE user_mailbox_grant ADD COLUMN mwi_suppress INTEGER NOT NULL DEFAULT 0"
        )
    if "notify_suppress" not in cols:
        conn.execute(
            "ALTER TABLE user_mailbox_grant ADD COLUMN notify_suppress INTEGER NOT NULL DEFAULT 0"
        )


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


# --- admin -------------------------------------------------------------------
def is_admin(extension: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM app_admin WHERE extension = ?", (extension,)
        ).fetchone()
        return row is not None


def admin_extensions() -> set[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT extension FROM app_admin").fetchall()
        return {r["extension"] for r in rows}


# --- hidden extensions (Users tab: "Hide extension from the app") ------------
def hidden_extensions() -> set[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT extension FROM hidden_extension").fetchall()
        return {r["extension"] for r in rows}


def set_hidden(extension: str, hidden: bool):
    with get_conn() as conn:
        if hidden:
            conn.execute(
                "INSERT OR IGNORE INTO hidden_extension (extension) VALUES (?)", (extension,)
            )
        else:
            conn.execute("DELETE FROM hidden_extension WHERE extension = ?", (extension,))


# --- login rate limiting ------------------------------------------------------
def record_login_attempt(extension: str, ok: bool):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO login_attempt (extension, ts, ok) VALUES (?, ?, ?)",
            (extension, now_iso(), 1 if ok else 0),
        )


def is_locked_out(extension: str) -> bool:
    cutoff = time.gmtime(time.time() - config.LOGIN_LOCKOUT_MINUTES * 60)
    cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%S", cutoff)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ok FROM login_attempt WHERE extension = ? AND ts > ? ORDER BY ts DESC LIMIT ?",
            (extension, cutoff_iso, config.LOGIN_MAX_ATTEMPTS),
        ).fetchall()
        if len(rows) < config.LOGIN_MAX_ATTEMPTS:
            return False
        return all(r["ok"] == 0 for r in rows)


# --- user mailbox grants (Users tab: explicit per-user extra access) ---------
def grants_for_viewer(extension: str) -> list[str]:
    with get_conn() as conn:
        return [
            r["mailbox_extension"]
            for r in conn.execute(
                "SELECT mailbox_extension FROM user_mailbox_grant WHERE viewer_extension = ?",
                (extension,),
            )
        ]


def grants_with_status_for_viewer(extension: str) -> list[dict]:
    """[{mailbox_extension, hide_review_status, mwi_suppress, notify_suppress}]
    for the Users tab editor — hide_review_status lets a grant see a
    mailbox's messages without the mailbox owner seeing that this viewer
    already reviewed them; mwi_suppress opts a granted group-mailbox viewer
    out of that mailbox's light on their own phone (see mwi_relay.py);
    notify_suppress opts them out of SMTP/push notifications for that
    mailbox (see notifications.py) -- neither affects their view access."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mailbox_extension, hide_review_status, mwi_suppress, notify_suppress FROM user_mailbox_grant WHERE viewer_extension = ?",
            (extension,),
        ).fetchall()
        return [
            {
                "mailbox_extension": r["mailbox_extension"],
                "hide_review_status": bool(r["hide_review_status"]),
                "mwi_suppress": bool(r["mwi_suppress"]),
                "notify_suppress": bool(r["notify_suppress"]),
            }
            for r in rows
        ]


def set_grants_for_viewer(extension: str, grants: list[dict]):
    """grants: [{mailbox_extension, hide_review_status, mwi_suppress, notify_suppress}]"""
    with get_conn() as conn:
        conn.execute("DELETE FROM user_mailbox_grant WHERE viewer_extension = ?", (extension,))
        conn.executemany(
            """
            INSERT INTO user_mailbox_grant (viewer_extension, mailbox_extension, hide_review_status, mwi_suppress, notify_suppress)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    extension,
                    g["mailbox_extension"],
                    1 if g["hide_review_status"] else 0,
                    1 if g.get("mwi_suppress") else 0,
                    1 if g.get("notify_suppress") else 0,
                )
                for g in grants
            ],
        )


def hidden_review_viewers_for_mailbox(mailbox_extension: str) -> set[str]:
    """Viewer extensions granted this mailbox with hide_review_status set —
    their review activity should stay invisible to the mailbox owner."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT viewer_extension FROM user_mailbox_grant WHERE mailbox_extension = ? AND hide_review_status = 1",
            (mailbox_extension,),
        ).fetchall()
        return {r["viewer_extension"] for r in rows}


def mwi_suppressed_viewers_for_mailbox(mailbox_extension: str) -> set[str]:
    """Viewer extensions granted this mailbox with mwi_suppress set — see
    mwi_relay.py's resolve_targets, which excludes them from that group
    mailbox's phone-light fan-out."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT viewer_extension FROM user_mailbox_grant WHERE mailbox_extension = ? AND mwi_suppress = 1",
            (mailbox_extension,),
        ).fetchall()
        return {r["viewer_extension"] for r in rows}


def notify_suppressed_viewers_for_mailbox(mailbox_extension: str) -> set[str]:
    """Viewer extensions granted this mailbox with notify_suppress set —
    excluded from that group mailbox's SMTP/push notification fan-out (see
    access.notification_recipients_for_mailbox) without affecting their
    view access."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT viewer_extension FROM user_mailbox_grant WHERE mailbox_extension = ? AND notify_suppress = 1",
            (mailbox_extension,),
        ).fetchall()
        return {r["viewer_extension"] for r in rows}


def viewers_granted_for_mailbox(mailbox_extension: str) -> list[str]:
    with get_conn() as conn:
        return [
            r["viewer_extension"]
            for r in conn.execute(
                "SELECT viewer_extension FROM user_mailbox_grant WHERE mailbox_extension = ?",
                (mailbox_extension,),
            )
        ]


def add_grant(
    viewer_extension: str,
    mailbox_extension: str,
    hide_review_status: bool = False,
    mwi_suppress: bool = False,
    notify_suppress: bool = False,
):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_mailbox_grant (viewer_extension, mailbox_extension, hide_review_status, mwi_suppress, notify_suppress)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(viewer_extension, mailbox_extension) DO UPDATE SET
                hide_review_status = excluded.hide_review_status,
                mwi_suppress = excluded.mwi_suppress,
                notify_suppress = excluded.notify_suppress
            """,
            (
                viewer_extension,
                mailbox_extension,
                1 if hide_review_status else 0,
                1 if mwi_suppress else 0,
                1 if notify_suppress else 0,
            ),
        )


def remove_grant(viewer_extension: str, mailbox_extension: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM user_mailbox_grant WHERE viewer_extension = ? AND mailbox_extension = ?",
            (viewer_extension, mailbox_extension),
        )


# --- department mailboxes (Departments tab) -----------------------------------
def department_mailboxes(department: str) -> list[str]:
    with get_conn() as conn:
        return [
            r["mailbox_extension"]
            for r in conn.execute(
                "SELECT mailbox_extension FROM department_mailbox WHERE department = ?",
                (department,),
            )
        ]


def all_department_mailboxes() -> dict[str, list[str]]:
    """department -> [mailbox_extension, ...] for every department with any assignment."""
    with get_conn() as conn:
        rows = conn.execute("SELECT department, mailbox_extension FROM department_mailbox").fetchall()
    result = defaultdict(list)
    for r in rows:
        result[r["department"]].append(r["mailbox_extension"])
    return result


def set_department_mailboxes(department: str, mailboxes: list[str]):
    with get_conn() as conn:
        conn.execute("DELETE FROM department_mailbox WHERE department = ?", (department,))
        conn.executemany(
            "INSERT INTO department_mailbox (department, mailbox_extension) VALUES (?, ?)",
            [(department, m) for m in mailboxes],
        )
        # Dropping a mailbox's department designation makes its exclusion
        # (and review-hide) rows meaningless — clear them so a later re-add
        # starts clean.
        conn.execute(
            """
            DELETE FROM department_mailbox_exclusion
            WHERE department = ?
              AND mailbox_extension NOT IN (
                  SELECT mailbox_extension FROM department_mailbox WHERE department = ?
              )
            """,
            (department, department),
        )
        conn.execute(
            """
            DELETE FROM department_mailbox_review_hide
            WHERE department = ?
              AND mailbox_extension NOT IN (
                  SELECT mailbox_extension FROM department_mailbox WHERE department = ?
              )
            """,
            (department, department),
        )
        conn.execute(
            """
            DELETE FROM department_mailbox_mwi_suppress
            WHERE department = ?
              AND mailbox_extension NOT IN (
                  SELECT mailbox_extension FROM department_mailbox WHERE department = ?
              )
            """,
            (department, department),
        )
        conn.execute(
            """
            DELETE FROM department_mailbox_notify_suppress
            WHERE department = ?
              AND mailbox_extension NOT IN (
                  SELECT mailbox_extension FROM department_mailbox WHERE department = ?
              )
            """,
            (department, department),
        )
        # Designating a mailbox as a department's shared mailbox retires it as
        # an individually-delegated personal mailbox, so auto-check the same
        # "hidden" flag the Users tab checkbox controls.
        conn.executemany(
            "INSERT OR IGNORE INTO hidden_extension (extension) VALUES (?)",
            [(m,) for m in mailboxes],
        )


def excluded_members(department: str, mailbox_extension: str) -> list[str]:
    with get_conn() as conn:
        return [
            r["member_extension"]
            for r in conn.execute(
                "SELECT member_extension FROM department_mailbox_exclusion WHERE department = ? AND mailbox_extension = ?",
                (department, mailbox_extension),
            )
        ]


def set_excluded_members(department: str, mailbox_extension: str, members: list[str]):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM department_mailbox_exclusion WHERE department = ? AND mailbox_extension = ?",
            (department, mailbox_extension),
        )
        conn.executemany(
            "INSERT INTO department_mailbox_exclusion (department, mailbox_extension, member_extension) VALUES (?, ?, ?)",
            [(department, mailbox_extension, m) for m in members],
        )


def hidden_review_members(department: str, mailbox_extension: str) -> list[str]:
    """Implicit (non-excluded) department members whose review activity on
    this mailbox stays invisible to the mailbox owner — the department-tab
    counterpart of a grant's hide_review_status."""
    with get_conn() as conn:
        return [
            r["member_extension"]
            for r in conn.execute(
                "SELECT member_extension FROM department_mailbox_review_hide WHERE department = ? AND mailbox_extension = ?",
                (department, mailbox_extension),
            )
        ]


def set_hidden_review_members(department: str, mailbox_extension: str, members: list[str]):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM department_mailbox_review_hide WHERE department = ? AND mailbox_extension = ?",
            (department, mailbox_extension),
        )
        conn.executemany(
            "INSERT INTO department_mailbox_review_hide (department, mailbox_extension, member_extension) VALUES (?, ?, ?)",
            [(department, mailbox_extension, m) for m in members],
        )


def mwi_suppressed_members(department: str, mailbox_extension: str) -> list[str]:
    """Implicit (non-excluded) department members who opted out of this
    group mailbox's light on their own phone — the department-tab
    counterpart of a grant's mwi_suppress. See mwi_relay.py's
    resolve_targets, which excludes them from the fan-out."""
    with get_conn() as conn:
        return [
            r["member_extension"]
            for r in conn.execute(
                "SELECT member_extension FROM department_mailbox_mwi_suppress WHERE department = ? AND mailbox_extension = ?",
                (department, mailbox_extension),
            )
        ]


def set_mwi_suppressed_members(department: str, mailbox_extension: str, members: list[str]):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM department_mailbox_mwi_suppress WHERE department = ? AND mailbox_extension = ?",
            (department, mailbox_extension),
        )
        conn.executemany(
            "INSERT INTO department_mailbox_mwi_suppress (department, mailbox_extension, member_extension) VALUES (?, ?, ?)",
            [(department, mailbox_extension, m) for m in members],
        )


def notify_suppressed_members(department: str, mailbox_extension: str) -> list[str]:
    """Implicit (non-excluded) department members who opted out of this
    group mailbox's SMTP/push notifications — the department-tab
    counterpart of a grant's notify_suppress. See
    access.notification_recipients_for_mailbox."""
    with get_conn() as conn:
        return [
            r["member_extension"]
            for r in conn.execute(
                "SELECT member_extension FROM department_mailbox_notify_suppress WHERE department = ? AND mailbox_extension = ?",
                (department, mailbox_extension),
            )
        ]


def set_notify_suppressed_members(department: str, mailbox_extension: str, members: list[str]):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM department_mailbox_notify_suppress WHERE department = ? AND mailbox_extension = ?",
            (department, mailbox_extension),
        )
        conn.executemany(
            "INSERT INTO department_mailbox_notify_suppress (department, mailbox_extension, member_extension) VALUES (?, ?, ?)",
            [(department, mailbox_extension, m) for m in members],
        )


# --- review tracking (one row per message per reviewer) -----------------------
def mark_reviewed(message_id: int, extension: str, reviewer: str):
    now = now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO message_review (message_id, extension, reviewed_by, reviewed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(message_id, reviewed_by) DO UPDATE SET reviewed_at = excluded.reviewed_at
            """,
            (message_id, extension, reviewer, now),
        )


def remove_review(message_id: int, reviewer: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM message_review WHERE message_id = ? AND reviewed_by = ?",
            (message_id, reviewer),
        )


def get_reviews(message_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM message_review WHERE message_id = ? ORDER BY reviewed_at", (message_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def reviews_for_mailbox(extension: str) -> dict:
    """message_id -> [review dict, ...], for a mailbox's messages."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM message_review WHERE extension = ?", (extension,)
        ).fetchall()
    result = defaultdict(list)
    for r in rows:
        result[r["message_id"]].append(dict(r))
    return result


# --- transcription settings (Settings > Transcription tab) --------------------
def get_transcription_settings() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT enabled, engine FROM transcription_setting WHERE id = 1"
        ).fetchone()
        return {"enabled": bool(row["enabled"]), "engine": row["engine"]}


def set_transcription_settings(enabled: bool, engine: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE transcription_setting SET enabled = ?, engine = ? WHERE id = 1",
            (1 if enabled else 0, engine),
        )


# --- transcription word timestamps (karaoke-style transcript) -----------------
def set_transcription_words(message_id: int, words: list[dict]):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO transcription_words (message_id, words_json) VALUES (?, ?)
            ON CONFLICT(message_id) DO UPDATE SET words_json = excluded.words_json
            """,
            (message_id, json.dumps(words)),
        )


def transcription_words_for_messages(message_ids: list[int]) -> dict[int, list]:
    """message_id -> [{word, start, end}, ...] for whichever of message_ids
    got word timestamps. Bulk (not one query per message) for the same
    reason app_db.reviews_for_mailbox is -- this backs a per-mailbox message
    list."""
    if not message_ids:
        return {}
    with get_conn() as conn:
        placeholders = ",".join("?" * len(message_ids))
        rows = conn.execute(
            f"SELECT message_id, words_json FROM transcription_words WHERE message_id IN ({placeholders})",
            message_ids,
        ).fetchall()
        return {r["message_id"]: json.loads(r["words_json"]) for r in rows}


# --- app_user (extension -> email) --------------------------------------------
def get_user_email(extension: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT email FROM app_user WHERE extension = ?", (extension,)
        ).fetchone()
        return row["email"] if row else None


def set_user_email(extension: str, email: str | None):
    """Admin override via the Users tab -- marks the row 'manual' so a later
    sync_user_email_from_3cx call won't silently overwrite it."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_user (extension, email, email_source, updated_at)
            VALUES (?, ?, 'manual', ?)
            ON CONFLICT(extension) DO UPDATE SET email = excluded.email, email_source = 'manual', updated_at = excluded.updated_at
            """,
            (extension, email, now_iso()),
        )


def sync_user_email_from_3cx(extension: str, email: str | None):
    """Called when notifications.py resolves a recipient address and finds
    no cached row yet, or an existing 'auto'-sourced one -- never overwrites
    a 'manual' admin override."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_user (extension, email, email_source, updated_at)
            VALUES (?, ?, '3cx', ?)
            ON CONFLICT(extension) DO UPDATE SET email = excluded.email, updated_at = excluded.updated_at
            WHERE app_user.email_source = '3cx'
            """,
            (extension, email, now_iso()),
        )


def all_user_emails() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT extension, email FROM app_user").fetchall()
        return {r["extension"]: r["email"] for r in rows}


# --- notification settings (Settings > Notifications tab) ---------------------
def get_notification_settings() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT smtp_enabled, pwa_enabled FROM notification_setting WHERE id = 1"
        ).fetchone()
        return {"smtp_enabled": bool(row["smtp_enabled"]), "pwa_enabled": bool(row["pwa_enabled"])}


def set_notification_settings(smtp_enabled: bool, pwa_enabled: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE notification_setting SET smtp_enabled = ?, pwa_enabled = ? WHERE id = 1",
            (1 if smtp_enabled else 0, 1 if pwa_enabled else 0),
        )


# --- push subscriptions ---------------------------------------------------------
def add_push_subscription(extension: str, endpoint: str, p256dh: str, auth: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO push_subscription (extension, endpoint, p256dh, auth, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET extension = excluded.extension, p256dh = excluded.p256dh, auth = excluded.auth
            """,
            (extension, endpoint, p256dh, auth, now_iso()),
        )


def remove_push_subscription(endpoint: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM push_subscription WHERE endpoint = ?", (endpoint,))


def push_subscriptions_for_extension(extension: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscription WHERE extension = ?", (extension,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- voicemail watcher high-water mark ------------------------------------------
def get_watcher_last_seen_id() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT last_seen_id FROM watcher_state WHERE id = 1").fetchone()
        return row["last_seen_id"]


def set_watcher_last_seen_id(message_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE watcher_state SET last_seen_id = ? WHERE id = 1", (message_id,))


# --- scheduler job state ---------------------------------------------------
def get_job_last_run_date(job_name: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_run_date FROM scheduler_job_state WHERE job_name = ?", (job_name,)
        ).fetchone()
        return row["last_run_date"] if row else None


def set_job_last_run_date(job_name: str, date_str: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO scheduler_job_state (job_name, last_run_date) VALUES (?, ?) "
            "ON CONFLICT(job_name) DO UPDATE SET last_run_date = excluded.last_run_date",
            (job_name, date_str),
        )


# --- heard audit ---------------------------------------------------------------
def record_heard_audit(message_id: int, extension: str, set_to: bool):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO heard_audit (message_id, extension, set_to, ts) VALUES (?, ?, ?, ?)",
            (message_id, extension, 1 if set_to else 0, now_iso()),
        )
