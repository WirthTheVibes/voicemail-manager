"""
Standalone worker invoked as `python3 -m app.transcribe_subprocess <wav_path>`
by transcription.py's local engine.

Runs in its own process for two reasons: faster-whisper/ctranslate2 costs
~700MB resident once loaded, which has no business living in the main app's
event-loop process; and it lets transcription.py's watchdog thread enforce
config.WHISPER_MEMORY_LIMIT_MB by killing *this* process on its own,
without touching the rest of the app.

Prints one line of JSON ({"text": ..., "words": [{"word","start","end"}, ...]})
to stdout on success -- "words" backs the click-to-seek/highlight-as-it-plays
transcript in the detail panel (see app.js's wireTranscriptWords). Any
exception is left to print its traceback to stderr with a non-zero exit
code, which transcription.py surfaces as a TranscriptionError.
"""
import json
import sys

from . import config


def main():
    wav_path = sys.argv[1]
    # Imported here, not at module load, so `python3 -m app.transcribe_subprocess`
    # never pays this cost unless it's actually about to transcribe.
    from faster_whisper import WhisperModel

    model = WhisperModel(
        config.WHISPER_MODEL_SIZE,
        device="cpu",
        compute_type=config.WHISPER_COMPUTE_TYPE,
        cpu_threads=config.WHISPER_CPU_THREADS,
        download_root=str(config.WHISPER_MODEL_CACHE_DIR),
    )
    segments, _info = model.transcribe(wav_path, beam_size=5, vad_filter=True, word_timestamps=True)
    text_parts = []
    words = []
    for segment in segments:
        text_parts.append(segment.text.strip())
        for w in segment.words or []:
            words.append({"word": w.word.strip(), "start": round(w.start, 2), "end": round(w.end, 2)})
    text = " ".join(text_parts).strip()
    print(json.dumps({"text": text, "words": words}))


if __name__ == "__main__":
    main()
