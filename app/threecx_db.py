"""
Read-only access to the 3CX PostgreSQL database. Marking a message
heard/unheard is NOT done here — a direct SQL write flips the row without
3CX itself ever recognizing the change (no SIP NOTIFY, stale until a service
reload). That write happens exclusively via the native RPC in
threecx_notify.py, which impersonates the mailbox owner and calls 3CX's own
MyPhone RPC, so 3CX's own service performs the write and fires the NOTIFY.
"""
import json

import psycopg2
import psycopg2.extras

from . import config


def crm_display_name(crm_contact_raw):
    """Builds "<FirstName LastName> - <CompanyName>" from the crm_contact
    JSON blob 3CX stores on s_voicemail, or None if there's no usable name
    (missing/malformed JSON, or FirstName+LastName both blank) so callers can
    fall back to caller_name."""
    if not crm_contact_raw:
        return None
    try:
        contact = json.loads(crm_contact_raw)
    except (json.JSONDecodeError, TypeError):
        return None
    full_name = f"{(contact.get('FirstName') or '').strip()} {(contact.get('LastName') or '').strip()}".strip()
    if not full_name:
        return None
    company = (contact.get('CompanyName') or '').strip()
    return f"{full_name} - {company}" if company else full_name


def get_conn():
    conn = psycopg2.connect(config.DATABASE_URL)
    conn.autocommit = True
    return conn


# --- Q1: authenticate extension + PIN ---------------------------------------
Q1_AUTHENTICATE = """
SELECT u.iduser,
       d.value        AS extension,
       u.firstname,
       u.lastname
FROM voicemail v
JOIN users      u ON v.fkiduser      = u.iduser
JOIN extension  e ON u.fkidextension = e.fkiddn
JOIN dn         d ON e.fkiddn         = d.iddn
WHERE d.value      = %(extension)s
  AND v.pinnumber  = %(pin)s
  AND v.enablevm   = true;
"""


def authenticate(extension: str, pin: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q1_AUTHENTICATE, {"extension": extension, "pin": pin})
        return cur.fetchone()


# --- Q2: directory of voicemail-enabled mailboxes, with department ----------
Q2_DIRECTORY = """
SELECT d.value AS extension, u.firstname, u.lastname, g.name AS department, v.email
FROM voicemail v
JOIN users      u  ON v.fkiduser      = u.iduser
JOIN extension  e  ON u.fkidextension = e.fkiddn
JOIN dn         d  ON e.fkiddn        = d.iddn
LEFT JOIN dngrp dg ON dg.fkiddn       = d.iddn AND dg.primarygroup = true
LEFT JOIN grp   g  ON g.fkiddn        = dg.fkidgrp
WHERE v.enablevm = true
ORDER BY g.name, d.value;
"""


def directory():
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q2_DIRECTORY)
        return cur.fetchall()


# --- Q3: list messages for a mailbox -----------------------------------------
Q3_MESSAGES = """
SELECT id, wav_file, caller, caller_name, callee,
       duration, created_time, heard, heard_time,
       forwarded_by, forwarded_to, crm_contact, transcription
FROM s_voicemail
WHERE callee = %(extension)s
  AND (removed IS NULL OR removed = '')
ORDER BY created_time DESC;
"""


def messages_for_mailbox(extension: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q3_MESSAGES, {"extension": extension})
        return cur.fetchall()


# --- Q5: new messages since a given id, across all mailboxes ----------------
# Backs voicemail_watcher.py's poll loop. Ordered ascending (oldest first) so
# handlers see messages in the order they actually arrived.
Q5_NEW_SINCE = """
SELECT id, wav_file, caller, caller_name, callee, duration, created_time
FROM s_voicemail
WHERE id > %(since_id)s
  AND (removed IS NULL OR removed = '')
ORDER BY id ASC;
"""


def messages_since(since_id: int):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q5_NEW_SINCE, {"since_id": since_id})
        return cur.fetchall()


def max_message_id() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM s_voicemail;")
        return cur.fetchone()[0]


# --- Q17: cheap unread count for a mailbox, for mwi_relay's poll loop -------
# Same heard/removed rules as Q3, but COUNT-only -- Q3 pulls every column
# (wav_file, transcription text, ...) which is wasteful when polled every
# few seconds across many mailboxes just to get one number.
Q17_UNREAD_COUNT = """
SELECT COUNT(*) FROM s_voicemail
WHERE callee = %(extension)s
  AND (removed IS NULL OR removed = '')
  AND (heard IS NULL OR heard IN ('', '0'));
"""


