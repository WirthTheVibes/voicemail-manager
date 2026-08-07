"""
MWI relay for 3CX group/department mailboxes.

A Yealink's MWI light is driven by whatever message-summary NOTIFY it last
received -- there's no "add to the light," only "replace it." A phone only
ever natively subscribes to its own personal mailbox, so a group/department
mailbox's messages never reach it: nothing is subscribed to that mailbox's
event, so 3CX has nowhere to send a NOTIFY even when the mailbox's own
message-summary state does change.

This started as a SIP-SUBSCRIBE-based design (subscribe to each relevant
mailbox's message-summary event, react to 3CX's own NOTIFYs). That doesn't
work: confirmed live that 3CX's SUBSCRIBE handler sends exactly one NOTIFY
back at subscribe time (sometimes even a stale one -- see the 900
reboot-resubscribe case) and then never pushes again to that dialog. Real
ongoing MWI dispatch only reaches the extension's actual registered device,
not a foreign SUBSCRIBE from another account. Confirmed by leaving fresh
voicemails on both a personal mailbox (308) and a group mailbox (805) with
an already-established subscription open and watching zero NOTIFY traffic
arrive for either.

So this instead polls s_voicemail directly (threecx_db.unread_count_for_mailbox),
the same proven approach voicemail_watcher.py already uses in production,
just extended to also catch heard/unheard flips (voicemail_watcher only
watches for new message ids). Every POLL_SECONDS, every relevant source
mailbox's real unread count is re-read; an in-memory cache
(Aggregator._source_count) means only mailboxes whose count actually
changed since last poll do any further work, and a target phone only gets
an unsolicited NOTIFY when its computed total (personal + every group
mailbox it belongs to) actually differs from what was last pushed to it --
not on every poll tick.

Target resolution deliberately mirrors only two of access.py's five
viewers_for_mailbox() rules -- department-implicit membership (minus
per-department exclusions) and explicit per-user grants (Users tab) -- not
supervisor/manager/org-wide review access. Those roles can *see* a mailbox
in the web UI without needing its light on their own desk phone.

Started from main.py's lifespan alongside phone_service, reusing the same
PBX_* SIP credentials (see config.py) for the outbound NOTIFY -- this relay
and phone_service use different protocols (raw NOTIFY here vs pjsua2 calls
there) against the same account, which is fine: NOTIFY is its own
transaction, authenticated independently of REGISTER state, so this never
touches phone_service's own registration.
"""
import logging
import socket
import threading
import time
from collections import defaultdict

from . import app_db, config, mwi_sip, threecx_db

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [mwi-relay] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

POLL_SECONDS = 5
MEMBERSHIP_REFRESH_SECONDS = 300  # how often to re-resolve department/grant membership
RECV_TIMEOUT = 10


def resolve_targets(group_ext: str) -> set[str]:
    """Department-implicit members (minus exclusions and MWI-suppressed
    members) + explicit per-user grants (minus MWI-suppressed grants) for
    `group_ext` -- see module docstring for why this is narrower than
    access.viewers_for_mailbox. mwi_suppress lets a viewer keep their
    review access to a group mailbox without that mailbox's light showing
    on their own phone (Departments/Users tab checkbox)."""
    targets: set[str] = set()

    for department, mailboxes in app_db.all_department_mailboxes().items():
        if group_ext not in mailboxes:
            continue
        excluded = set(app_db.excluded_members(department, group_ext))
        suppressed = set(app_db.mwi_suppressed_members(department, group_ext))
        for row in threecx_db.department_extensions(department):
            if row["extension"] != group_ext and row["extension"] not in excluded and row["extension"] not in suppressed:
                targets.add(row["extension"])

    mwi_suppressed_extra = app_db.mwi_suppressed_viewers_for_mailbox(group_ext)
    for viewer_ext in app_db.viewers_granted_for_mailbox(group_ext):
        if viewer_ext != group_ext and viewer_ext not in mwi_suppressed_extra:
            targets.add(viewer_ext)

    return targets


def group_mailbox_extensions() -> set[str]:
    return {ext for mailboxes in app_db.all_department_mailboxes().values() for ext in mailboxes}


class Aggregator:
    """Tracks the last-known unread count per source mailbox and which
    target phones depend on which sources, purely in memory -- this is
    what lets the poll loop skip work for anything that hasn't changed."""

    def __init__(self):
        self._source_count: dict[str, int] = {}
        self._target_sources: dict[str, set[str]] = {}
        self._last_pushed: dict[str, int] = {}

    def set_target_sources(self, mapping: dict[str, set[str]]) -> None:
        """Replaces the full target->sources mapping. Targets/sources
        dropped from `mapping` are forgotten (their cached counts are
        harmless to keep, but stop being pushed to)."""
        removed_targets = set(self._target_sources) - set(mapping)
        self._target_sources = mapping
        for target_ext in removed_targets:
            self._last_pushed.pop(target_ext, None)

    def all_sources(self) -> set[str]:
        sources: set[str] = set()
        for s in self._target_sources.values():
            sources |= s
        return sources

    def poll_and_get_dirty_targets(self, poll_count) -> dict[str, int]:
        """Re-reads every source mailbox's real count via `poll_count`
        (threecx_db.unread_count_for_mailbox). Returns {target_ext: total}
        for exactly the targets whose computed total actually changed
        since the last push -- everything else is skipped."""
        changed_sources: set[str] = set()
        for source_ext in self.all_sources():
            try:
                count = poll_count(source_ext)
            except Exception:
                logger.exception("[%s] poll failed, keeping previous count", source_ext)
                continue
            if self._source_count.get(source_ext) != count:
                self._source_count[source_ext] = count
                changed_sources.add(source_ext)

        if not changed_sources:
            return {}

        dirty: dict[str, int] = {}
        for target_ext, sources in self._target_sources.items():
            if not sources & changed_sources:
                continue
            total = sum(self._source_count.get(s, 0) for s in sources)
            if self._last_pushed.get(target_ext) == total:
                continue
            self._last_pushed[target_ext] = total
            dirty[target_ext] = total
        return dirty


