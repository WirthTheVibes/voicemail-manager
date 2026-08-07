import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import app_db, notifications
from .events import broadcaster
from .mwi_relay import mwi_relay
from .phone_service import phone_service
from .routes import admin, auth_routes, calls, events, mailboxes, messages, phonebook, push, yealink
from .transcription_worker import transcription_worker
from .voicemail_watcher import voicemail_watcher

STATIC_DIR = Path(__file__).parent / "static"


def _broadcast_new_message(message: dict) -> None:
    broadcaster.publish({"type": "new_message", "mailbox": message["callee"], "message_id": message["id"]})


@asynccontextmanager
async def lifespan(app: FastAPI):
    app_db.init_db()
    broadcaster.bind_loop(asyncio.get_running_loop())
    phone_service.start()
    mwi_relay.start()
    # transcription_worker has no thread of its own -- handle_new_message
    # runs on voicemail_watcher's thread (see transcription_worker.py's
    # module docstring for why there's no backfill scan to start here).
    # Order matters: transcription's immediate-trigger runs before
    # notifications' dispatch, so an SMTP/push notification that eventually
    # links back to the message has the best chance of a transcription
    # already being there (best-effort -- neither handler waits on the
    # other, this is just poll-order within one dispatch).
    voicemail_watcher.register(transcription_worker.handle_new_message)
    voicemail_watcher.register(notifications.dispatch)
    voicemail_watcher.register(_broadcast_new_message)
    voicemail_watcher.start()
    yield


app = FastAPI(title="3CX Voicemail Manager", lifespan=lifespan)

app.include_router(auth_routes.router)
app.include_router(mailboxes.router)
app.include_router(messages.router)
app.include_router(calls.router)
app.include_router(admin.router)
app.include_router(push.router)
app.include_router(yealink.router)
app.include_router(phonebook.router)
app.include_router(events.router)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/app", include_in_schema=False)
@app.get("/app/{path:path}", include_in_schema=False)
def app_shell(path: str = ""):
    # A real, separate page from "/" (the login page) — see login.js/app.js
    # for why: password managers need an actual navigation between the two
    # to correctly treat sign-in as "complete" instead of re-triggering a
    # save-password prompt on every button click inside the app.
    # Client-side auth check (redirects to "/" if not signed in) happens in
    # app.js via GET /api/me; this route just serves the static shell.
    #
    # The `{path:path}` variant exists so that URLs like /app/205 or
    # /app/settings/users/205 — pushed into browser history by app.js's own
    # router — still resolve to something on a hard refresh or a pasted
    # link, instead of 404ing against the StaticFiles mount below. app.js
    # re-derives which mailbox/settings pane to show from location.pathname.
    return FileResponse(STATIC_DIR / "app.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