def unread_count_for_mailbox(extension: str) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(Q17_UNREAD_COUNT, {"extension": extension})
        return cur.fetchone()[0]


# --- Q4: single message (for audio path lookup + authz) ---------------------
# caller_name/created_time/duration/transcription are additive on top of the
# original id/wav_file/callee/heard/caller -- added for the Yealink detail
# screen (routes/yealink.py) so it doesn't need a second query; existing
# callers only ever index this dict by key, so they're unaffected.
Q4_MESSAGE = """
SELECT id, wav_file, callee, heard, heard_time, caller,
       caller_name, created_time, duration, transcription, crm_contact
FROM s_voicemail
WHERE id = %(id)s;
"""


def get_message(message_id: int):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q4_MESSAGE, {"id": message_id})
        return cur.fetchone()


# --- Q6: caller's primary department + role ----------------------------------
Q6_DEPARTMENT_ROLE = """
SELECT g.name AS department, dg.roletag
FROM users u
JOIN extension e  ON u.fkidextension = e.fkiddn
JOIN dn        d  ON e.fkiddn        = d.iddn
JOIN dngrp     dg ON dg.fkiddn       = d.iddn AND dg.primarygroup = true
JOIN grp       g  ON g.fkiddn        = dg.fkidgrp
WHERE d.value = %(extension)s;
"""


def department_and_role(extension: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q6_DEPARTMENT_ROLE, {"extension": extension})
        return cur.fetchone()


# --- Q8: department + role for every extension (bulk form of Q6) ------------
# Used to compute a mailbox's expected-reviewer list (access.viewers_for_mailbox)
# without an N+1 query per directory entry.
Q8_ALL_ROLES = """
SELECT d.value AS extension, g.name AS department, dg.roletag
FROM users u
JOIN extension e  ON u.fkidextension = e.fkiddn
JOIN dn        d  ON e.fkiddn        = d.iddn
JOIN dngrp     dg ON dg.fkiddn       = d.iddn AND dg.primarygroup = true
JOIN grp       g  ON g.fkiddn        = dg.fkidgrp;
"""


def all_roles():
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q8_ALL_ROLES)
        return cur.fetchall()


# --- Q7: all voicemail-enabled extensions in a department --------------------
Q7_DEPARTMENT_EXTENSIONS = """
SELECT d.value AS extension, u.firstname, u.lastname
FROM voicemail v
JOIN users     u  ON v.fkiduser      = u.iduser
JOIN extension e  ON u.fkidextension = e.fkiddn
JOIN dn        d  ON e.fkiddn        = d.iddn
JOIN dngrp     dg ON dg.fkiddn       = d.iddn AND dg.primarygroup = true
JOIN grp       g  ON g.fkiddn        = dg.fkidgrp
WHERE v.enablevm = true
  AND g.name = %(department)s;
"""


def department_extensions(department: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q7_DEPARTMENT_EXTENSIONS, {"department": department})
        return cur.fetchall()


# --- Q11: mailbox owner's email (3CX's own voicemail-to-email address) ------
# Lives on `voicemail`, not `users` -- this is the address 3CX's own
# voicemail-to-email feature would send to, keyed off the same voicemail
# settings row Q1/Q2 already join through.
Q11_MAILBOX_EMAIL = """
SELECT d.value AS extension, v.email
FROM voicemail v
JOIN users     u ON v.fkiduser      = u.iduser
JOIN extension e ON u.fkidextension = e.fkiddn
JOIN dn        d ON e.fkiddn        = d.iddn
WHERE d.value = %(extension)s;
"""


def mailbox_email(extension: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q11_MAILBOX_EMAIL, {"extension": extension})
        return cur.fetchone()


# --- Q13: extension by email (MS Auth sign-in) -------------------------------
# Matches an Entra ID UPN/email claim back to a 3CX extension via the same
# voicemail.email field Q2's directory surfaces. Case-insensitive since
# email/UPN comparisons conventionally are. enablevm = true mirrors Q2 --
# an extension with voicemail disabled was never a candidate for the Users
# directory either.
Q13_BY_EMAIL = """
SELECT d.value AS extension, u.iduser, u.firstname, u.lastname
FROM voicemail v
JOIN users      u  ON v.fkiduser      = u.iduser
JOIN extension  e  ON u.fkidextension = e.fkiddn
JOIN dn         d  ON e.fkiddn        = d.iddn
WHERE v.enablevm = true
  AND lower(v.email) = lower(%(email)s);
"""


def by_email(email: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q13_BY_EMAIL, {"email": email})
        return cur.fetchone()


