# Architecture and persistence

```text
Phone / desktop browser ──HTTP :8765──> Local AI Studio Python
       (password)                        │
                                         ├── HTML/CSS/JS, UI and auth
                                         ├── queued background jobs
                                         ├── data/jobs/ durable files
                                         ├── GET /api/preview/:id (text)
                                         ├── GET /api/audio/:id (Range / audio)
                                         │
                                         ├── localhost :8080 llama-server ── GGUF translation
                                         └── localhost :8178 CrispASR ─── ASR GGUF
```

## Sequence of a combined job

1. Browser submits raw media bytes to `POST /api/upload?mode=asr_translate...`, with model IDs and language/format parameters in the query string.
2. Server validates the session, CSRF, file extension and size, generates a UUID, saves media as `jobs/<id>.<ext>`, writes `<id>.json`, and queues it.
3. The browser can close after the upload response. A single Python worker drives the job independently.
4. Worker ensures CrispASR is ready, converts audio with FFmpeg when available, creates an MP3 playback preview where possible, and calls CrispASR over localhost HTTP.
5. Worker saves `<id>.transcript`, then ensures llama-server is ready. It batches subtitle dialogue (without timestamps), verifies marker alignment and context budget, retries smaller batches on failure, and periodically saves `<id>.partial`.
6. On success, worker writes `<id>.result`, clears `.partial`, marks the job done and retains the original media/MP3 for playback. Converted temporary `-16k.wav` is removed.
7. Browser lists jobs with `GET /api/jobs`; on selection, loads text from `GET /api/preview/:id` and audio from `GET /api/audio/:id`. Finished files are downloaded with `GET /api/download/:id`.
8. User deletes the job with `POST /api/delete`; all saved files belonging to that job are removed, including audio.

## Files and ownership

- `config.json`: path settings, ports, salted password hash; mode 0600.
- `<id>.json`: state and parameters, no raw source text or audio; atomically replaced.
- `<id>.input.txt`: raw text while waiting/in-progress; removed after text completion.
- `<id>.<media extension>`: uploaded media retained for playback after ASR completion.
- `<id>.audio.mp3`: optional FFmpeg playback conversion.
- `<id>.transcript`: original speech transcription, especially combined jobs.
- `<id>.partial`: partial translated output for failure recovery.
- `<id>.result`: finished translated/transcribed output.
- `web.log`, `llm.log`, `asr.log`: debugging logs, may contain filenames/errors.

## Job states

`uploading` → `queued` → `loading` → `running` → `done` or `error` or `cancelled`.

On startup, `recover_jobs()` reloads JSON metadata. It requeues queued/loading/running jobs when the source exists and marks interrupted uploads failed. Completed jobs remain available. It also imports orphan `.result` files from older versions.

## Limits

- Input limits: 10 MiB for source text and 100 MiB for uploaded media.
- `llama-server` context: 32,768 tokens (by default); text chunk target: 1,800 tokens, max output 4,096 per request; output-length retries split again.
- Web text preview: 350,000 characters per request; download is untruncated.
- Browser playback requires audio codec support; the MP3 conversion is best effort.

## Security boundaries

- HTML, CSS, JS, and login/session checks served by one Python process.
- All private API endpoints require session; POST endpoints require a CSRF token.
- Model servers listen only on `127.0.0.1`.
- Web process checks LAN private IP, loopback, or Tailscale's carrier-grade range.
- Audio path is UUID-derived from registered jobs; byte-range requests cannot select arbitrary filesystem paths.
- Poweroff requires password re-entry and an explicit `SHUTDOWN` confirmation, and is refused while jobs are active.
