# STT Compare

A bring-your-own-key speech-to-text playground. Record once, upload a file, or
stream a microphone to multiple providers and read independent transcripts side
by side. This is a separate app from [TTS Compare](https://sidpandey3006.github.io/tts-compare/).

## What works

| Provider | Recorded / file | Live microphone | Speaker labels |
|---|---|---|---|
| Azure Speech | Fast Transcription, Azure Transcribe | Speech SDK continuous recognition | File only |
| OpenAI | `gpt-transcribe`, legacy 4o / Whisper models | Not implemented | Not implemented |
| Deepgram | Nova 3 / Nova 2 | True WebSocket streaming | File and live |
| Soniox | Asynchronous v5 / v4 file jobs | Real-time v5 / v4 WebSocket | File and live |

Model access, locales, multilingual coverage, and account availability differ by
provider. A language selection does not imply every provider supports it.
OpenAI's legacy Whisper / 4o transcription models retire February 26, 2027;
prefer `gpt-transcribe`.

- Record microphone audio as the same 16 kHz, mono, little-endian PCM for every
  provider. Live mode shows interim and final text; it does not simulate streaming
  by repeatedly submitting files.
- Upload up to 24 MiB. Mic recordings and live sessions are limited to five minutes.
  File requests time out after approximately five minutes; use shorter clips.
- Choose models, language, speaker labels, Deepgram formatting, live endpoint
  silence, and OpenAI / Soniox context.
- Replay recorded/uploaded audio, copy transcripts, inspect provider-native
  timestamp/speaker details, and export a key-free JSON comparison.
- Each card reports its own failure. Timing means observed backend request time,
  first text after the first audio frame, or live session duration, **not** a
  controlled latency benchmark. No estimated pricing or accuracy scores are invented.

**Recorded / file is not the same as a durable bulk-job queue.** Azure uses its
synchronous Fast Transcription API, OpenAI and Deepgram use file endpoints, and
Soniox creates and polls an asynchronous job. Large production batch workflows,
Azure's separate Batch Transcription service, OpenAI Realtime, Google, and
AssemblyAI are not implemented in this version. Uploaded files cannot be replayed
through live streaming; use the microphone for live comparisons.

## Local setup

Python 3.12 and Node 22.12+ are recommended.

```powershell
cd C:\Users\sidpandey\stt-compare
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd backend
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001
```

In another terminal:

```powershell
cd C:\Users\sidpandey\stt-compare\frontend
npm ci
npm run dev
```

Open **http://127.0.0.1:5174**. Vite proxies REST and WebSocket traffic to port 8001.
Set `API_PROXY` to change the local API address. On macOS/Linux use
`backend/.venv/bin/python` instead of `backend\.venv\Scripts\python.exe`.
Microphone capture requires HTTPS or localhost and AudioWorklet support.

### Provider credentials

Open **Settings** on each included provider card and enter your own API key.
Azure file mode additionally requires the Speech **resource name**, not a
Foundry project URL. Azure live mode needs the resource **region** (e.g. `eastus`)
and an explicit locale. Azure file mode uses the resource-hosted
`2025-10-15` Fast Transcription API.

Keys are kept in React memory, not local storage, URLs, exports, or cookies.
They pass through this backend, so host it somewhere you trust. Keys and audio
are sent only to selected providers when starting a comparison.

## Deployment: Pages + Render

1. Push this repository to GitHub. Under Settings > Pages, select **GitHub Actions**.
2. In Render, create a Blueprint from the repository. `render.yaml` provisions a
   Docker backend named `stt-compare-api`. This may require authorizing Render's
   GitHub app for the new repository.
3. Keep `ALLOWED_ORIGINS=https://sidpandey3006.github.io` for this account, or
   replace it with your exact frontend origin (no path or trailing slash).
4. Verify `https://YOUR-SERVICE.onrender.com/api/health` returns `{"ok":true}`.
5. Set the GitHub Actions repository variable **API_BASE** to the service HTTPS
   origin and rerun **Validate and deploy STT Compare**. Do not put keys in variables.

Pages can deploy before `API_BASE` is set, but shows an explicit unconfigured
backend banner and will not send transcription requests.
The workflow validates Python, TypeScript, lint, and mocked browser interactions
before deploying. Render free instances sleep while idle; initial requests may
need a warm-up. WebSockets must be supported by your backend host.

## Privacy and operational limits

This app does not intentionally persist audio, keys, or transcripts, and does not
log request bodies or raw upstream errors. File parsing may spool an upload to
an OS temporary file, removed when the request closes. Your selected providers
have their own retention policies and can charge your account. **Do not upload
sensitive audio.** Use restricted keys and provider spending limits.

Soniox file mode attempts to delete exactly the file and transcription job created
by the request, even on processing failures. Cleanup failures are logged without
keys and shown as warnings or explicit errors; verify deletion in the Soniox
console if cleanup is not confirmed. Abrupt server shutdown can prevent cleanup.
Cancel closes browser requests/streams; already-submitted upstream file work may
continue and incur charges.

Origins are allowlisted for both REST browser access and WebSockets. The backend
limits simultaneous provider requests and bounds audio and session sizes. CORS is
not authentication: this is a personal playground, not a production multi-tenant
service. Add user authentication and infrastructure-level rate limits before
opening a shared managed-key deployment.

The Azure Speech SDK uses native platform libraries. The Docker image includes
Linux dependencies; test the image on the target host. Windows SDK loading is
covered locally; macOS execution requires verification on a Mac.

## Checks

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
cd ..\frontend
npm run lint
npm run build
npx playwright install chromium
npm test
```

Tests mock billable providers. They cover request contracts, independent errors,
upload limits, secret-safe errors, Soniox cleanup, real PCM relay and finalization
for all three live adapters, microphone WAV capture, interim text, and exports.
They do **not** establish successful transcription with real paid provider accounts.

## API references

- [Azure Fast Transcription](https://learn.microsoft.com/azure/ai-services/speech-service/fast-transcription-create)
- [Azure continuous recognition](https://learn.microsoft.com/azure/ai-services/speech-service/how-to-recognize-speech)
- [OpenAI file transcription](https://developers.openai.com/api/docs/guides/speech-to-text)
- [Deepgram live API](https://developers.deepgram.com/reference/speech-to-text/listen-streaming)
- [Soniox asynchronous transcription](https://soniox.com/docs/stt/async/async-transcription)
- [Soniox real-time API](https://soniox.com/docs/api-reference/stt/websocket-api)
