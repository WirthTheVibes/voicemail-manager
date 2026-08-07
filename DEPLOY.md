# Deploying to another 3CX server

## 1. Copy the code over (not the data)

From this server:

```bash
# Excludes this server's app-specific state — the new server should start
# with a clean SQLite DB and its own .env, not inherit this one's. `models/`
# is the downloaded faster-whisper weights (see app/transcription.py) --
# skip it too so a new server re-downloads on first local transcription
# instead of inheriting a possibly-stale copy.
rsync -av --exclude='.env' --exclude='vm_manager.db' --exclude='venv' --exclude='models' \
  /opt/vm-manager/ root@<new-server>:/opt/vm-manager/
```

(Any method works — scp, a tarball, git — the point is just: skip `.env`,
`vm_manager.db`, and `models/`.)

## 2. Run the installer on the new server

```bash
ssh root@<new-server>
cd /opt/vm-manager
sudo ./install.sh
```

This checks the new server actually looks like a 3CX box (the `phonesystem`
OS user, Postgres peer auth, the voicemail audio path), installs the Python
dependencies system-wide, generates a **fresh** `.env` with a new
`SECRET_KEY`, sets ownership, installs+enables the systemd service, installs
an nginx snippet at `/var/lib/3cxpbx/Bin/nginx/conf/snippets/60-vm-manager.conf`
that reverse-proxies `https://<host>/vm-manager/` to the app on 3CX's own
nginx, and installs+starts `sip-reject-watch.service` (mandatory — see
below) — but does **not** start `vm-manager.service` itself yet.

The app listens on `127.0.0.1:8080` by default (see `HOST` in `.env`) — it's
only reachable through that nginx proxy on 443, which 3CX already has open
in its firewall. **No firewall changes are needed for a normal install.**

**`sip-reject-watch.service` is mandatory, not optional.** `call_extension`/
`call_back` (`phone_service.py`) can't tell a real answer from 3CX
transparently rerouting a rejected/no-answer call to its own internal
voicemail app — 3CX is a B2BUA, so it reports a clean `200 OK`/CONFIRMED
back to our own SIP leg either way. The callee's phone's actual `486 Busy
Here` / `487 Request Terminated` / etc. only ever appears on the wire
between 3CX's core and that phone, never in our dialog (see
`_query_reject_watch`'s docstring in `phone_service.py` for the full
finding, backed by a raw SIP trace on the original server). Without it,
those phone features silently degrade to trusting CONFIRMED and will
misreport rejected/no-answer calls as picked up — `install.sh` installs and
starts it unconditionally, requires `CAP_NET_RAW` via its own systemd unit
(`vm-manager.service` deliberately doesn't have that capability), and
auto-detects the PBX-facing NIC from the default route to patch
`sip_reject_watch.py`'s `IFACE` — but **still verify it's actually seeing
traffic**, since a wrong interface starts fine and just silently sees
nothing:

```bash
systemctl status sip-reject-watch.service   # should be active, a few MB RSS
tcpdump -i <iface> -n "port 5060"           # during a real call — confirm you see SIP traffic
```

`Group=phonesystem` in its unit matters: it's what lets `vm-manager.service`
(which runs as `phonesystem`) read/write the query socket without either
service running as root.

## 2.5 If you need dial_and_play.py (PJSUA2) on the new server

`dial_and_play.py` isn't installed by `install.sh` — it needs `pjsua2`, which
has no working prebuilt PyPI wheel (see the comment at the top of
`build_pjsua2.sh`). Run it once per server:

```bash
sudo ./build_pjsua2.sh
```

This builds pjproject from source and installs `pjsua2` into the system
python3 (~5-10 minutes). It's independent of `install.sh` and doesn't touch
`app/config.py` or anything the running service depends on — safe to skip
entirely if this server doesn't need outbound-call/playback functionality.

Then append a config block to `.env` (see the bottom of this server's `.env`
for the exact format) with **that server's own** extension credentials from
the 3CX admin console (Extensions → the extension → Generic/SIP tab) — don't
copy this server's `EXTENSION`/`AUTH_ID`/`PASSWORD` values over, they're
specific to this PBX's extension 998.

## 3. Finish the one thing the script can't safely automate

- **Set `ADMIN_EXTENSIONS` in `.env`** — the installer leaves this blank on
  purpose; it's specific to who should be an admin on that PBX. Also fill in
  `THREECX_PBX_URL`, `THREECX_ADMIN_EXTENSION`, `THREECX_ADMIN_PASSWORD` —
  required, the app won't start without them. `PUBLIC_BASE_URL` (used for
  Yealink visual voicemail links) defaults to `THREECX_PBX_URL` +
  `/vm-manager`, which is correct for a standard install — only set it
  explicitly if this server's nginx mount point isn't `/vm-manager/`.

