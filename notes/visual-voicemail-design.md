# Yealink Visual Voicemail — Design Spec

A PIN-gated voicemail list on a Yealink desk phone's own screen, built
entirely on Yealink's XML Browser feature (no app/softphone involved).
Phone presses a key → GETs a URL → server returns an XML object → phone
renders it. Every screen transition is just another GET to a
server-generated URL.

## Protocol constraints (from Yealink's XML Browser spec — not optional)

- Phone only ever does HTTP **GET** to browse/pull. No POST body, no
  custom headers, no reliable cookies. Any auth token has to live in the
  URL query string — that's a spec limitation, not a design choice.
- **No hook-state-change event exists.** Can't detect hang-up, can't
  auto-stop or switch audio output when the handset goes down.
- **No true multi-column table.** `TextMenu`/`Directory` rows are single
  plain strings. `FormattedTextScreen` has per-line align/color/size but
  is non-interactive — can't have selectable rows.
- `Directory`'s `MenuItem.URI` is a **phone number to dial**, not a
  navigable link — wrong object for a list where selecting an item should
  open a detail screen. Use `TextMenu` for that.
- `Wav.Play`/`Wav.Stop` are the only audio commands: fire-and-forget, no
  "finished" callback, no pause/resume, no way to control ring-vs-call
  volume path (that's tied to real SIP call state, which `Wav.Play` never
  creates).
- `LockIn="yes"` on a screen suppresses default off-hook/line-key/digit
  key/Cancel/OK behavior (e.g. stops "lift handset → tries to dial")
  without disabling your own `SoftKey` entries.
- `doneAction` binds the OK/check key on `TextScreen` (and doesn't exist
  on touchscreen models) -- this fires *unconditionally* whenever OK is
  pressed, regardless of `LockIn`. If left unset, this phone shows
  "Invalid URI" on OK press rather than no-op'ing, so every `TextScreen`
  with a physical OK key needs a `doneAction`. On the message detail
  screen it's bound to the same `Wav.Play:` URI as the Play softkey, so OK
  plays the message too -- confirmed this doesn't reintroduce the
  double-playback bug (that's a Yealink firmware defect independent of
  what triggers `Wav.Play`, see the report below), and matches how the
  screen behaved before this got investigated.
- **`Wav.Play` plays the full message twice, confirmed on both a T4x and a
  T5xW phone against this deployment (2026-07-30) -- a Yealink firmware
  defect in that specific playback mechanism, not fixable from this app.**
  The same audio plays correctly exactly once via 3CX's native voicemail
  IVR and via click-to-call (both real SIP/RTP call-audio paths) on the
  same phones -- only the local-file `Wav.Play` path (shared with the
  custom-ringtone feature) double-plays. Full investigation, everything
  ruled out server-side, and the acoustic proof are in
  `notes/yealink-wavplay-double-playback-report.md` -- that's the report
  to hand to Yealink/a reseller if this needs escalating. Don't re-litigate
  the request flow, caching, WAV framing, or MWI-notify angles without
  reading that first; they're each individually disproven with evidence
  there.

## Data needed

- **Voicemail messages**: id, extension, caller_id, caller_name,
  received_at, duration_sec, callback_number, audio location, is_read.
- **Extension → PIN** mapping (no row = no PIN required for that
  extension).

## Auth model

1. `menu?ext=X` with no/invalid token → `InputScreen` PIN prompt
   (numeric, `password="yes"` to mask). No session exists, so `ext` is
   carried through the submit by adding a second, read-only `InputField`
   (`type="empty" editable="no"`) alongside the PIN field — both get
   appended to the submit URL.
2. Correct PIN → mint a short-lived signed token (HMAC/itsdangerous or
   equivalent), embed it in every link on every subsequent screen.
3. **Every message-scoped route re-derives the authorizing extension from
   that message's own record — never from the request's `ext` param.**
   Otherwise a valid token for extension A plus a guessed message id
   belonging to extension B gets through.
4. The same rule applies to the raw audio file route — gate it identically,
   or it's `curl`-able with zero auth.

## Screens needed, with the actual XML

Base URL below is a placeholder (`https://your-server`) — substitute
whatever address the phone can actually reach.

**PIN prompt** (`InputScreen`):

```xml
<YealinkIPPhoneInputScreen Beep="no" Timeout="60" defaultIndex="2">
  <Title wrap="yes">Enter Voicemail PIN</Title>
  <URL>https://your-server/vvm/auth/check</URL>
  <InputField type="empty" editable="no">
    <Parameter>ext</Parameter>
    <Default>1000</Default>
  </InputField>
  <InputField type="number" password="yes">
    <Prompt>PIN:</Prompt>
    <Parameter>pin</Parameter>
    <Default></Default>
  </InputField>
</YealinkIPPhoneInputScreen>
```

On Submit the phone appends both fields to `<URL>` in order:
`?ext=1000&pin=<what was typed>`.

