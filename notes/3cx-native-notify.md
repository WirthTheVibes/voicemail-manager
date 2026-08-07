# 3CX Voicemail Manager — Integration Notes

Reverse-engineered against 3CX v20.0.9.995 (Debian/self-hosted). Undocumented — may break on future 3CX updates.

## Goal
External app needs to:
1. Read/manage voicemails across arbitrary extensions (not just one mailbox)
2. Mark voicemails heard/unheard in a way that triggers 3CX's real MWI (phone light) update in real time
3. Auth completely decoupled from end-user identity (Entra handles user login separately; this uses one stored admin credential)

## Why direct DB writes don't work
- `s_voicemail` table has no Postgres trigger/LISTEN on it (confirmed via `pg_stat_activity` / statement logging).
- Writing `heard`/`heard_time` columns directly via SQL does **not** trigger the SIP NOTIFY to phones. The NOTIFY is fired by 3CX's application code as a side effect of specific API calls — not by the DB state itself.
- This is why the original workflow needed a full service reload to reflect changes.

## Auth flow (confirmed working end-to-end)

All calls are relative to `https://<pbx-domain>`.

### Step 1 — Admin login (once, refresh_token is long-lived ~90 days)
```
POST /webclient/api/Login/GetAccessToken
Content-Type: application/json

{"Username":"900","Password":"<admin_web_password>","SecurityCode":"","ReCaptchaResponse":null}
```
Response:
```json
{
  "Status": "AuthSuccess",
  "Token": {
    "access_token": "<JWT, 1hr>",
    "refresh_token": "<JWT, ~90 days>",
    "expires_in": 3600
  }
}
```
`900` = System Owner / top-level admin extension. Only admin-role accounts can impersonate.

