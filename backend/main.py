import asyncio
import io
import logging
import os
import time
import wave
from pathlib import PurePath

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse

from providers import AZURE_ENDPOINT_ERROR, GEMINI_MIME, MAX_AUDIO, MODELS, ProviderError, Settings, transcribe
from streaming import STREAM_ERRORS, relay

ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5174,http://127.0.0.1:5174").split(",")
    if o.strip()
]
app = FastAPI(title="STT Compare", docs_url=None, redoc_url=None)
logger = logging.getLogger("stt")
batch_slots = asyncio.Semaphore(8)
live_slots = asyncio.Semaphore(12)
EXTENSIONS = {".wav", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".webm", ".ogg", ".flac", ".aac", ".opus"}


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers", [])).get(b"content-length", b"")
        if length.isdigit() and int(length) > MAX_AUDIO + 65536:
            response = JSONResponse({"detail": "Audio must be at most 24 MiB."}, status_code=413)
            return await response(scope, receive, send)
        received = 0

        async def limited():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_AUDIO + 65536:
                raise HTTPException(413, "Audio must be at most 24 MiB.")
            return message

        return await self.app(scope, limited, send)


app.add_middleware(BodyLimit)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/providers")
async def providers():
    return MODELS


@app.exception_handler(ValidationError)
async def validation_error(_request, exc: ValidationError):
    # Pydantic error details can contain the submitted API key.
    errors = exc.errors(include_input=False, include_context=False, include_url=False)
    detail = (
        AZURE_ENDPOINT_ERROR
        if any(error["loc"] == ("resource",) for error in errors)
        else "Invalid provider settings. Check model, language, resource/endpoint, and key."
    )
    return JSONResponse({"detail": detail}, status_code=422)


@app.post("/api/transcribe")
async def batch(request: Request):
    async with request.form(max_files=1, max_fields=1, max_part_size=16384) as form:
        audio = form.get("audio")
        options = form.get("settings")
        if not isinstance(audio, UploadFile):
            raise HTTPException(422, "An audio file is required.")
        if not isinstance(options, str):
            raise HTTPException(422, "Provider settings are required.")
        settings = Settings.model_validate_json(options)
        if settings.mode != "batch":
            raise HTTPException(422, "File transcription requires recorded/file mode.")
        filename = PurePath(audio.filename or "audio.wav").name
        suffix = "." + filename.rsplit(".", 1)[-1].lower()
        if suffix not in EXTENSIONS:
            raise HTTPException(
                415, "Unsupported audio extension. Use WAV, MP3, MP4, M4A, WebM, Ogg, FLAC, AAC, or Opus."
            )
        if settings.provider == "openai" and suffix not in {".wav", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".webm"}:
            raise HTTPException(
                415, "OpenAI requires WAV, MP3, MP4, MPEG, MPGA, M4A, or WebM. Convert the file or deselect OpenAI."
            )
        if settings.provider == "gemini" and suffix not in GEMINI_MIME:
            raise HTTPException(415, "Gemini requires an audio file such as WAV, MP3, M4A, or WebM, not MP4 video.")
        data = await audio.read(MAX_AUDIO + 1)
        if not data:
            raise HTTPException(422, "Audio file is empty.")
        if len(data) > MAX_AUDIO:
            raise HTTPException(413, "Audio must be at most 24 MiB.")
        if settings.provider == "sarvam" and suffix == ".wav":
            try:
                with wave.open(io.BytesIO(data), "rb") as recording:
                    duration = recording.getnframes() / recording.getframerate()
            except (wave.Error, EOFError):
                raise HTTPException(415, "Sarvam WAV uploads must be readable PCM WAV files.") from None
            if duration >= 30:
                raise HTTPException(
                    422, "Sarvam file clips must be under 30 seconds. Use live streaming for longer audio."
                )
        try:
            await asyncio.wait_for(batch_slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise HTTPException(429, "Server is busy. Please retry shortly.") from None
        started = time.monotonic()
        try:
            async with asyncio.timeout(280):
                result = await transcribe(settings, data, filename, audio.content_type or "application/octet-stream")
            return result | {
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "provider": settings.provider,
                "model": settings.model,
            }
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from None
        except (TimeoutError, httpx.TimeoutException):
            raise HTTPException(504, "Provider timed out. Try a shorter recording or retry.") from None
        except httpx.ConnectError:
            logger.warning("Provider connection failed: %s", settings.provider)
            detail = (
                "Cannot connect to Azure Speech. Use the exact endpoint from Azure Keys and Endpoint; "
                "the resource name is not always its DNS subdomain. Check networking/firewall settings. "
                "Private endpoints are not reachable from this public backend."
                if settings.provider == "azure"
                else "Cannot connect to the provider. Check network connectivity and retry."
            )
            raise HTTPException(502, detail) from None
        except httpx.HTTPError:
            logger.warning("Provider request failed: %s", settings.provider)
            raise HTTPException(502, "Provider network request failed. Check connectivity and retry.") from None
        except (KeyError, ValueError):
            logger.warning("Unexpected provider response: %s", settings.provider)
            raise HTTPException(
                502, "Provider returned an unexpected response. Check the endpoint, API version, and audio format."
            ) from None
        finally:
            batch_slots.release()


@app.websocket("/api/live")
async def live(socket: WebSocket):
    if socket.headers.get("origin") not in ORIGINS:
        await socket.close(code=1008)
        return
    await socket.accept()
    acquired = False
    try:
        async with asyncio.timeout(15):
            raw = await socket.receive_text()
        if len(raw) > 16384:
            raise ProviderError("Live settings are too large.")
        settings = Settings.model_validate_json(raw)
        if settings.mode != "live":
            raise ProviderError("Live transcription requires a supported live model.")
        try:
            await asyncio.wait_for(live_slots.acquire(), 0.1)
            acquired = True
        except TimeoutError:
            raise ProviderError("Server is busy. Please retry shortly.") from None
        async with asyncio.timeout(350):
            await relay(socket, settings)
        await socket.send_json({"type": "done"})
    except WebSocketDisconnect:
        return
    except ValidationError:
        await socket.send_json(
            {"type": "error", "message": "Invalid live settings. Check model, language, region, and key."}
        )
    except STREAM_ERRORS as exc:
        message = (
            str(exc)
            if isinstance(exc, ProviderError)
            else "Live connection failed or timed out. Check provider settings and retry."
        )
        logger.warning("Live session failed: %s", type(exc).__name__)
        try:
            await socket.send_json({"type": "error", "message": message})
        except (WebSocketDisconnect, RuntimeError, OSError):
            logger.info("Live client disconnected before error delivery")
    finally:
        if acquired:
            live_slots.release()
        try:
            await socket.close()
        except (RuntimeError, WebSocketDisconnect, OSError):
            logger.debug("Live socket already closed")