**Voicemail list** (`TextMenu` — token embedded in every item's link):

```xml
<YealinkIPPhoneTextMenu style="numbered" Beep="no" Timeout="60">
  <Title wrap="yes">Voicemail (4)</Title>
  <MenuItem>
    <Prompt>Support Line - 2026-07-28 13:15</Prompt>
    <URI>https://your-server/vvm/msg/4?ext=1000&amp;token=SIGNED_TOKEN</URI>
  </MenuItem>
  <MenuItem>
    <Prompt>* Bob's Auto Shop - 2026-07-28 12:53</Prompt>
    <URI>https://your-server/vvm/msg/2?ext=1000&amp;token=SIGNED_TOKEN</URI>
  </MenuItem>
  <SoftKey index="1"><Label>Open</Label><URI>SoftKey:Select</URI></SoftKey>
  <SoftKey index="4"><Label>Exit</Label><URI>SoftKey:Exit</URI></SoftKey>
</YealinkIPPhoneTextMenu>
```

`*` prefix marking unread is our own convention, not a Yealink attribute.
Empty-list case is a plain `TextScreen` instead ("No voicemail messages.").

**Message detail** (`TextScreen` — `LockIn` only, no `doneAction`):

```xml
<YealinkIPPhoneTextScreen Beep="no" Timeout="60" LockIn="yes">
  <Title wrap="yes">Message #4</Title>
  <Text>From: Support Line (5878889765)
Received: 2026-07-28 13:15
Duration: 30s</Text>
  <SoftKey index="1"><Label>Play</Label>
    <URI>https://your-server/vvm/msg/4/play?ext=1000&amp;token=SIGNED_TOKEN</URI></SoftKey>
  <SoftKey index="2"><Label>Call</Label><URI>Dial:5878889765</URI></SoftKey>
  <SoftKey index="3"><Label>Delete</Label>
    <URI>https://your-server/vvm/msg/4/delete?ext=1000&amp;token=SIGNED_TOKEN</URI></SoftKey>
  <SoftKey index="4"><Label>Back</Label>
    <URI>https://your-server/vvm/mailbox/1000/messages?...</URI></SoftKey>
</YealinkIPPhoneTextScreen>
```

**Play** (`Execute` — non-UI, fire-and-forget):

```xml
<YealinkIPPhoneExecute Beep="no">
  <ExecuteItem URI="Wav.Play:https://your-server/vvm/audio/4.wav?ext=1000&amp;token=SIGNED_TOKEN"/>
</YealinkIPPhoneExecute>
```

There's deliberately no Stop softkey/route anymore. It used to toggle via
an in-memory "is this message currently playing" flag with no ground
truth from the phone (still true, per the no-hook-state-change point
above) — but the flag was also the mechanism the doneAction race above
was fighting over, and the phone already stops playback when the screen
navigates away (e.g. pressing Back), so Stop was both buggy and redundant.

**New-message push** (optional, server-initiated, not phone-pulled —
`Status`):

```xml
<YealinkIPPhoneStatus Beep="yes" SessionID="vvm-1001" Timeout="0">
  <Message Size="normal" Align="left" Color="green" Icon="Message">1 New Voicemail(s)</Message>
</YealinkIPPhoneStatus>
```

Sent via `POST http://<phone_ip>/servlet?push=xml`, body `xml=<...>`.
Requires the phone's `push_xml.server` setting to include this server's
address, and requires knowing the phone's IP per extension.

## Gotchas actually hit while building this — don't rediscover them

- **Every screen past the first hung on "Loading, please wait" forever,
  with no error anywhere.** Root cause: the base URL baked into every
  generated link didn't match the address the phone actually used (it
  was going through a reverse proxy). The first page loaded fine because
  that URL was typed directly into the phone's key config; every
  subsequent link the *server* generated pointed at the wrong host, so
  the phone silently couldn't reach it. There is no error surfaced for
  this — "stuck on Loading" is the only symptom. The base URL used to
  build every link must be the exact address the phone uses, full stop.
- **Lifting the handset while a screen was open tried to place a call.**
  Default phone behavior: off-hook with `LockIn="no"` (the default)
  enters the pre-dial screen. Fixed with `LockIn="yes"` on that screen —
  confirmed this does *not* disable soft keys, only the implicit
  off-hook/dial/line-key/digit-key/Cancel/OK behaviors.
- **A bare `curl` could pull the raw audio file with zero credentials.**
  Only the menu/PIN-check routes originally validated the token —
  everything downstream (detail, play, delete, the audio file itself)
  trusted the request blindly. Fixed by validating the token against the
  message's *actual* owning record on every route that touches one, not
  just the entry point.
- **Volume rocker adjusts ring volume, not call volume, during playback.**
  Confirmed as a hard platform limitation, not a bug: `Wav.Play` never
  creates a real SIP call, so the phone's call-state machine still
  considers itself idle, and idle = ring volume at the firmware level.
  Nothing in the XML API can intercept or remap the physical volume
  keys — there's no matching hook for it anywhere in the spec. The only
  real fix is routing playback through an actual call leg (e.g. dialing
  into a PBX IVR extension) instead of `Wav.Play` — a materially
  different architecture.
- **Asked for a 3-column table view; not achievable.** Checked every
  object type — none support grid/table layout. Considered switching the
  list to `Directory` since it *sounds* like the right fit, but its
  `MenuItem.URI` is dial-only, not link-navigable, so it can't open a
  detail screen — `TextMenu` is the only option that supports navigation.
  Landed on a single formatted line per row instead (`Name - Date`).
- **No way to auto-stop or switch audio output on hang-up.** There is no
  hook-state-change event in the spec at all. `LockIn` only suppresses
  the *dial* action on off-hook; nothing runs code on hang-up. Stop has
  to be a soft key the user presses themselves.
- WAV encoding compatibility for local playback isn't guaranteed across
  phone models — 8kHz/16-bit PCM mono (the same profile Yealink expects
  for custom ringtones) is the safe target if audio silently fails to
  play.