# --- Q14: extension by extension number (MS Auth override sign-in) ----------
# Backs MS_AUTH_OVERRIDE_EXTENSION/MS_AUTH_OVERRIDE_EMAILS in config.py: a
# handful of individually-owned Entra ID accounts (e.g. several admins) that
# should all sign in as one shared management extension rather than each
# needing their own voicemail.email match.
Q14_BY_EXTENSION = """
SELECT d.value AS extension, u.iduser, u.firstname, u.lastname
FROM voicemail v
JOIN users      u  ON v.fkiduser      = u.iduser
JOIN extension  e  ON u.fkidextension = e.fkiddn
JOIN dn         d  ON e.fkiddn        = d.iddn
WHERE v.enablevm = true
  AND d.value = %(extension)s;
"""


def by_extension(extension: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q14_BY_EXTENSION, {"extension": extension})
        return cur.fetchone()


# --- Q12: call-history rows for one mailbox (dnowner), newest first ---------
Q12_CALL_HISTORY = """
SELECT idmpch14, call_id, calltype, party_dn, party_dntype, party_name,
       party_callerid, start_time, established_time, end_time
FROM myphone_callhistory_v14
WHERE dnowner = %(extension)s
ORDER BY idmpch14 DESC;
"""


def call_history(extension: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q12_CALL_HISTORY, {"extension": extension})
        return cur.fetchall()


# --- Q13: call-history rows between one mailbox and one counterpart ---------
# counterpart_key is party_dn for internal rows, party_callerid for external
# rows (party_dntype & 128 = 128 means external) -- the route layer passes
# whichever key the clicked row identified as. External numbers are matched
# on their last 10 digits rather than exact string equality: 3CX logs an
# inbound trunk call's caller ID as a bare 10-digit number but an outbound
# dial to that same number as 11 digits with the leading "1" (and any number
# could show up with a "+", dashes, etc.), so an exact match silently misses
# real callbacks -- e.g. the "called back" phone icon (access.
# called_back_after_review) never lighting up for a legitimate callback.
Q13_CALL_HISTORY_WITH_COUNTERPART = """
SELECT idmpch14, call_id, calltype, party_dn, party_dntype, party_name,
       party_callerid, start_time, established_time, end_time
FROM myphone_callhistory_v14
WHERE dnowner = %(extension)s
  AND (
        (party_dntype & 128 = 128 AND RIGHT(regexp_replace(party_callerid, '\\D', '', 'g'), 10)
                                     = RIGHT(regexp_replace(%(counterpart_key)s, '\\D', '', 'g'), 10))
     OR (party_dntype & 128 != 128 AND party_dn = %(counterpart_key)s)
      )
ORDER BY idmpch14 DESC;
"""


def call_history_with_counterpart(extension: str, counterpart_key: str):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q13_CALL_HISTORY_WITH_COUNTERPART, {"extension": extension, "counterpart_key": counterpart_key})
        return cur.fetchall()


# --- Write: voicemail transcription -------------------------------------------
# Unlike `heard` (see module docstring), 3CX ties no SIP NOTIFY or other side
# effect to this column -- it's plain text 3CX's own webclient displays
# verbatim if present, regardless of whether 3CX's own transcription add-on
# wrote it or, as here, this app did. So a direct UPDATE is the correct write
# path here: there's no native RPC for it, and none is needed.
Q10_SET_TRANSCRIPTION = """
UPDATE s_voicemail SET transcription = %(text)s WHERE id = %(id)s;
"""


