import asyncio
import base64
import json
from contextlib import suppress
from urllib.parse import urlencode

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from providers import ProviderError, Settings, deepgram_options, soniox_text

MAX_SECONDS = 300
SAMPLE_RATE = 16000
MAX_PCM = MAX_SECONDS * SAMPLE_RATE * 2


async def receive_audio(browser: WebSocket, write, finish):
    total = 0
    async with asyncio.timeout(MAX_SECONDS + 30):
        while True:
            message = await browser.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            audio = message.get("bytes")
            if audio is not None:
                total += len(audio)
                if len(audio) > 32768 or len(audio) % 2 or total > MAX_PCM:
                    raise ProviderError("Audio exceeds the five-minute limit or is not valid mono PCM16.")
                await write(audio)
            elif message.get("text") == '{"type":"stop"}':
                await finish()
                return
            else:
                raise ProviderError("Invalid live control message.")


async def relay(browser: WebSocket, s: Settings):
    if s.provider == "azure":
        await azure_live(browser, s)
        return
    if s.provider in {"elevenlabs", "sarvam"}:
        await json_live(browser, s)
        return
    if s.provider == "deepgram":
        params = deepgram_options(s) | {
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "interim_results": "true",
            "endpointing": s.endpoint_ms,
        }
        url = f"wss://api.deepgram.com/v1/listen?{urlencode(params)}"
        headers = {"Authorization": f"Token {s.key}"}
    else:
        url = "wss://stt-rt.soniox.com/transcribe-websocket"
        headers = {"Authorization": f"Bearer {s.key}"}
    async with connect(url, additional_headers=headers, open_timeout=15, max_size=2**20) as upstream:
        if s.provider == "soniox":
            config = {
                "model": s.model,
                "audio_format": "pcm_s16le",
                "sample_rate": SAMPLE_RATE,
                "num_channels": 1,
                "enable_speaker_diarization": s.diarize,
                "enable_endpoint_detection": True,
                "max_endpoint_delay_ms": s.endpoint_ms,
            }
            if s.language != "auto":
                config["language_hints"] = [s.lang]
            if s.prompt:
                config["context"] = {"text": s.prompt}
            await upstream.send(json.dumps(config))

        async def finish():
            await upstream.send(json.dumps({"type": "CloseStream"}) if s.provider == "deepgram" else "")

        async def results():
            async for raw in upstream:
                event = json.loads(raw)
                if event.get("error_code") or event.get("type") == "Error":
                    raise ProviderError("Provider rejected the stream. Check key, model, language, and quota.")
                if s.provider == "deepgram" and event.get("type") == "Results":
                    alternative = event["channel"]["alternatives"][0]
                    await browser.send_json(
                        {
                            "type": "transcript",
                            "text": alternative["transcript"],
                            "final": event.get("is_final", False),
                            "segments": alternative.get("words", []),
                        }
                    )
                elif s.provider == "soniox":
                    tokens = event.get("tokens", [])
                    final = [t for t in tokens if t.get("is_final")]
                    interim = [t for t in tokens if not t.get("is_final")]
                    if final:
                        await browser.send_json(
                            {
                                "type": "transcript",
                                "text": soniox_text(final),
                                "final": True,
                                "segments": final,
                                "join": "",
                            }
                        )
                    await browser.send_json({"type": "transcript", "text": soniox_text(interim), "final": False})
                    if event.get("finished"):
                        return

        await browser.send_json({"type": "ready"})
        await run_live(browser, upstream.send, finish, results)


