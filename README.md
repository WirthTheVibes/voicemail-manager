# voicemail-manager

A vibecoded 3CX Voicemail Manager to better improve on functionality within the pre-installed voicemail system, while also providing the backend to serve a Yealink XML App for visual voicemail.

-------------------
# App Dependencies
- Python
  - FastAPI: API for UI functioins
  - Uvicorn: Web Server
  - psycopg2-binary: reading/writing  3CX Postgres Database
  - itsdangerous: session tokens
  - requests: 3CX Admin Spoofing
  - faster-whisper: on-server transcription
  - pywebpush: browser push notifications (not implemented yet)
  - pyjwt[crypto]: JWT signing for MS Auth and Yealink XML Browser sessions
- PJSUA
- 3CX
  - Local CDR file for accurate call path tracing.
  - 1+ free extensions for voicemail manager phone extension and group voicemail boxes. 