If you deliberately want the app reachable directly (bypassing nginx), set
`HOST=0.0.0.0` in `.env` and open the port yourself in
`/var/lib/3cxpbx/Bin/nftables.conf` (3CX's own managed config, not something
`install.sh` edits unattended) — add it to the `phonesystem` chain's `tcp
dport` set in all three tables (`inet`, `ip`, `ip6`), matching the existing
entries, then `systemctl reload-or-restart 3CXFirewall.service`. This isn't
needed for a normal install.

## 4. Start it

```bash
systemctl start vm-manager.service
systemctl status vm-manager.service
```

Then log in from a browser at `https://<new-server-hostname>/vm-manager/`
with a real extension + PIN on that PBX.

## Updating an already-deployed server

Same idea as a fresh install, just re-run both steps — everything in them is
idempotent and skips/preserves what's already there:

```bash
# from this server
rsync -av --exclude='.env' --exclude='vm_manager.db' --exclude='venv' --exclude='models' \
  /opt/vm-manager/ root@<target-server>:/opt/vm-manager/

ssh root@<target-server>
cd /opt/vm-manager
sudo ./install.sh          # leaves .env alone since it already exists;
                            # re-syncs deps, systemd unit, nginx snippet
systemctl restart vm-manager.service
```

`install.sh` won't touch an existing `.env`, so pulling in new `.env` keys
added since the target server's last install (check this file's diff, or
just `grep` for the key in `install.sh`'s heredoc) is a manual step — append
them by hand.

`install.sh` reinstalls and restarts `sip-reject-watch.service` on every
run, so it picks up code changes automatically — no separate step needed.

If you added `dial_and_play.py`/pjsua2 (2.5, still optional) on the target
server, that's not touched by `install.sh`; `build_pjsua2.sh` only needs
re-running if `dial_and_play.py`'s pjsua2 usage itself changed, not for
routine app updates.

## Things worth double-checking on a different 3CX install

- **Voicemail audio path.** This app uses
  `/var/lib/3cxpbx/Instance1/Data/Ivr/Voicemail/Extensions/<ext>/<file>.wav`.
  That `Instance1` / `Extensions` layout isn't 3CX-documented — it was found
  by testing on the original server. A multi-instance install (unlikely, but
  possible) could use a different instance name. `install.sh` checks this
  path exists and warns if not; if it's different, update `VOICEMAIL_ROOT`
  in `.env`.
- **Single-tenant vs multi-tenant 3CX.** `DATABASE_URL` assumes the database
  is named `database_single` (true for standard/single-tenant installs). A
  multi-tenant install may differ.
- **3CX version differences in `heard`/`removed`/`duration` conventions.**
  This app's read/write logic (see `app/threecx_db.py`) encodes specific
  observed behavior: `heard` as `'1'`/`'0'`/empty, `duration` in
  milliseconds, timestamps in UTC. These were verified live against this
  server's 3CX version — re-verify if the new server runs a notably
  different 3CX version.
- **3CX updates can overwrite the nginx include, less often the snippets
  themselves.** `install.sh` re-installs `60-vm-manager.conf` every run, so
  re-running it after a 3CX update is a cheap way to restore it if a 3CX
  update ever wipes `conf/snippets/`. (Only relevant if you went the direct
  `HOST=0.0.0.0` route above: a 3CX update regenerating `nftables.conf` would
  drop your manually-added port rule too.)
- **`dial_and_play.py` / pjsua2 don't move via rsync.** `build_pjsua2.sh`
  compiles pjproject against whatever glibc/openssl/python3 the *target*
  server actually has, so rebuild there rather than copying this server's
  `/usr/local/lib/libpj*.so` files or its `pjsua2` install across — that's
  the point of shipping the build script instead of the binaries.
- **Transcription is off by default on a fresh install.** An admin has to
  turn it on via Settings > Transcription in the app itself (see
  `app/transcription_worker.py`) — this is deliberate, so a new deployment
  never silently starts running CPU-heavy local transcription or, if the
  OpenAI engine is picked, sending call audio to a third party. The local
  engine downloads a model into `models/` on first use (~75MB for the
  default "tiny" size; larger WHISPER_MODEL_SIZE values cost more), needs
  outbound internet access to Hugging Face for that, and is capped at
  `WHISPER_MEMORY_LIMIT_MB` (1GB by default, ~320MB actually used by "tiny")
  enforced by a userspace RSS watchdog, not a kernel/cgroup limit — see
  `app/transcription.py`'s docstring for why.
- **`sip-reject-watch.service`'s auto-detected `IFACE` and transport.**
  `install.sh` installs and starts this unconditionally and auto-detects the
  PBX-facing NIC from the default route, but a wrong interface (rare, but
  possible on unusual network setups) starts the service fine and silently
  never sees any traffic — verify with `tcpdump` as shown in step 2. Also
  confirm the phones' actual SIP transport (TCP vs UDP) with the same
  `tcpdump` if reject detection seems to not be working; the sniffer handles
  either already, this is just about confirming traffic is visible at all.
  If it does turn out broken, `phone_service.py` degrades gracefully
  (falls back to trusting CONFIRMED, same as before this existed) rather
  than failing outright — it'll just occasionally treat a rejected/no-answer
  call as a real pickup.