async def run_live(browser: WebSocket, write, finish, results):
    sender = asyncio.create_task(receive_audio(browser, write, finish))
    reader = asyncio.create_task(results())
    try:
        done, _ = await asyncio.wait({sender, reader}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if sender in done:
            await asyncio.wait_for(reader, timeout=20)
        elif not sender.done():
            raise ProviderError("Provider ended the live session before recording stopped.")
    finally:
        for task in (sender, reader):
            task.cancel()
        await asyncio.gather(sender, reader, return_exceptions=True)


async def json_live(browser: WebSocket, s: Settings):
    eleven = s.provider == "elevenlabs"
    if eleven:
        params = {
            "model_id": s.model,
            "audio_format": "pcm_16000",
            "commit_strategy": "manual",
            "include_timestamps": "false",
        }
        if s.language != "auto":
            params["language_code"] = s.lang
        url = f"wss://api.elevenlabs.io/v1/speech-to-text/realtime?{urlencode(params)}"
        headers = {"xi-api-key": s.key}
    else:
        params = {
            "model": s.model,
            "language_code": s.sarvam_language,
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "stream_type": "balanced",
            "mode": s.sarvam_mode,
            "endpointing": "vad",
            "silence_duration_ms": s.endpoint_ms,
            "return_timestamps": "true",
        }
        url = f"wss://api.sarvam.ai/speech-to-text-realtime/ws?{urlencode(params)}"
        headers = {"api-subscription-key": s.key}
    async with connect(url, additional_headers=headers, open_timeout=15, max_size=2**20) as upstream:
        started = json.loads(await asyncio.wait_for(upstream.recv(), timeout=15))
        if (started.get("message_type") if eleven else started.get("event")) != (
            "session_started" if eleven else "session.begin"
        ):
            raise ProviderError(
                "Provider did not start recognition. Check your API key, model access, language, and quota."
            )
        stopping = False
        has_audio = False

        async def write(audio):
            nonlocal has_audio
            if not audio:
                return
            has_audio = True
            encoded = base64.b64encode(audio).decode("ascii")
            event = (
                {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": encoded,
                    "commit": False,
                    "sample_rate": SAMPLE_RATE,
                }
                if eleven
                else {"event": "audio_input", "audio": encoded}
            )
            await upstream.send(json.dumps(event))

        async def finish():
            nonlocal stopping
            stopping = True
            if eleven and not has_audio:
                await upstream.close()
                return
            event = (
                {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": "",
                    "commit": True,
                    "sample_rate": SAMPLE_RATE,
                }
                if eleven
                else {"event": "end"}
            )
            await upstream.send(json.dumps(event))

        async def results():
            async for raw in upstream:
                event = json.loads(raw)
                kind = event.get("message_type") if eleven else event.get("event")
                if event.get("error") or kind == "error":
                    raise ProviderError("Provider rejected the stream. Check key, model, language, and quota.")
                if eleven and kind == "warning":
                    raise ProviderError(
                        "ElevenLabs reported a streaming warning. Check your provider console before retrying."
                    )
                if kind in {"partial_transcript", "transcript.partial", "committed_transcript", "transcript.final"}:
                    final = kind in {"committed_transcript", "transcript.final"}
                    await browser.send_json(
                        {
                            "type": "transcript",
                            "text": event["text"],
                            "final": final,
                            "segments": [event] if final else [],
                        }
                    )
                    if eleven and final and stopping:
                        return
                if not eleven and kind == "session.end":
                    return
            if not (stopping and not has_audio and eleven):
                raise ProviderError(
                    "Provider closed the stream before confirming final transcription. Partial results are preserved."
                )

        await browser.send_json({"type": "ready"})
        await run_live(browser, write, finish, results)


async def azure_live(browser: WebSocket, s: Settings):
    import azure.cognitiveservices.speech as speechsdk

    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue(maxsize=256)
    overflow = asyncio.Event()

    def enqueue(event):
        if events.full():
            overflow.set()
        else:
            events.put_nowait(event)

    def emit(event):
        loop.call_soon_threadsafe(enqueue, event)

    config = speechsdk.SpeechConfig(subscription=s.key, region=s.region)
    config.speech_recognition_language = s.language
    stream = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(samples_per_second=SAMPLE_RATE, bits_per_sample=16, channels=1)
    )
    recognizer = speechsdk.SpeechRecognizer(
        speech_config=config, audio_config=speechsdk.audio.AudioConfig(stream=stream)
    )
    recognizer.recognizing.connect(lambda e: emit({"type": "transcript", "text": e.result.text, "final": False}))
    recognizer.recognized.connect(
        lambda e: (
            emit(
                {
                    "type": "transcript",
                    "text": e.result.text,
                    "final": True,
                    "segments": [{"text": e.result.text, "offset": e.result.offset, "duration": e.result.duration}],
                }
            )
            if e.result.reason == speechsdk.ResultReason.RecognizedSpeech
            else None
        )
    )
    recognizer.session_stopped.connect(lambda _: emit({"type": "ended"}))
    recognizer.canceled.connect(
        lambda e: emit(
            {
                "type": "error" if e.reason == speechsdk.CancellationReason.Error else "ended",
                "message": "Azure ended recognition. Check your key, region, locale, and quota.",
            }
        )
    )

    async def write(data):
        stream.write(data)

    async def finish():
        stream.close()

    async def results():
        while True:
            if overflow.is_set():
                raise ProviderError("Azure results could not be delivered fast enough. Please retry.")
            event = await events.get()
            if event["type"] == "ended":
                return
            if event["type"] == "error":
                raise ProviderError(event["message"])
            await browser.send_json(event)

    try:
        await asyncio.wait_for(asyncio.to_thread(recognizer.start_continuous_recognition_async().get), 20)
        await browser.send_json({"type": "ready"})
        await run_live(browser, write, finish, results)
    finally:
        stream.close()
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.to_thread(recognizer.stop_continuous_recognition_async().get), 10)


STREAM_ERRORS = (ProviderError, TimeoutError, ConnectionClosed, OSError, ValueError, KeyError, RuntimeError)
