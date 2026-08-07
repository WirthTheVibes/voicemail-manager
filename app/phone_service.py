"""
Persistent PJSUA2 SIP client backing the voicemail detail panel's phone-icon
playback button and "Call back" button (routes/messages.py, routes/calls.py).

Registers once at app startup (see main.py's lifespan) as the extension
configured in .env (PBX_HOST/EXTENSION/AUTH_ID/PASSWORD -- same block
dial_and_play.py uses), and stays registered for the life of the process.

Every pjsua2/pjsip API call must happen on the one thread that called
Endpoint.libCreate()/libInit() -- that's the thread pjsip implicitly treats
as registered; touching a pjsua2 object from any other thread (including
deallocating one via Python's GC) aborts the whole process. So this module
runs a single dedicated worker thread with a job queue: route handlers never
touch pjsua2 objects directly, they enqueue a job and block briefly on a
concurrent.futures.Future for the "INVITE sent" acknowledgement.

Without an explicit outbound proxy, pjsip resolves each INVITE's destination
from the Request-URI's own host (PBX_DOMAIN, e.g. your-pbx.3cx.ca) via
plain DNS -- which resolves to the PBX's *public* IP even when running on the
PBX box itself, and that hairpin round-trip never comes back. Routing every
request through PBX_HOST as an explicit proxy is what makes PBX_HOST=127.0.0.1
apply to calls, not just registration (confirmed via a raw SIP trace on this
box).
"""
import concurrent.futures
import logging
import queue
import socket
import threading
import time
import wave

from . import config

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [phone] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

try:
    import pjsua2 as pj
except ImportError:
    pj = None

RING_TIMEOUT_SECONDS = 45
REG_TIMEOUT_SECONDS = 8
# Grace period between the agent's extension answering and the blind
# transfer firing -- 3CX needs a beat to finish setting up the confirmed
# leg's media before it'll accept a REFER on it.
XFER_DELAY_SECONDS = 0.2


class PhoneServiceUnavailable(Exception):
    """Phone playback isn't configured, or the SIP account isn't registered."""


