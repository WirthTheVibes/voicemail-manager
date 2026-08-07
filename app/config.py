import os
from pathlib import Path

from dotenv import load_dotenv

from . import app_setting_store as _settings

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

APP_ROOT = Path(__file__).resolve().parent.parent


def _migrated(key: str, env_name: str, default=None, encrypted: bool = False):
    """Reads a setting that used to be .env-only from the DB-backed
    app_setting store instead. On first boot after this key was introduced,
    seeds the DB from whatever .env still holds (so upgrading doesn't lose
    an existing deployment's value) -- every boot after that, the DB alone
    is authoritative and the .env value (if anyone leaves it there) is
    ignored. See app_setting_store.py's module docstring for why this can't
    just go through app_db.py."""
    env_val = os.environ.get(env_name)
    if env_val is not None:
        _settings.seed_if_absent(key, env_val, encrypted=encrypted)
    return _settings.get(key, default)

DATABASE_URL = os.environ["DATABASE_URL"]
SECRET_KEY = os.environ["SECRET_KEY"]
ADMIN_EXTENSIONS = set(
    e.strip() for e in os.environ.get("ADMIN_EXTENSIONS", "").split(",") if e.strip()
)
VOICEMAIL_ROOT = Path(os.environ.get("VOICEMAIL_ROOT", "/var/lib/3cxpbx/Instance1/Data/Ivr/Voicemail/Extensions"))
APP_DB_PATH = os.environ.get("APP_DB_PATH", "/opt/vm-manager/vm_manager.db")

# Native 3CX MWI notify (see notes/3cx-native-notify.md). Required: marking a
# message heard/unheard has no DB-write fallback (see threecx_notify.py) —
# without these, POST /api/messages/{id}/heard always fails.
THREECX_PBX_URL = os.environ["THREECX_PBX_URL"].rstrip("/")
THREECX_ADMIN_EXTENSION = os.environ["THREECX_ADMIN_EXTENSION"]
THREECX_ADMIN_PASSWORD = os.environ["THREECX_ADMIN_PASSWORD"]

# Phone playback (see phone_service.py): reuses dial_and_play.py's own env
# vars so the two never drift out of sync. Optional -- if PBX_HOST/EXTENSION/
# PASSWORD aren't set, the phone-call button just stays unavailable (503)
# rather than blocking app startup, since voicemail viewing doesn't depend
# on it.
PHONE_HOST = _migrated("pbx_host", "PBX_HOST")
PHONE_DOMAIN = _migrated("pbx_domain", "PBX_DOMAIN", PHONE_HOST)
PHONE_TRANSPORT = _migrated("pbx_transport", "PBX_TRANSPORT", "udp").lower()
PHONE_PORT = int(_migrated("pbx_port", "PBX_PORT", 5061 if PHONE_TRANSPORT == "tls" else 5060))
PHONE_EXTENSION = _migrated("pjsua_extension", "EXTENSION")
# AUTH_ID/PASSWORD are the actual SIP login credentials -- encrypted at rest
# (see app_setting_store.py), unlike the connection details above.
PHONE_AUTH_ID = _migrated("pjsua_auth_id", "AUTH_ID", PHONE_EXTENSION, encrypted=True)
PHONE_PASSWORD = _migrated("pjsua_password", "PASSWORD", encrypted=True)
PHONE_ENABLED = bool(PHONE_HOST and PHONE_EXTENSION and PHONE_PASSWORD)