### Step 2 — Impersonate target extension (per mailbox, as needed)
**Correction (2026-07-25):** earlier revisions of this doc said this call
needs `Authorization: Bearer <admin refresh_token>`. That's wrong and fails
with a bare `401 Content-Length: 0` (rejected by the auth middleware before
reaching app logic — no JSON error body, unlike a real app-level failure).
Confirmed via a HAR capture of the real webclient: **no Authorization
header, and no refresh_token anywhere in the request.** Step 1's response
sets an HttpOnly `RefreshTokenCookie` (not visible in a HAR unless "include
cookies" is enabled on export, and not part of the JSON body) — that cookie
is what authenticates this call. Use a single `requests.Session()` (or
equivalent cookie jar) across both steps, not stateless per-call requests.
```
POST /connect/token
Content-Type: application/x-www-form-urlencoded;charset=UTF-8
Cookie: RefreshTokenCookie=<set automatically by Step 1's response>

client_id=Webclient&grant_type=refresh_token&impersonate=<target_extension>
```
Response:
```json
{
  "token_type": "Bearer",
  "expires_in": 3600,
  "access_token": "<JWT scoped to target_extension>",
  "refresh_token": null
}
```
Decoded JWT claims include `unique_name` (target ext), `original_name` (admin ext), `role: ["Impersonate", ...]`.

### Step 3 — Create a MyPhone session using the impersonated token
```
POST /webclient/api/MyPhone/session
Content-Type: application/json
Authorization: Bearer <access_token from step 2>

{"name":"Webclient","version":"20.0.9.0","isHuman":true}
```
Response:
```json
{
  "sessionKey": "<uuid>",
  "pass": "<uuid>",
  ...
}
```
`sessionKey` = the value used as `MyPhoneSession` header on the legacy binary RPC endpoint below.

## Voicemail RPC (binary Protobuf-over-HTTP, `MPWebService.asmx`)

3CX's legacy MyPhone backend. All requests/responses are raw Protobuf, `Content-Type: application/octet-stream`, no JSON.

### Endpoint
```
POST /MyPhone/MPWebService.asmx
Content-Type: application/octet-stream
Accept: application/octet-stream
MyPhoneSession: <sessionKey from Step 3>
```

### General wire format
Every RPC follows the same envelope:
```
0x08 <method_id_byte>          -- field 1: method ID (varint, repeated as tag)
<tag for field=method_id, wiretype=2>
<length varint>
<payload bytes>
```

### Mark voicemail heard/unheard — method ID 114 (0x72)
Payload = `{ field1: voicemail_id (varint), field2: heard_bool (varint 0/1) }`

Example: mark voicemail id=6 as **heard**:
```
08 72 92 07 04 08 06 10 01
```
Mark id=6 as **unheard**: change trailing byte `01` → `00`.

For ids ≥ 128, the id byte needs proper varint multi-byte encoding (not yet implemented/tested).

Response is **always** the same generic ack regardless of success/failure — do not treat response bytes as a success signal:
```
08 cf 01 fa 0c 2d 08 01 1a 27 "Request has been successfully processed" 30 00
```
**The only reliable way to confirm success is checking the DB** (`SELECT heard, heard_time FROM s_voicemail WHERE id=?`).

### Delete voicemail — method ID 126 (0x7e)
Payload = `{ field1: voicemail_id (varint) }` — no second field (unlike method 114, there's no boolean to set).

Example: delete voicemail id=22:
```
08 7e f2 07 02 08 16
```
Confirmed via a second capture (HAR, `deletevm.har`) deleting id=19:
```
08 7e f2 07 02 08 13
```
Same envelope shape as method 114, just a shorter inner payload and no trailing bool.

**This is a hard delete, not a flag flip.** Confirmed against the live DB: after calling this RPC for id=22, `SELECT * FROM s_voicemail WHERE id=22` returns no row at all (vs. the `removed` column some other queries filter on) — 3CX actually removes the row. There is no undo; treat this RPC as destructive.

Response is the same generic ack as method 114 — verify via DB (row now absent), never trust the response bytes.

Implemented in `threecx_notify.notify_delete` / `_delete_payload`, exposed as `DELETE /api/messages/{id}` in `routes/messages.py`. Same ownership-scoping caveat as method 114 applies (impersonate the mailbox owner, not the viewer).

### Fetch voicemail audio — method ID 105 (0x69)
Payload contains the filename (from `s_voicemail.wav_file`, e.g. `vmail_900_301_20260724225559.wav`).
Response is a Protobuf envelope wrapping a raw WAV file. **Strip everything before the `RIFF` magic bytes** to get a playable WAV — header length varies, search for `RIFF` rather than hardcoding an offset.

## Critical finding: RPC is strictly ownership-scoped
- A session (even one with full admin/System Owner rights, non-impersonated) **cannot** mark another extension's voicemail heard/unheard via method 114. Confirmed via direct testing: 900's own admin session silently no-op'd against extension 301's mailbox (id=6), while succeeding against its own mailbox (id=3).
- **Impersonation (Step 2 above) is required** to act on any mailbox other than the one you're natively authenticated as. This is why the full 3-step auth chain is necessary, not just a single admin login.
- The RPC returns identical "success" acks whether or not the write actually happened — ownership mismatches fail silently, not with an HTTP error or distinct Protobuf error field. Always verify via DB.

## SIP MWI behavior (confirmed via packet capture)
- Marking heard/unheard via the correct RPC (own mailbox, or impersonated) triggers an **immediate** SIP NOTIFY to the phone — no delay, no reload, no wait for the phone's periodic SUBSCRIBE renewal.
- NOTIFY body:
  ```
  Content-Type: application/simple-message-summary
  Event: message-summary

  Messages-Waiting: yes|no
  voice-message: <unheard_count>/<old_count>
  ```
- This confirms the correct implementation path is calling the RPC (not writing the DB directly and waiting/reloading).

## Recommended app architecture
```
Your app (Entra-authenticated users)
    ↓
Backend maps: entra_user → 3cx_extension_number
    ↓
Backend holds ONE stored admin credential (900 + password)
    ↓
On startup / token expiry: Step 1 (admin login) → cache refresh_token (~90 day validity)
    ↓
Per mailbox action needed: Step 2 (impersonate target ext) → Step 3 (get sessionKey)
    ↓
Call method-114 RPC with that sessionKey to mark heard/unheard
    ↓
Verify via DB read (response bytes are not reliable)
```

Token caching strategy: cache the admin refresh_token long-term; impersonated access_tokens are cheap to mint per-request (1hr expiry) so probably don't need caching beyond a single operation, but could be cached per-extension for ~1hr if doing bulk operations to reduce round trips.

## Confirmed via testing (2026-07-25)
- **Full 3-step auth chain works end-to-end as documented above** — verified live, method-114 RPC successfully marked another extension's voicemail via impersonation, confirmed via DB.
- **`grant_type=password` is not viable as an alternative to impersonation.** Impersonation (Step 2) is required to act on any mailbox other than the natively-authenticated one — a direct per-user login would still hit the same ownership-scoping restriction on the method-114 RPC itself, so there's no auth-flow shortcut around it. Use the single-admin-credential + impersonation model, not per-user stored credentials.
- **Admin refresh_token issues a new token on every refresh** — i.e. refreshing is not idempotent/reusable; each Step 1 (or re-auth) call mints a fresh refresh_token. Design the backend to always persist the *latest* refresh_token returned, not assume a static long-lived value — if an old refresh_token is discarded/rotated out server-side after a new one is issued, using a stale cached one could fail. Recommend: store the most recent refresh_token after every login, and treat it as rotating rather than fixed.

## Open items / not yet tested
- Varint encoding for voicemail IDs ≥ 128.
- Exact behavior/necessity of the `pass` field returned alongside `sessionKey` in Step 3 (unused so far, may be required for some other RPC).
- Rate limiting / throttling behavior of `/connect/token` under bulk impersonation calls (e.g. syncing many mailboxes in sequence).
- Whether the rotating refresh_token has a hard reuse-detection/invalidation policy (i.e., does using an old refresh_token after a newer one was issued get flatly rejected, or does it still work until natural expiry?) — worth testing if the backend ever needs concurrent sessions.
