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
| ElevenLabs | Scribe v2 / v1 | Scribe v2 Realtime, partials while speaking, final on Stop | File only |
| Sarvam | Saaras v4 / v3, clips under 30 seconds | Saaras v4 / v3-realtime (beta), true partials | Not implemented |
| Gemini | `gemini-3.5-transcribe`, dedicated transcription | Not implemented | File only |

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
  silence, OpenAI / Soniox context, and Sarvam normalized/verbatim/code-mixed/Roman-script output.
- Use **Add provider** to search and add adapters, including ElevenLabs, Sarvam,
  and Gemini. Adding a card includes it and opens its settings. Remove cards to
  clear their keys/results. Include all affects only visible cards.
- Replay recorded/uploaded audio, copy transcripts, inspect provider-native
  timestamp/speaker details, and export a key-free JSON comparison.
- Each card reports its own failure. Timing means observed backend request time,
  first text after the first audio frame, or live session duration, **not** a
  controlled latency benchmark. No estimated pricing or accuracy scores are invented.

**Recorded / file is not the same as a durable bulk-job queue.** Azure uses its
synchronous Fast Transcription API, OpenAI and Deepgram use file endpoints, and
Soniox creates and polls an asynchronous job. Large production batch workflows,
Azure's separate Batch Transcription service, OpenAI Realtime, Gemini Live,
Sarvam's long-audio batch API, and AssemblyAI are not implemented in this version. Uploaded files cannot be replayed
through live streaming; use the microphone for live comparisons.

Sarvam's file endpoint accepts **clips under 30 seconds**, not five-minute
recordings. PCM WAV duration is checked before submission; for compressed formats,
the provider enforces this limit. English locale hints are explicitly mapped to
`en-IN`; unsupported non-Indian languages are rejected. Auto language detection is
available. REST and realtime use their respective documented `od-IN` / `or-IN`
Odia codes. Speaker labels require a different batch API and are not offered here.

ElevenLabs live uses manual commits: interim text updates while speaking, then
Stop commits the final transcript. File mode supports word timestamps and speaker
labels; live speaker labels and endpoint-silence tuning are not offered here.

Gemini uses the dedicated Transcribe model in verbatim mode, with word timestamps
and optional speaker labels. Audio is inline in the Interactions request with
`store: false`, avoiding a persistent Files API upload. This setting opts out of
interaction storage; it is **not** a promise of zero provider retention. MP4 video
is not accepted by this adapter; use M4A or another supported audio format.

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

Use **Add provider** for additional adapters, then open **Settings** on each
included card and enter your own API key. ElevenLabs uses an ElevenLabs API key,
Sarvam a subscription key, and Gemini a Gemini API key with model access.
For Azure file mode, paste the exact HTTPS resource **Endpoint** from Azure/Foundry
**Keys and Endpoint** into **Azure resource name or endpoint**.
Foundry resource origins such as `https://your-resource.services.ai.azure.com/`,
regional origins such as `https://westus.api.cognitive.microsoft.com/`,
and custom-subdomain origins such as `https://your-subdomain.cognitiveservices.azure.com/`
are supported without rewriting the chosen hostname. A bare resource name still works if its matching custom subdomain
exists, but the resource's display name is not necessarily its DNS subdomain.
Use the resource origin, not a Foundry project URL: remove any `/api/projects/...`
suffix or other API path. Do not add a query string, credentials, or port.
The key must belong to that resource; Speech availability still depends on its
region, permissions, and service support.
There is no need to enable a custom domain to use a regional endpoint.
Azure live mode needs the resource **region** (e.g. `eastus`) and an explicit
locale or **Auto-detect** with candidate languages. Azure file mode uses the
`2025-10-15` Fast Transcription API.

### Auto language detection

The shared **Auto-detect** choice is not a promise that every provider supports
word-by-word code switching. The UI explains provider-specific behavior before
recording, and each card shows the language configuration for its next run.

- Azure file auto mode omits a fixed locale and uses the service's multilingual
  model. Its language coverage differs from live recognition.
- Azure live auto mode uses the Speech SDK's **continuous language identification**
  through the region's Speech v2 endpoint, not a literal `auto` locale. Choose
  2-10 likely candidate languages in the visible selector; the initial selection
  is English and Hindi. Only one locale per base language is allowed. Azure can
  detect language changes between utterances, but not within the same sentence.
  The selected candidates must include the spoken language; Azure can return a
  candidate even when the actual language is outside the list. Detected locales
  are preserved in Details / JSON.
- Other adapters use their provider's documented automatic-language behavior;
  supported languages, mixed-language recognition, and formatting vary by model.

Live comparisons are disabled when Azure has fewer than two candidates. This
constraint does not carry into file mode or a fixed-language live run.

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
and builds the Linux deployment image with a native Azure SDK initialization
check before deploying. Render free instances sleep while idle; initial requests may
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
for all five live adapters, provider additions/removals, microphone WAV capture, interim text, and exports.
They do **not** establish successful transcription with real paid provider accounts.

## API references

- [Azure Fast Transcription](https://learn.microsoft.com/azure/ai-services/speech-service/fast-transcription-create)
- [Azure Fast Transcription endpoint reference](https://learn.microsoft.com/rest/api/speechtotext/transcriptions/transcribe?view=rest-speechtotext-2025-10-15)
- [Azure continuous recognition](https://learn.microsoft.com/azure/ai-services/speech-service/how-to-recognize-speech)
- [Azure language identification](https://learn.microsoft.com/azure/ai-services/speech-service/language-identification)
- [OpenAI file transcription](https://developers.openai.com/api/docs/guides/speech-to-text)
- [Deepgram live API](https://developers.deepgram.com/reference/speech-to-text/listen-streaming)
- [Soniox asynchronous transcription](https://soniox.com/docs/stt/async/async-transcription)
- [Soniox real-time API](https://soniox.com/docs/api-reference/stt/websocket-api)
- [ElevenLabs file transcription](https://elevenlabs.io/docs/api-reference/speech-to-text/convert)
- [ElevenLabs realtime transcription](https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime)
- [Sarvam file transcription](https://docs.sarvam.ai/api-reference/speech-to-text/transcribe)
- [Sarvam realtime transcription](https://docs.sarvam.ai/api-reference/speech-to-text/transcribe/realtime/ws)
- [Gemini Transcribe](https://ai.google.dev/gemini-api/docs/transcribe)