# --- Voicemail transcription --------------------------------------------
# Enable/disable and engine choice ("local"/"openai") are runtime-editable
# via the Settings > Transcription tab (see app_db.get/set_transcription_settings)
# rather than .env, so an admin can flip them without a restart. Everything
# below is deployment-level config instead: model choice, resource limits,
# and the OpenAI credential (a secret, so it stays in .env like every other
# credential this app holds -- never typed into the web UI).
#
# WHISPER_MODEL_CACHE_DIR defaults under this app's own directory (not
# phonesystem's home, which is 3CX's own tree) so the ~500MB-1GB model
# download is obviously this app's state -- same reasoning as APP_DB_PATH.
WHISPER_MODEL_SIZE = _migrated("whisper_model_size", "WHISPER_MODEL_SIZE", "tiny")
WHISPER_COMPUTE_TYPE = _migrated("whisper_compute_type", "WHISPER_COMPUTE_TYPE", "int8")
WHISPER_CPU_THREADS = int(_migrated("whisper_cpu_threads", "WHISPER_CPU_THREADS", "2"))
WHISPER_MODEL_CACHE_DIR = Path(
    os.environ.get("WHISPER_MODEL_CACHE_DIR", str(Path(__file__).resolve().parent.parent / "models"))
)
# Local transcription runs in a subprocess specifically so this cap can be
# enforced by polling that subprocess's own RSS and killing it -- see
# transcription.py's module docstring for why (RLIMIT_AS doesn't work here).
WHISPER_MEMORY_LIMIT_MB = int(_migrated("whisper_memory_limit_mb", "WHISPER_MEMORY_LIMIT_MB", "1024"))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_TRANSCRIBE_MODEL = _migrated("openai_transcribe_model", "OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")

# --- Voicemail watcher ---------------------------------------------------
# Drives notifications + immediate transcription (see voicemail_watcher.py).
# 5s was measured against this server's actual s_voicemail size/load and
# costs well under a millisecond per poll -- see the perf discussion this
# value came out of. Not runtime-editable like the Settings toggles above:
# changing the poll cadence isn't something an admin needs day to day.
VM_WATCH_POLL_SECONDS = int(os.environ.get("VM_WATCH_POLL_SECONDS", "5"))

# --- Notification delivery (Settings > Notifications tab) ----------------
# Enable/disable (SMTP vs PWA push, independently) is runtime-editable via
# the Settings UI (app_db.get/set_notification_settings) -- everything
# below is deployment-level config/secrets that stays in .env, same
# reasoning as OPENAI_API_KEY above.
SMTP_HOST = _migrated("smtp_host", "SMTP_HOST")
SMTP_PORT = int(_migrated("smtp_port", "SMTP_PORT", "587"))
SMTP_USERNAME = _migrated("smtp_username", "SMTP_USERNAME")
# The actual mailbox password -- encrypted at rest, unlike the rest of this block.
SMTP_PASSWORD = _migrated("smtp_password", "SMTP_PASSWORD", encrypted=True)
SMTP_FROM = _migrated("smtp_from", "SMTP_FROM", SMTP_USERNAME)
SMTP_USE_TLS = str(_migrated("smtp_use_tls", "SMTP_USE_TLS", "true")).lower() != "false"
SMTP_CONFIGURED = bool(SMTP_HOST and SMTP_FROM)

# Web Push (PWA notifications). VAPID_CONTACT is the mailto: URL push
# services require to reach an operator about a misbehaving sender --
# has nothing to do with who receives the notification.
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_CONTACT_EMAIL = os.environ.get("VAPID_CONTACT_EMAIL")
PUSH_CONFIGURED = bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_CONTACT_EMAIL)


def resolve_voicemail_path(callee: str, wav_file: str) -> Path:
    """Joins VOICEMAIL_ROOT/callee/wav_file.wav, resolved and guarded against
    a callee/wav_file value that could escape VOICEMAIL_ROOT via "..".
    Raises ValueError if it would escape. Doesn't check the file exists --
    callers do that themselves (and decide how to report it missing)."""
    root = VOICEMAIL_ROOT.resolve()
    candidate = (root / callee / f"{wav_file}.wav").resolve()
    if root not in candidate.parents:
        raise ValueError(f"Path {candidate} escapes VOICEMAIL_ROOT")
    return candidate


SESSION_COOKIE_NAME = "vm_session"
SESSION_MAX_AGE_SECONDS = int(os.environ.get("SESSION_MAX_AGE_SECONDS", 8 * 60 * 60))

