# HTTP API (v6)

Base: `http://<PC-LAN-IP>:8765`. Browser and client must use the login session cookie. Non-GET requests (except login) also require the `X-CSRF-Token` returned at login/session check.

| Method | Route | Use |
| --- | --- | --- |
| `GET` | `/api/session` | Current login status, `{authenticated, csrf}` |
| `POST` | `/api/login` | `{password}`; issues an HttpOnly session cookie and CSRF token |
| `POST` | `/api/logout` | End session |
| `GET` | `/api/models` | Available local LLM/ASR model IDs (absolute local paths) |
| `GET` | `/api/status` | Backend state, ownership, active/queued jobs |
| `POST` | `/api/start` | `{which:'llm'|'asr', model:'<listed ID>'}` |
| `POST` | `/api/stop` | `{which:'llm'|'asr'}` (app-managed servers only) |
| `POST` | `/api/translate` | `{text,filename,kind,source,target,llm_model}`; returns job ID |
| `POST` | `/api/upload?mode=asr&format=srt&language=ja&asr_model=<encoded-id>` | Raw media bytes with `X-Upload-Name`; returns job ID |
| `GET` | `/api/jobs` | Up to 150 recent jobs, ordered newest first |
| `GET` | `/api/job/<32hex>` | One job, including `has_audio`, `has_transcript`, `has_partial` |
| `GET` | `/api/preview/<32hex>?kind=result` | `{text,truncated,kind}`; `kind` can be `result`, `transcript`, `partial` |
| `GET` | `/api/download/<32hex>?kind=result` | Full file attachment; `kind` likewise supports transcript/partial |
| `GET` | `/api/audio/<32hex>` | Authenticated audio stream, supports `Range: bytes=start-end`, returns 206 for partial range |
| `POST` | `/api/cancel` | `{id}` cancel queued/running job |
| `POST` | `/api/delete` | `{id}` remove completed/failed/cancelled job and all its saved files |
| `POST` | `/api/stopweb` | Exit web app when no jobs running |
| `POST` | `/api/poweroff` | `{password,confirmation:'SHUTDOWN'}`; requires configured permissions and idle queue |

## Upload details

`POST /api/upload?mode=asr_translate&format=srt&source=ja&target=en&language=ja&asr_model=<url-encoded-model-id>&llm_model=<url-encoded-model-id>`

Request body is the **raw media file**, not multipart: `Content-Type: application/octet-stream` and header `X-Upload-Name: <url-encoded filename>`.

Accepted media extensions: WAV, MP3, FLAC, OGG, OPUS, M4A, AAC, WEBM, MP4, MKV, MOV. File limit 100 MiB.

## Preview versus download

`GET /api/preview/:id` is a lightweight text response for displaying a result in the app. It reads at most 350,001 characters and sets `truncated=true` if the preview was capped. To retrieve the entire result, use `/api/download/:id`.

## Audio byte ranges

`GET /api/audio/<id>` returns the MP3 preview when available, otherwise the saved uploaded media. The server responds with `Accept-Ranges: bytes`; a partial request such as `Range: bytes=0-99` returns `206 Partial Content` and `Content-Range: bytes 0-99/<size>`. Invalid or out-of-range requests return HTTP 416. No disk path is exposed to the caller.

## Compatibility warnings

The API is an internal single-user interface, not a stable third-party SDK. Avoid connecting this web server directly to the public Internet. Run model backends on loopback and serve the web UI over a trusted private network.