class MwiRelay:
    def __init__(self):
        self.aggregator = Aggregator()
        self.pbx_ip = None
        self.pbx_port = None
        self.pbx_domain = None
        self.auth_user = None
        self.auth_pass = None
        self.from_ext = None

    def start(self):
        """Spins up the poll-loop thread. Safe to call even when phone
        playback isn't configured -- reuses the same PBX_* credentials as
        phone_service, so if those aren't set this just no-ops."""
        if not config.PHONE_ENABLED:
            logger.info("MWI relay disabled (same PBX_HOST/EXTENSION/PASSWORD as phone playback, unset in .env)")
            return
        self.pbx_ip = config.PHONE_HOST
        self.pbx_port = config.PHONE_PORT
        self.pbx_domain = config.PHONE_DOMAIN
        self.auth_user = config.PHONE_AUTH_ID
        self.auth_pass = config.PHONE_PASSWORD
        self.from_ext = config.PHONE_EXTENSION
        threading.Thread(target=self._loop, name="mwi-relay-poll", daemon=True).start()

    def push_notify(self, target_ext: str, unread: int) -> None:
        """Fire-and-forget unsolicited NOTIFY to target_ext with the
        combined count, routed through 3CX's own proxy (resolves to
        whichever phone is currently registered for that extension)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        sock.settimeout(RECV_TIMEOUT)
        local_port = sock.getsockname()[1]
        call_id = f"{mwi_sip.tag(16)}@{self.pbx_ip}"
        from_tag = mwi_sip.tag()
        cseq = 1

        def build(auth_header=None):
            return mwi_sip.build_notify(
                self.pbx_domain, self.pbx_ip, local_port, self.from_ext, target_ext, unread,
                call_id, cseq, mwi_sip.branch(), from_tag, auth_header,
            )

        try:
            sock.sendto(build(), (self.pbx_ip, self.pbx_port))
            data, _ = sock.recvfrom(65535)
            resp = data.decode(errors="replace")
            code = mwi_sip.parse_status_code(resp)
            if code in ("401", "407"):
                headers = mwi_sip.parse_headers(resp)
                challenge = mwi_sip.find_challenge(headers)
                uri = f"sip:{target_ext}@{self.pbx_domain}"
                auth_header = mwi_sip.build_auth_header(self.auth_user, self.auth_pass, "NOTIFY", uri, challenge)
                cseq += 1
                sock.sendto(build(auth_header=auth_header), (self.pbx_ip, self.pbx_port))
                data, _ = sock.recvfrom(65535)
                resp = data.decode(errors="replace")
                code = mwi_sip.parse_status_code(resp)
            logger.info("push -> %s (total=%d): %s", target_ext, unread, code)
        except socket.timeout:
            logger.warning("push -> %s (total=%d): no response (timeout)", target_ext, unread)
        finally:
            sock.close()

    def _refresh_membership(self) -> None:
        group_exts = group_mailbox_extensions()
        target_to_groups: dict[str, set[str]] = defaultdict(set)
        for group_ext in group_exts:
            for target_ext in resolve_targets(group_ext):
                target_to_groups[target_ext].add(group_ext)

        mapping = {target_ext: {target_ext} | groups for target_ext, groups in target_to_groups.items()}
        self.aggregator.set_target_sources(mapping)
        logger.info(
            "membership refreshed: %d group mailbox(es), %d target phone(s)",
            len(group_exts), len(mapping),
        )

    def _loop(self):
        last_membership_refresh = 0.0
        while True:
            now = time.time()
            if now - last_membership_refresh > MEMBERSHIP_REFRESH_SECONDS:
                try:
                    self._refresh_membership()
                except Exception:
                    logger.exception("membership refresh failed, keeping previous mapping")
                last_membership_refresh = now

            try:
                dirty = self.aggregator.poll_and_get_dirty_targets(threecx_db.unread_count_for_mailbox)
            except Exception:
                logger.exception("poll cycle failed")
                dirty = {}

            for target_ext, total in dirty.items():
                threading.Thread(
                    target=self.push_notify, args=(target_ext, total),
                    name=f"mwi-push-{target_ext}", daemon=True,
                ).start()

            time.sleep(POLL_SECONDS)


mwi_relay = MwiRelay()
