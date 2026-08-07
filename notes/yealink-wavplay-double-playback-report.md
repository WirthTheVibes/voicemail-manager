# Yealink `Wav.Play` double-playback bug report

**Symptom:** Pressing Play on a voicemail message in the vm-manager XML
Browser app plays the message in full, then plays it again in full after a
~0.85s gap. Confirmed on two separate Yealink phone model families:

- A T5xW-series touchscreen model (firmware `96.87.0.22`, per the
  provisioning download seen in the packet capture)
- A T4x-series softkey model

**Not reproducible via any other audio path on the same phones/PBX:**

- 3CX's native voicemail IVR playback of the exact same message: plays once, correctly.
- Click-to-call playback of the same message: plays once, correctly.

Both of those use the phone's real-time call audio path (SIP/RTP). Only
`Wav.Play` -- the XML Browser command that downloads a WAV file over HTTP
and plays it from a local buffer (the same mechanism used for custom
ringtones) -- exhibits the double playback. This isolates the bug to that
one playback subsystem, not audio/codec handling in general.

## What's been ruled out, with evidence

1. **Duplicate `Wav.Play` commands in the XML response.** The raw XML
   returned for the message-detail screen was inspected directly (`curl`
   against the live server) and contains exactly one `<SoftKey>` with one
   `Wav.Play:<url>` URI. No duplication in the response itself.

2. **Duplicate HTTP requests.** Server access logs (`journalctl -u
   vm-manager -f`, watched live during a test) show exactly one GET to the
   audio-serving endpoint per Play press, every time, with a 200 response.

3. **Duplicate network transfer.** A packet capture taken on the phone
   during a reproduction shows exactly one TCP connection, one TLS
   handshake, and one continuous, unbroken download of ~53KB (matching the
   WAV file size plus HTTP/TLS overhead) -- no retransmits, no repeated
   sequence numbers, no second connection.

4. **The WAV file containing the message twice.** Checked two ways:
   - RIFF/`fmt `/`data` chunk sizes were parsed directly; all consistent,
     no trailing or duplicate chunks.
   - Full-signal autocorrelation of the decoded PCM (FFT-based, scanning
     all lags, not just a fixed 50/50 split) shows no secondary
     correlation peak beyond normal speech-periodicity noise (~0.05-0.11)
     -- nowhere near what a true in-file duplicate would produce.

5. **Non-canonical WAV framing.** 3CX writes a non-canonical 18-byte
   `fmt ` chunk (16-byte PCM format info plus a padded, unused `cbSize`
   field) rather than the canonical 16-byte PCM `fmt ` chunk. Re-muxed the
   file server-side to a canonical 16-byte `fmt ` chunk (verified via
   direct chunk inspection of the served output) before serving it to the
   phone. No change in behavior.

6. **Phone-side caching of the audio URL.** The audio URL includes a
   random per-request cache-busting query parameter, so no two Play
   presses -- or even the very first press in a session -- ever request
   the identical URL twice. No change in behavior.

7. **A stray `doneAction`/OK-key interaction with the Play softkey.**
   Originally `doneAction` (bound to the phone's physical OK/check key)
   pointed at the same URI as the Play softkey. Removed `doneAction`
   entirely and retested -- doubling persisted with no `doneAction` present
   at all, ruling this out. (`doneAction` was subsequently restored,
   pointed elsewhere, since the phone shows "Invalid URI" on OK press if
   it's absent -- an unrelated, now-fixed issue.)

8. **The mark-heard SIP NOTIFY (MWI update) triggering a parallel native
   playback.** Every Play press also sends a real SIP NOTIFY to update the
   mailbox's MWI count, which raised the possibility of an unrelated
   native voicemail-alert behavior firing in parallel. Disabled this call
   entirely and retested -- doubling persisted with the call removed. (Also
   subsequently restored, since it wasn't the cause and the app needs it
   for the mark-heard-on-play requirement.)

## Direct acoustic confirmation

A recording of the double playback was captured and analyzed. Two loud
segments in the recording (measured at ~6.9-9.05s and ~9.9-12.05s) were
cross-correlated against each other: **0.91 normalized correlation at
essentially zero lag**, confirming they are the same audio content,
genuinely played twice, roughly 0.85 seconds apart. This is not an
artifact of recording/room echo (which would show smaller-amplitude,
closely-spaced, decaying reflections within milliseconds, not a
full-amplitude repeat of matching duration ~1 second later).

## Conclusion

With every input to `Wav.Play` (the XML response, the request count, the
network transfer, the file's byte-level framing, and the file's actual
decoded content) independently verified correct, and the same audio
content confirmed to play correctly exactly once via two different
call-audio-path mechanisms on the same phones, the double playback is
isolated to a defect in the phone's local WAV-file playback engine
(the `Wav.Play` XML Browser command / custom-ringtone subsystem)
itself -- reproducible across at least two Yealink model families, likely
sharing a common firmware SDK component for that feature.

## Suggested next steps for Yealink

- Test `Wav.Play` against a plain, minimal, non-3CX-hosted WAV file and
  XML page (isolate from this deployment's provisioning/PBX entirely) to
  confirm the bug is general to the phone/firmware, not specific to this
  file or template.
- Check whether firmware newer than `96.87.0.22` (T5xW) fixes this.
- If reproducible on a clean setup, this warrants a formal bug report to
  Yealink with the above evidence attached (WAV chunk dump, packet
  capture, and the recording's cross-correlation result).
