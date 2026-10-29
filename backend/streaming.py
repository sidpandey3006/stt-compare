import asyncio
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
        sender = asyncio.create_task(receive_audio(browser, upstream.send, finish))
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

    sender = None
    reader = None
    try:
        await asyncio.wait_for(asyncio.to_thread(recognizer.start_continuous_recognition_async().get), 20)
        await browser.send_json({"type": "ready"})
        sender = asyncio.create_task(receive_audio(browser, write, finish))
        reader = asyncio.create_task(results())
        done, _ = await asyncio.wait({sender, reader}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if sender in done:
            await asyncio.wait_for(reader, 20)
        elif not sender.done():
            raise ProviderError("Azure ended recognition before recording stopped.")
    finally:
        stream.close()
        for task in (sender, reader):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (sender, reader) if t), return_exceptions=True)
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.to_thread(recognizer.stop_continuous_recognition_async().get), 10)


STREAM_ERRORS = (ProviderError, TimeoutError, ConnectionClosed, OSError, ValueError, KeyError, RuntimeError)