# --- Yealink XML Browser (visual voicemail on the phone's own screen) --------
# PUBLIC_BASE_URL is the exact address the phone reaches this app through --
# baked into every generated link. Must match nginx's external mount
# (/vm-manager/, prefix stripped before proxying to this app's root -- see
# /var/lib/3cxpbx/Bin/nginx/conf/snippets/60-vm-manager.conf), not HOST/PORT
# (this app's own bind address). Getting this wrong is a silent failure: the
# phone just hangs on "Loading" forever, per notes/README.md's Gotchas.
# Defaults to THREECX_PBX_URL + /vm-manager, which is correct as long as this
# app's nginx mount point is the standard /vm-manager/ -- override explicitly
# if a server ever needs a different mount point.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"{THREECX_PBX_URL}/vm-manager")
# Short-lived by design (unlike SESSION_MAX_AGE_SECONDS): this token rides in
# a URL on a shared desk phone, not an HttpOnly cookie in one person's
# browser, so it re-prompts for the PIN quickly rather than staying valid
# for a full shift.
YEALINK_TOKEN_MAX_AGE_SECONDS = int(os.environ.get("YEALINK_TOKEN_MAX_AGE_SECONDS", 300))
# Desk phones show times converted to this zone rather than the raw UTC 3CX
# stores -- pick the zone your desk phones are physically in.
YEALINK_TIME_ZONE = os.environ.get("YEALINK_TIME_ZONE", "America/Vancouver")

LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", 5))
LOGIN_LOCKOUT_MINUTES = int(os.environ.get("LOGIN_LOCKOUT_MINUTES", 15))

# --- Microsoft (Entra ID) sign-in for the web UI --------------------------
# Public-client Authorization Code + PKCE flow (app registration's platform
# must be "Single-page application" so login.microsoftonline.com's /token
# endpoint allows the browser to call it directly) -- no client secret
# exists anywhere in this flow, so there's nothing to rotate. The browser
# does the code/PKCE exchange itself and hands vm-manager only the
# resulting ID token; see ms_auth.py for how that token gets verified
# (against Microsoft's own public JWKS, not a shared secret) and matched to
# a 3CX extension via voicemail.email (see threecx_db.by_email).
MS_AUTH_TENANT_ID = _migrated("ms_auth_tenant_id", "MS_AUTH_TENANT_ID")
MS_AUTH_CLIENT_ID = _migrated("ms_auth_client_id", "MS_AUTH_CLIENT_ID")
MS_AUTH_ENABLED = bool(MS_AUTH_TENANT_ID and MS_AUTH_CLIENT_ID)

# PIN sign-in for the web UI is meant to go away once MS Auth above is live
# -- PIN afterwards is only for the Yealink visual-voicemail flow (see
# auth.create_yealink_token/verify_yealink_token), which doesn't use this
# flag at all. This is the one back door: flip PIN_LOGIN_ENABLED=true here
# if MS Auth ever locks everyone out. Defaults to enabled as long as MS
# Auth isn't configured yet, so this doesn't break login the moment
# MS_AUTH_TENANT_ID/MS_AUTH_CLIENT_ID land in .env before the rollout is
# actually flipped on.
PIN_LOGIN_ENABLED = os.environ.get("PIN_LOGIN_ENABLED", "false" if MS_AUTH_ENABLED else "true").lower() == "true"

# A handful of individually-owned Entra ID accounts (e.g. several admins)
# that should all sign in as one shared management extension instead of
# each needing their own voicemail.email match -- see threecx_db.by_extension.
# Comma-separated, matched case-insensitively against the ID token's
# preferred_username/upn/email claim.
MS_AUTH_OVERRIDE_EMAILS = set(
    e.strip().lower() for e in (_migrated("ms_auth_override_emails", "MS_AUTH_OVERRIDE_EMAILS", "") or "").split(",")
    if e.strip()
)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))