def _wav_duration(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _build_uri(user: str, host: str, port: int, transport: str) -> str:
    scheme = "sips" if transport == "tls" else "sip"
    suffix = "" if transport == "udp" else f";transport={transport}"
    return f"{scheme}:{user}@{host}:{port}{suffix}"


SIP_REJECT_WATCH_SOCK = "/run/sip-reject-watch/query.sock"


def _query_reject_watch(extension: str, since_ts: float) -> str | None:
    """Asks the sip-reject-watch helper (sip_reject_watch.py, its own
    systemd unit with CAP_NET_RAW) whether the callee's own phone sent a
    486/487/480/600/603 addressed to `extension` at or after `since_ts` --
    wire evidence 3CX never forwards to our own dialog (see
    sip_reject_watch.py's module docstring: 3CX B2BUAs every call, so our
    leg's own CONFIRMED/200 OK is legitimately clean even when the real
    phone rejected or never answered).

    `since_ts` must be this call's own start time -- the watcher keeps a
    rolling ~10s window, so without it a rejection from a *previous* call
    attempt to the same extension would still match a fresh retry and kill
    it too.

    Best-effort only: any failure (helper not running, socket missing,
    timeout) returns None and the caller just trusts CONFIRMED as before --
    never blocks or delays the call."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            s.connect(SIP_REJECT_WATCH_SOCK)
            s.sendall(f"{extension} {since_ts}".encode())
            reply = s.recv(256).decode(errors="replace").strip()
    except OSError:
        return None
    return None if reply in ("", "NONE") else reply


if pj is not None:
    TRANSPORTS = {"udp": pj.PJSIP_TRANSPORT_UDP, "tcp": pj.PJSIP_TRANSPORT_TCP, "tls": pj.PJSIP_TRANSPORT_TLS}

    class WavPlayer(pj.AudioMediaPlayer):
        def __init__(self, wav_path: str, call: "PlaybackCall"):
            super().__init__()
            self.call = call

        def onEof2(self):
            logger.info("[phone:%s] playback finished, hanging up", self.call.label)
            try:
                self.call.hangup(pj.CallOpParam(True))
            except pj.Error:
                pass

    class PlaybackCall(pj.Call):
        """Voicemail phone-icon: dial `extension`, and once answered, play
        `wav_path` down the line once, then hang up."""

        def __init__(self, acc, wav_path: str, label: str, on_finished, request_hangup):
            super().__init__(acc)
            self.wav_path = wav_path
            self.label = label
            self.player = None
            self.answered = False
            self._start_ts = time.time()
            self._on_finished = on_finished
            self._ring_timer = threading.Timer(RING_TIMEOUT_SECONDS, lambda: request_hangup(label))
            self._ring_timer.daemon = True
            self._ring_timer.start()

        def onCallState(self, prm):
            ci = self.getInfo()
            logger.info("[phone:%s] state=%s code=%s reason=%s", self.label, ci.stateText, ci.lastStatusCode, ci.lastReason)
            if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
                self._ring_timer.cancel()
                extension = self.label.rsplit("-", 1)[0]
                reject = _query_reject_watch(extension, self._start_ts)
                if reject is not None:
                    logger.info(
                        "[phone:%s] CONFIRMED but wire shows %s from ext %s -- not a real pickup, hanging up",
                        self.label, reject, extension,
                    )
                    try:
                        self.hangup(pj.CallOpParam(True))
                    except pj.Error:
                        pass
                    return
                self.answered = True
            elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                self._ring_timer.cancel()
                self._on_finished(self)

        def onCallMediaState(self, prm):
            ci = self.getInfo()
            for mi in ci.media:
                if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    audio_media = self.getAudioMedia(-1)
                    if self.player is None:
                        duration = _wav_duration(self.wav_path)
                        logger.info("[phone:%s] media up, playing %s (%.1fs)", self.label, self.wav_path, duration)
                        self.player = WavPlayer(self.wav_path, self)
                        self.player.createPlayer(self.wav_path, pj.PJMEDIA_FILE_NO_LOOP)
                    self.player.startTransmit(audio_media)

    class CallbackCall(pj.Call):
        """"Call back" button: dial `extension`, wait for the agent to
        actually pick up (CONFIRMED), then blind transfer (REFER) that call
        to `dest_number` -- the voicemail's DID. EARLY (180 Ringing) is just
        the office phone ringing -- firing the transfer there cancels the
        ring before the agent ever sees it, which shows up as a missed call,
        not a transfer."""

        def __init__(self, acc, label: str, dest_number: str, on_finished, request_hangup, request_xfer):
            super().__init__(acc)
            self.label = label
            self.dest_number = dest_number
            self.answered = False
            self._xfer_sent = False
            self._xfer_timer = None
            self._start_ts = time.time()
            self._on_finished = on_finished
            self._ring_timer = threading.Timer(RING_TIMEOUT_SECONDS, lambda: request_hangup(label))
            self._ring_timer.daemon = True
            self._ring_timer.start()
            self._request_xfer = request_xfer

        def onCallState(self, prm):
            ci = self.getInfo()
            logger.info("[phone:%s] state=%s code=%s reason=%s", self.label, ci.stateText, ci.lastStatusCode, ci.lastReason)
            if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
                self._ring_timer.cancel()
                extension = self.label.rsplit("-", 1)[0]
                reject = _query_reject_watch(extension, self._start_ts)
                if reject is not None:
                    logger.info(
                        "[phone:%s] CONFIRMED but wire shows %s from ext %s -- not a real pickup,"
                        " hanging up instead of transferring",
                        self.label, reject, extension,
                    )
                    try:
                        self.hangup(pj.CallOpParam(True))
                    except pj.Error:
                        pass
                    return
                self.answered = True
                logger.info("[phone:%s] answered, transferring in %.0fms", self.label, XFER_DELAY_SECONDS * 1000)
                self._xfer_timer = threading.Timer(XFER_DELAY_SECONDS, lambda: self._request_xfer(self.label))
                self._xfer_timer.daemon = True
                self._xfer_timer.start()
            elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                self._ring_timer.cancel()
                if self._xfer_timer is not None:
                    self._xfer_timer.cancel()
                self._on_finished(self)

        def do_transfer(self):
            """Runs on the worker thread (see PhoneService._handle_xfer) --
            never called directly from the delay timer's own OS thread."""
            if self._xfer_sent:
                return
            self._xfer_sent = True
            dest_uri = _build_uri(self.dest_number, config.PHONE_DOMAIN, config.PHONE_PORT, config.PHONE_TRANSPORT)
            logger.info("[phone:%s] transferring to %s", self.label, dest_uri)
            self.xfer(dest_uri, pj.CallOpParam(True))

        def onCallTransferStatus(self, prm):
            # This is a BLIND transfer: the agent's leg drops as soon as 3CX
            # accepts the REFER, not once the DID actually picks up. Waiting
            # for prm.finalNotify/a connect status here would turn this into
            # an attended transfer, leaving the agent's phone "ringing" for
            # however long the DID takes to answer. So hang up unconditionally
            # on this callback's first firing (the REFER's own 202 Accepted) --
            # subsequent NOTIFYs about the referred call's progress are 3CX's
            # problem now, not ours to wait on.
            logger.info("[phone:%s] transfer status %s %s (final=%s)", self.label, prm.statusCode, prm.reason, prm.finalNotify)
            prm.cont = False
            try:
                self.hangup(pj.CallOpParam(True))
            except pj.Error:
                pass

    class PjAccount(pj.Account):
        def __init__(self):
            super().__init__()
            self.registered = threading.Event()
            self.reg_ok = False
            self.reg_reason = ""

        def onRegState(self, prm):
            ai = self.getInfo()
            self.reg_ok = ai.regIsActive
            self.reg_reason = f"{prm.code} {prm.reason}"
            logger.info("[phone account] %s: %s", "registered" if ai.regIsActive else "unregistered", self.reg_reason)
            self.registered.set()


class PhoneService:
    def __init__(self):
        self._jobs: queue.Queue = queue.Queue()
        self._acc = None
        self._active: dict[str, object] = {}
        self._active_lock = threading.Lock()
        self._label_seq = 0
        self._started = threading.Event()
        self._start_ok = False

    def start(self):
        """Spins up the dedicated pjsua2 thread. Safe to call even when
        phone playback isn't configured -- it just no-ops so the rest of the
        app (voicemail viewing/streaming) is unaffected."""
        if not config.PHONE_ENABLED:
            logger.info("Phone playback disabled (PBX_HOST/EXTENSION/PASSWORD not set in .env)")
            return
        if pj is None:
            logger.warning("Phone playback disabled: pjsua2 is not installed")
            return
        threading.Thread(target=self._run, name="phone-service", daemon=True).start()
        self._started.wait(REG_TIMEOUT_SECONDS + 3)

    def is_available(self) -> bool:
        return self._start_ok and self._acc is not None and self._acc.reg_ok

    def status(self) -> dict:
        """For the Settings > Phone header's registration indicator -- not
        used by call_extension/call_back, which check reg_ok directly."""
        if not config.PHONE_ENABLED:
            return {"configured": False, "registered": False, "reason": "Not configured"}
        if not self._start_ok:
            return {"configured": True, "registered": False, "reason": "SIP client failed to start"}
        if self._acc is None:
            return {"configured": True, "registered": False, "reason": "No SIP account"}
        return {"configured": True, "registered": self._acc.reg_ok, "reason": self._acc.reg_reason}

    def _run(self):
        try:
            ep = pj.Endpoint()
            ep.libCreate()
            ep_cfg = pj.EpConfig()
            ep_cfg.logConfig.consoleLevel = 1
            ep_cfg.uaConfig.maxCalls = 32
            ep.libInit(ep_cfg)
            tcfg = pj.TransportConfig()
            tcfg.port = 0
            ep.transportCreate(TRANSPORTS[config.PHONE_TRANSPORT], tcfg)
            ep.libStart()
            ep.audDevManager().setNullDev()  # headless PBX box, no sound card
            logger.info("endpoint started, pjsua2 %s", ep.libVersion().full)

            acc_cfg = pj.AccountConfig()
            acc_cfg.idUri = _build_uri(config.PHONE_EXTENSION, config.PHONE_DOMAIN, config.PHONE_PORT, config.PHONE_TRANSPORT)
            acc_cfg.regConfig.registrarUri = f"sip:{config.PHONE_HOST}:{config.PHONE_PORT}" + (
                "" if config.PHONE_TRANSPORT == "udp" else f";transport={config.PHONE_TRANSPORT}"
            )
            proxies = pj.StringVector()
            proxies.append(f"sip:{config.PHONE_HOST}:{config.PHONE_PORT};lr")
            acc_cfg.sipConfig.proxies = proxies
            acc_cfg.sipConfig.authCreds.append(
                pj.AuthCredInfo("digest", "*", config.PHONE_AUTH_ID, 0, config.PHONE_PASSWORD)
            )
            acc = PjAccount()
            acc.create(acc_cfg)
            self._acc = acc

            if not acc.registered.wait(REG_TIMEOUT_SECONDS):
                logger.warning("registration timed out (ext %s)", config.PHONE_EXTENSION)
            elif not acc.reg_ok:
                logger.warning("registration failed: %s", acc.reg_reason)
            self._start_ok = True
        except Exception:
            logger.exception("failed to start SIP endpoint")
            self._start_ok = False
            self._started.set()
            return

        self._started.set()
        self._loop()

    def _loop(self):
        while True:
            job = self._jobs.get()
            kind = job[0]
            if kind == "dial":
                self._handle_dial(*job[1:])
            elif kind == "callback":
                self._handle_callback(*job[1:])
            elif kind == "hangup":
                self._handle_hangup(job[1])
            elif kind == "xfer":
                self._handle_xfer(job[1])

    def _next_label(self, extension: str) -> str:
        self._label_seq += 1
        return f"{extension}-{self._label_seq}"

    def _handle_dial(self, wav_path: str, extension: str, future) -> None:
        try:
            if not (self._acc and self._acc.reg_ok):
                raise PhoneServiceUnavailable("Phone SIP account is not registered with 3CX right now")
            label = self._next_label(extension)
            dest_uri = _build_uri(extension, config.PHONE_DOMAIN, config.PHONE_PORT, config.PHONE_TRANSPORT)
            call = PlaybackCall(self._acc, wav_path, label, self._on_call_finished, self._request_hangup)
            with self._active_lock:
                self._active[label] = call
            call.makeCall(dest_uri, pj.CallOpParam(True))
            logger.info("[phone:%s] dialing %s -> %s", label, dest_uri, wav_path)
            future.set_result(label)
        except Exception as e:  # noqa: BLE001 - reported back to the HTTP caller as-is
            future.set_exception(e)

    def _handle_callback(self, extension: str, dest_number: str, future) -> None:
        try:
            if not (self._acc and self._acc.reg_ok):
                raise PhoneServiceUnavailable("Phone SIP account is not registered with 3CX right now")
            label = self._next_label(extension)
            dest_uri = _build_uri(extension, config.PHONE_DOMAIN, config.PHONE_PORT, config.PHONE_TRANSPORT)
            call = CallbackCall(self._acc, label, dest_number, self._on_call_finished, self._request_hangup, self._request_xfer)
            with self._active_lock:
                self._active[label] = call
            call.makeCall(dest_uri, pj.CallOpParam(True))
            logger.info("[phone:%s] calling ext %s, will transfer to %s", label, extension, dest_number)
            future.set_result(label)
        except Exception as e:  # noqa: BLE001 - reported back to the HTTP caller as-is
            future.set_exception(e)

    def _handle_hangup(self, label: str) -> None:
        # Runs on _loop's thread (the one pjlib knows about), unlike the
        # ring-timer callback that queued this job.
        with self._active_lock:
            call = self._active.get(label)
        if call is None or call.answered:
            return
        try:
            call.hangup(pj.CallOpParam(True))
        except pj.Error:
            pass

    def _handle_xfer(self, label: str) -> None:
        # Runs on _loop's thread (the one pjlib knows about), unlike the
        # xfer-delay timer callback that queued this job.
        with self._active_lock:
            call = self._active.get(label)
        if call is None:
            return
        try:
            call.do_transfer()
        except pj.Error as e:
            logger.warning("[phone:%s] xfer() failed: %s", label, e.info())
            self._request_hangup(label)

    def _on_call_finished(self, call) -> None:
        with self._active_lock:
            self._active.pop(call.label, None)
        # call<->timer/player form reference cycles that plain refcounting
        # won't free -- only cyclic GC would, and that can run on any
        # thread. Deallocating pjsua2/SWIG objects from a thread pjlib never
        # registered aborts the whole process, so break the cycles here, on
        # this call's own registered worker thread.
        call._ring_timer = None
        if getattr(call, "_xfer_timer", None) is not None:
            call._xfer_timer = None
        if getattr(call, "player", None) is not None:
            call.player.call = None
            call.player = None

    def _request_xfer(self, label: str) -> None:
        """Thread-safe, pjlib-free: safe to call from the xfer-delay timer's
        own OS thread. Just queues the actual transfer for _loop's thread."""
        self._jobs.put(("xfer", label))

    def _request_hangup(self, label: str) -> None:
        """Thread-safe, pjlib-free: safe to call from the ring-timer's own
        OS thread. Just queues the actual hangup() for _loop's thread."""
        self._jobs.put(("hangup", label))

    def call_extension(self, wav_path: str, extension: str, timeout: float = 5.0) -> str:
        """Enqueues a call to `extension` playing `wav_path`, and blocks (up
        to `timeout`) for confirmation the INVITE was sent -- not answered.
        Safe to call from any thread; never touches pjsua2 objects itself."""
        if not config.PHONE_ENABLED:
            raise PhoneServiceUnavailable("Phone playback is not configured on this server")
        if not self._start_ok:
            raise PhoneServiceUnavailable("Phone SIP client failed to start")
        future: "concurrent.futures.Future[str]" = concurrent.futures.Future()
        self._jobs.put(("dial", wav_path, extension, future))
        return future.result(timeout=timeout)

    def call_back(self, extension: str, dest_number: str, timeout: float = 5.0) -> str:
        """Enqueues a callback: rings `extension` and, once answered,
        blind-transfers that call to `dest_number`. Blocks (up to `timeout`)
        for confirmation the first INVITE was sent -- not that the transfer
        completed. Safe to call from any thread."""
        if not config.PHONE_ENABLED:
            raise PhoneServiceUnavailable("Phone playback is not configured on this server")
        if not self._start_ok:
            raise PhoneServiceUnavailable("Phone SIP client failed to start")
        future: "concurrent.futures.Future[str]" = concurrent.futures.Future()
        self._jobs.put(("callback", extension, dest_number, future))
        return future.result(timeout=timeout)


phone_service = PhoneService()