def set_transcription(message_id: int, text: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(Q10_SET_TRANSCRIPTION, {"id": message_id, "text": text})


# --- Write: voicemail PIN ------------------------------------------------------
# Same reasoning as set_transcription above: 3CX ties no side effect (no SIP
# NOTIFY, nothing else reads it live) to voicemail.pinnumber, and there's no
# native RPC that changes it -- that only exists in 3CX's management console,
# which isn't something this app impersonates. A direct UPDATE is therefore
# the correct write path, same as set_transcription.
Q16_SET_PIN = """
UPDATE voicemail v
SET pinnumber = %(pin)s
FROM users u, extension e, dn d
WHERE v.fkiduser = u.iduser
  AND u.fkidextension = e.fkiddn
  AND e.fkiddn = d.iddn
  AND d.value = %(extension)s;
"""


def set_pin(extension: str, new_pin: str) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(Q16_SET_PIN, {"extension": extension, "pin": new_pin})
        return cur.rowcount > 0


# --- Q18: call path a voicemail took to reach its mailbox --------------------
# s_voicemail.cdr_participant_id points at the cdroutput leg that actually
# answered into the mailbox (the divert/no_destinations leg landing on the
# voicemail DN). From there this walks cdroutput.continued_in_cdr_id
# backwards to the call's first leg (call_init), which reconstructs the
# sequence of extensions/queues/scripts 3CX actually routed through --
# the same chain the 3CX web client's Call Reports page displays, not the
# parallel hunt-group legs that were polled and rejected along the way.
# Call Flow Designer route points (digital receptionists) have no dn name of
# their own in cdroutput, so those fall back to the script's C# namespace
# pulled straight from routepoint.scriptcode.
Q18_CALL_PATH = """
WITH RECURSIVE landing AS (
    SELECT c.cdr_id, c.source_dn_number, c.source_dn_name,
           c.source_participant_phone_number, c.source_participant_name,
           c.destination_dn_number, c.destination_dn_name,
           c.cdr_started_at, c.cdr_ended_at,
           c.creation_method, c.termination_reason, c.termination_reason_details,
           c.continued_in_cdr_id
    FROM s_voicemail v
    JOIN cdroutput c ON c.destination_participant_id = v.cdr_participant_id
    WHERE v.id = %(message_id)s
),
chain AS (
    SELECT *, 1 AS depth FROM landing
    UNION ALL
    SELECT prev.cdr_id, prev.source_dn_number, prev.source_dn_name,
           prev.source_participant_phone_number, prev.source_participant_name,
           prev.destination_dn_number, prev.destination_dn_name,
           prev.cdr_started_at, prev.cdr_ended_at,
           prev.creation_method, prev.termination_reason, prev.termination_reason_details,
           prev.continued_in_cdr_id, chain.depth + 1
    FROM cdroutput prev
    JOIN chain ON prev.continued_in_cdr_id = chain.cdr_id
),
scriptnames AS (
    SELECT dn.value, (regexp_match(rp.scriptcode, 'namespace\\s+([A-Za-z0-9_]+)'))[1] AS script_name
    FROM dn JOIN routepoint rp ON rp.fkiddn = dn.iddn
)
SELECT DISTINCT ON (c.cdr_id)
       c.cdr_started_at, c.cdr_ended_at,
       c.source_dn_number, c.source_participant_phone_number, c.source_participant_name,
       COALESCE(NULLIF(c.source_dn_name, ''), sn1.script_name) AS source_name,
       c.destination_dn_number,
       COALESCE(NULLIF(c.destination_dn_name, ''), sn2.script_name) AS destination_name,
       c.creation_method, c.termination_reason, c.termination_reason_details
FROM chain c
LEFT JOIN scriptnames sn1 ON sn1.value = c.source_dn_number
LEFT JOIN scriptnames sn2 ON sn2.value = c.destination_dn_number
ORDER BY c.cdr_id, c.cdr_started_at;
"""


def call_path(message_id: int):
    """Returns {"origin": {...}, "hops": [...]}. For an external call, origin
    is the DID that was dialed (the call_init leg's destination, flagged by
    termination_reason_details == "by_did") -- not the caller's own phone
    number, and not the trunk DN (source_dn_number/name is always the trunk
    extension, e.g. "10000 Telnyx", for every leg of an inbound call, so
    neither is a routing step worth showing). For an internal call there's
    no DID, so origin falls back to the initiating extension. hops is the
    remaining chain of destinations the call was routed through, oldest
    first."""
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(Q18_CALL_PATH, {"message_id": message_id})
        rows = cur.fetchall()
    rows.sort(key=lambda r: r["cdr_started_at"])
    if not rows:
        return {"origin": None, "hops": []}
    first = rows[0]
    is_did_entry = first["creation_method"] == "call_init" and first["termination_reason_details"] == "by_did"
    if is_did_entry and len(rows) > 1:
        origin = {"number": first["destination_dn_number"], "name": first["destination_name"]}
        rows = rows[1:]
    else:
        origin = {
            "number": first["source_participant_phone_number"] or first["source_dn_number"],
            "name": first["source_participant_name"] or first["source_name"],
        }
    hops = [
        {
            "number": r["destination_dn_number"],
            "name": r["destination_name"],
            "creation_method": r["creation_method"],
            "termination_reason": r["termination_reason"],
            "termination_reason_details": r["termination_reason_details"],
            "cdr_started_at": r["cdr_started_at"],
            "cdr_ended_at": r["cdr_ended_at"],
        }
        for r in rows
    ]
    return {"origin": origin, "hops": hops}
