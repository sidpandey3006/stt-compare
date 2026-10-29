import asyncio
import base64
import io
import json
import wave
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main
import streaming
from providers import Settings

KEY = "test-key-not-a-real-credential"
ORIGIN = "http://127.0.0.1:5174"


def config(provider="openai", mode="batch", **overrides):
    models = {
        "openai": "gpt-transcribe",
        "azure": "azure-transcribe" if mode == "batch" else "azure-realtime",
        "deepgram": "nova-3",
        "soniox": "stt-async-v5" if mode == "batch" else "stt-rt-v5",
        "elevenlabs": "scribe_v2" if mode == "batch" else "scribe_v2_realtime",
        "sarvam": "saaras:v4",
        "gemini": "gemini-3.5-transcribe",
    }
    return {
        "provider": provider,
        "mode": mode,
        "model": models[provider],
        "api_key": KEY,
        "resource": "test-speech",
        **overrides,
    }


@pytest.fixture
def client():
    with TestClient(main.app) as client:
        yield client


def upload(client, options=None, content=b"RIFF-test-audio", name="clip.wav"):
    return client.post(
        "/api/transcribe",
        files={"audio": (name, content, "audio/wav")},
        data={"settings": json.dumps(options or config())},
    )


def pcm_wav(seconds=1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16000)
        recording.writeframes(b"\0\0" * (16000 * seconds))
    return buffer.getvalue()


@respx.mock
def test_elevenlabs_file_transcription_contract(client):
    route = respx.post("https://api.elevenlabs.io/v1/speech-to-text").respond(
        200,
        json={"text": "Hello", "words": [{"text": "Hello", "start": 0.1, "end": 0.5, "speaker_id": "speaker_0"}]},
    )
    response = upload(client, config("elevenlabs", language="hi-IN", diarize=True))
    assert response.status_code == 200
    assert response.json()["text"] == "Hello"
    assert response.json()["segments"][0]["speaker_id"] == "speaker_0"
    request = route.calls.last.request
    assert request.headers["xi-api-key"] == KEY
    assert b'name="model_id"\r\n\r\nscribe_v2' in request.content
    assert b'name="language_code"\r\n\r\nhi' in request.content
    assert b'name="diarize"\r\n\r\ntrue' in request.content
    assert b'name="timestamps_granularity"\r\n\r\nword' in request.content
    assert b"RIFF-test-audio" in request.content


@pytest.mark.parametrize(("language", "expected"), [("en-US", "en-IN"), ("auto", "unknown"), ("or-IN", "od-IN")])
@respx.mock
def test_sarvam_file_language_output_and_timestamps(client, language, expected):
    timestamps = {"words": ["Hello"], "start_time_seconds": [0.1], "end_time_seconds": [0.5]}
    route = respx.post("https://api.sarvam.ai/speech-to-text").respond(
        200,
        json={"transcript": "Hello", "timestamps": timestamps},
    )
    response = upload(client, config("sarvam", language=language, sarvam_mode="codemix"), content=pcm_wav())
    assert response.status_code == 200
    assert response.json()["text"] == "Hello"
    assert response.json()["segments"] == [timestamps]
    request = route.calls.last.request
    assert request.headers["api-subscription-key"] == KEY
    assert f'name="language_code"\r\n\r\n{expected}'.encode() in request.content
    assert b'name="mode"\r\n\r\ncodemix' in request.content
    assert b'name="with_timestamps"\r\n\r\ntrue' in request.content


@pytest.mark.parametrize("content", [pcm_wav(30), b"RIFF-invalid"], ids=["long-wav", "invalid-wav"])
@respx.mock
def test_sarvam_wav_limits_prevent_provider_submission(client, content):
    route = respx.post("https://api.sarvam.ai/speech-to-text").respond(200, json={})
    response = upload(client, config("sarvam"), content=content)
    assert response.status_code in {415, 422}
    assert "Sarvam" in response.json()["detail"]
    assert not route.called


@respx.mock
def test_gemini_dedicated_model_inline_audio_and_annotations(client):
    word = {"type": "word_info", "text": "Hello", "speaker": "spk_1", "start_offset": "0.100s", "end_offset": "0.400s"}
    route = respx.post("https://generativelanguage.googleapis.com/v1beta/interactions").respond(
        200,
        json={
            "status": "completed",
            "steps": [
                {"type": "thought", "content": [{"type": "text", "text": "Not a transcript"}]},
                {"type": "model_output", "content": [{"type": "text", "text": "Hello", "annotations": [word]}]},
            ],
        },
    )
    response = upload(client, config("gemini", diarize=True), name="clip.m4a")
    assert response.status_code == 200
    assert response.json()["text"] == "Hello"
    assert response.json()["segments"] == [word]
    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url)
    body = json.loads(request.content)
    assert body["model"] == "gemini-3.5-transcribe"
    assert body["store"] is False
    assert body["input"] == [
        {"type": "audio", "data": base64.b64encode(b"RIFF-test-audio").decode(), "mime_type": "audio/m4a"}
    ]
    assert body["generation_config"]["transcription_config"] == {
        "language_codes": ["en-US"],
        "mode": {"type": "verbatim", "timestamp_granularities": ["word"], "diarization_mode": "speaker"},
    }


@pytest.mark.parametrize("status", ["failed", "in_progress", "completed"])
@respx.mock
def test_gemini_missing_or_incomplete_transcript_is_not_success(client, status):
    respx.post("https://generativelanguage.googleapis.com/v1beta/interactions").respond(
        200,
        json={"status": status, "steps": [], "error": {"message": f"{KEY} private audio"}},
    )
    response = upload(client, config("gemini"))
    assert response.status_code == 502
    assert KEY not in response.text and "private audio" not in response.text


def test_gemini_rejects_video_container(client):
    response = upload(client, config("gemini"), name="video.mp4")
    assert response.status_code == 415
    assert "M4A" in response.json()["detail"]


@pytest.mark.parametrize(
    ("provider", "overrides"),
    [
        ("sarvam", {"diarize": True}),
        ("sarvam", {"language": "fr-FR"}),
        ("elevenlabs", {"mode": "live", "model": "scribe_v2_realtime", "diarize": True}),
        ("gemini", {"mode": "live"}),
        ("gemini", {"sarvam_mode": "codemix"}),
    ],
)
def test_new_provider_settings_reject_unsupported_capabilities(client, provider, overrides):
    response = upload(client, config(provider, **overrides))
    assert response.status_code == 422
    assert KEY not in response.text


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("elevenlabs", "https://api.elevenlabs.io/v1/speech-to-text"),
        ("sarvam", "https://api.sarvam.ai/speech-to-text"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/interactions"),
    ],
)
@respx.mock
def test_new_provider_errors_are_secret_safe(client, provider, url):
    respx.post(url).respond(401, json={"message": f"{KEY} private audio"})
    response = upload(client, config(provider), content=pcm_wav())
    assert response.status_code == 502
    assert "HTTP 401" in response.json()["detail"]
    assert KEY not in response.text and "private audio" not in response.text


@respx.mock
def test_openai_upload_uses_current_language_contract(client):
    route = respx.post("https://api.openai.com/v1/audio/transcriptions").respond(200, json={"text": "Hello world"})
    response = upload(client, config(language="hi-IN", prompt="A sample"))
    assert response.status_code == 200
    assert response.json()["text"] == "Hello world"
    assert response.json()["model"] == "gpt-transcribe"
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert b"languages[]" in request.content
    assert b"\r\nhi\r\n" in request.content
    assert b"RIFF-test-audio" in request.content
    assert response.json()["elapsed_ms"] >= 0


@respx.mock
def test_openai_legacy_language_and_unsupported_audio(client):
    route = respx.post("https://api.openai.com/v1/audio/transcriptions").respond(200, json={"text": "Hello"})
    assert upload(client, config(model="whisper-1")).status_code == 200
    assert b'name="language"' in route.calls.last.request.content
    assert upload(client, name="audio.flac").status_code == 415
    assert route.call_count == 1


@respx.mock
def test_azure_fast_transcription_settings(client):
    route = respx.post(
        "https://test-speech.cognitiveservices.azure.com/speechtotext/transcriptions:transcribe"
    ).respond(200, json={"combinedPhrases": [{"text": "Hello"}], "phrases": [{"speaker": 1, "text": "Hello"}]})
    response = upload(client, config("azure", diarize=True))
    assert response.status_code == 200
    assert response.json()["text"] == "Hello"
    assert response.json()["segments"][0]["speaker"] == 1
    assert route.calls.last.request.url.params["api-version"] == "2025-10-15"
    assert b'"diarization": {"enabled": true}' in route.calls.last.request.content


@respx.mock
def test_deepgram_batch_transcript_and_diarization(client):
    route = respx.post("https://api.deepgram.com/v1/listen").respond(
        200,
        json={
            "results": {
                "channels": [{"alternatives": [{"transcript": "Hi there.", "words": [{"word": "Hi", "speaker": 0}]}]}]
            }
        },
    )
    response = upload(client, config("deepgram", language="auto", diarize=True))
    assert response.status_code == 200
    assert response.json()["text"] == "Hi there."
    assert response.json()["segments"][0]["speaker"] == 0
    assert route.calls.last.request.url.params["detect_language"] == "true"
    assert route.calls.last.request.url.params["diarize"] == "true"
    assert route.calls.last.request.content == b"RIFF-test-audio"


@pytest.mark.parametrize("failure", [False, True])
@respx.mock
def test_soniox_poll_and_cleanup_on_success_and_failure(client, failure):
    respx.post("https://api.soniox.com/v1/files").respond(200, json={"id": "file1"})
    respx.post("https://api.soniox.com/v1/transcriptions").respond(200, json={"id": "job1"})
    respx.get("https://api.soniox.com/v1/transcriptions/job1").respond(
        200, json={"status": "error" if failure else "completed"}
    )
    if not failure:
        respx.get("https://api.soniox.com/v1/transcriptions/job1/transcript").respond(
            200, json={"tokens": [{"text": "Hello"}, {"text": " world"}, {"text": "<end>"}]}
        )
    delete_job = respx.delete("https://api.soniox.com/v1/transcriptions/job1").respond(204)
    delete_file = respx.delete("https://api.soniox.com/v1/files/file1").respond(204)
    response = upload(client, config("soniox"))
    assert response.status_code == (502 if failure else 200)
    if not failure:
        assert response.json()["text"] == "Hello world"
    assert delete_job.called and delete_file.called


@respx.mock
def test_soniox_reports_cleanup_failure_without_losing_transcript(client):
    respx.post("https://api.soniox.com/v1/files").respond(200, json={"id": "file1"})
    respx.post("https://api.soniox.com/v1/transcriptions").respond(200, json={"id": "job1"})
    respx.get("https://api.soniox.com/v1/transcriptions/job1").respond(200, json={"status": "completed"})
    respx.get("https://api.soniox.com/v1/transcriptions/job1/transcript").respond(
        200, json={"tokens": [{"text": "Hello"}]}
    )
    respx.delete("https://api.soniox.com/v1/transcriptions/job1").respond(503)
    respx.delete("https://api.soniox.com/v1/files/file1").respond(204)
    response = upload(client, config("soniox"))
    assert response.status_code == 200
    assert response.json()["text"] == "Hello"
    assert "cleanup failed" in response.json()["warnings"][0]


@respx.mock
def test_soniox_cleans_upload_if_job_creation_fails(client):
    respx.post("https://api.soniox.com/v1/files").respond(200, json={"id": "file1"})
    respx.post("https://api.soniox.com/v1/transcriptions").respond(403)
    cleanup = respx.delete("https://api.soniox.com/v1/files/file1").respond(204)
    assert upload(client, config("soniox")).status_code == 502
    assert cleanup.called


@pytest.mark.parametrize(
    "patch",
    [
        {"model": "invalid"},
        {"api_key": ""},
        {"api_key": "  "},
        {"api_key": "abc\r\nbad"},
        {"mode": "live"},
        {"diarize": True},
        {"extra": "not-supported"},
        {"language": "../bad"},
    ],
)
def test_rejects_invalid_settings_without_echoing_key(client, patch):
    response = upload(client, config(**patch))
    assert response.status_code == 422
    assert KEY not in response.text


def test_blocks_arbitrary_azure_host_and_unsupported_live_options():
    from pydantic import ValidationError

    for options in [
        config("azure", resource="evil.test/path"),
        config("azure", resource=""),
        config("azure", mode="live", diarize=True),
        config("azure", mode="live", language="auto"),
        config("openai", mode="live"),
    ]:
        with pytest.raises(ValidationError):
            Settings.model_validate(options)


@pytest.mark.parametrize("content,name,status", [(b"", "clip.wav", 422), (b"x", "bad.txt", 415)])
def test_invalid_audio(client, content, name, status):
    assert upload(client, content=content, name=name).status_code == status


def test_upload_size_limit_before_provider_call(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_AUDIO", 64)
    assert upload(client, content=b"x" * 65).status_code == 413
    assert client.post("/api/transcribe", content=b"x" * 66000).status_code == 413


@respx.mock
def test_provider_errors_do_not_echo_keys_or_audio(client):
    respx.post("https://api.openai.com/v1/audio/transcriptions").respond(401, text=f"{KEY} secret audio")
    response = upload(client)
    assert response.status_code == 502
    assert "HTTP 401" in response.json()["detail"]
    assert KEY not in response.text and "secret audio" not in response.text


@respx.mock
def test_provider_timeout_is_explicit(client):
    respx.post("https://api.openai.com/v1/audio/transcriptions").mock(side_effect=httpx.ReadTimeout("secret"))
    response = upload(client)
    assert response.status_code == 504
    assert "secret" not in response.text


def test_cors_and_websocket_origin(client):
    response = client.options("/api/transcribe", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
    assert response.headers["access-control-allow-origin"] == ORIGIN
    response = client.options(
        "/api/transcribe", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-origin" not in response.headers
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/live", headers={"Origin": "https://evil.example"}):
            pass


def test_websocket_rejects_settings_without_echoing_secrets(client):
    with client.websocket_connect("/api/live", headers={"Origin": ORIGIN}) as socket:
        socket.send_json(config("openai", mode="live"))
        result = socket.receive_json()
        assert result["type"] == "error"
        assert KEY not in json.dumps(result)


class FakeUpstream:
    def __init__(self, provider, failure=False):
        self.provider = provider
        self.failure = failure
        self.queue = asyncio.Queue()
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.queue.get()
        if value is None:
            raise StopAsyncIteration
        return json.dumps(value)

    async def send(self, value):
        self.sent.append(value)
        if isinstance(value, bytes):
            if self.failure:
                await self.queue.put({"type": "Error", "error_code": 401})
            elif self.provider == "deepgram":
                await self.queue.put(
                    {"type": "Results", "is_final": False, "channel": {"alternatives": [{"transcript": "Hello"}]}}
                )
            else:
                await self.queue.put(
                    {"tokens": [{"text": "Hello", "is_final": True}, {"text": " world", "is_final": False}]}
                )
        elif value == "" or json.loads(value).get("type") == "CloseStream":
            if self.provider == "deepgram":
                await self.queue.put(
                    {
                        "type": "Results",
                        "is_final": True,
                        "channel": {"alternatives": [{"transcript": "Hello world", "words": [{"word": "Hello"}]}]},
                    }
                )
            else:
                await self.queue.put({"tokens": [{"text": " world", "is_final": True}], "finished": True})
            await self.queue.put(None)


class JsonUpstream(FakeUpstream):
    async def __aenter__(self):
        self.audio_received = False
        await self.queue.put(
            {"message_type": "session_started"} if self.provider == "elevenlabs" else {"event": "session.begin"}
        )
        return self

    async def recv(self):
        return await self.__anext__()

    async def close(self):
        await self.queue.put(None)

    async def send(self, value):
        self.sent.append(value)
        event = json.loads(value)
        eleven = self.provider == "elevenlabs"
        if event.get("commit") or event.get("event") == "end":
            if self.audio_received:
                await self.queue.put(
                    {"message_type": "committed_transcript", "text": "Hello world"}
                    if eleven
                    else {
                        "event": "transcript.final",
                        "text": "Hello world",
                        "utterance_idx": 0,
                        "start_s": 0,
                        "end_s": 1,
                    }
                )
            if not eleven:
                await self.queue.put({"event": "session.end"})
            await self.queue.put(None)
        elif self.failure:
            await self.queue.put(
                {"message_type": "auth_error", "error": f"{KEY} private audio"}
                if eleven
                else {"event": "error", "message": f"{KEY} private audio", "is_fatal": True}
            )
        else:
            self.audio_received = True
            await self.queue.put(
                {"message_type": "partial_transcript", "text": "Hello"}
                if eleven
                else {"event": "transcript.partial", "text": "Hello", "utterance_idx": 0}
            )


@pytest.mark.parametrize("provider", ["elevenlabs", "sarvam"])
def test_json_live_encodes_pcm_and_drains_final_on_stop(client, monkeypatch, provider):
    upstream = JsonUpstream(provider)
    connector = Mock(return_value=upstream)
    monkeypatch.setattr(streaming, "connect", connector)
    pcm = b"\x01\x00" * 2048
    with client.websocket_connect("/api/live", headers={"Origin": ORIGIN}) as socket:
        socket.send_json(config(provider, mode="live"))
        assert socket.receive_json()["type"] == "ready"
        socket.send_bytes(pcm)
        interim = socket.receive_json()
        assert interim["text"] == "Hello" and interim["final"] is False
        socket.send_text('{"type":"stop"}')
        final = socket.receive_json()
        assert final["text"] == "Hello world" and final["final"] is True
        assert socket.receive_json()["type"] == "done"
    audio, stop = [json.loads(value) for value in upstream.sent]
    assert base64.b64decode(audio["audio_base_64"] if provider == "elevenlabs" else audio["audio"]) == pcm
    url = connector.call_args.args[0]
    params = parse_qs(urlparse(url).query)
    assert KEY not in url
    headers = connector.call_args.kwargs["additional_headers"]
    if provider == "elevenlabs":
        assert headers["xi-api-key"] == KEY
        assert params["commit_strategy"] == ["manual"]
        assert audio["commit"] is False and stop["commit"] is True
        assert stop["audio_base_64"] == ""
    else:
        assert headers["api-subscription-key"] == KEY
        assert params["language_code"] == ["en-IN"]
        assert params["stream_type"] == ["balanced"]
        assert stop == {"event": "end"}


@pytest.mark.parametrize("provider", ["elevenlabs", "sarvam"])
def test_json_live_surfaces_sanitized_errors_and_releases_slots(client, monkeypatch, provider):
    monkeypatch.setattr(streaming, "connect", lambda *_, **__: JsonUpstream(provider, failure=True))
    with client.websocket_connect("/api/live", headers={"Origin": ORIGIN}) as socket:
        socket.send_json(config(provider, mode="live"))
        assert socket.receive_json()["type"] == "ready"
        socket.send_bytes(b"\0\0")
        response = socket.receive_json()
        assert response["type"] == "error"
        assert KEY not in str(response) and "private audio" not in str(response)
    assert main.live_slots._value == 12


@pytest.mark.parametrize("provider", ["elevenlabs", "sarvam"])
def test_json_live_stop_without_audio_does_not_hang(client, monkeypatch, provider):
    upstream = JsonUpstream(provider)
    monkeypatch.setattr(streaming, "connect", lambda *_, **__: upstream)
    with client.websocket_connect("/api/live", headers={"Origin": ORIGIN}) as socket:
        socket.send_json(config(provider, mode="live"))
        assert socket.receive_json()["type"] == "ready"
        socket.send_text('{"type":"stop"}')
        response = socket.receive_json()
        assert response["type"] == "done"


@pytest.mark.parametrize("provider", ["elevenlabs", "sarvam"])
def test_json_live_waits_for_provider_readiness(client, monkeypatch, provider):
    upstream = JsonUpstream(provider)

    async def rejected_start():
        return json.dumps({"error": f"{KEY} private audio"})

    monkeypatch.setattr(upstream, "recv", rejected_start)
    monkeypatch.setattr(streaming, "connect", lambda *_, **__: upstream)
    with client.websocket_connect("/api/live", headers={"Origin": ORIGIN}) as socket:
        socket.send_json(config(provider, mode="live"))
        response = socket.receive_json()
        assert response["type"] == "error"
        assert KEY not in str(response) and "private audio" not in str(response)
    assert main.live_slots._value == 12


@pytest.mark.parametrize("provider", ["elevenlabs", "sarvam"])
def test_json_live_requires_final_confirmation_after_stop(client, monkeypatch, provider):
    class DroppedFinal(JsonUpstream):
        async def send(self, value):
            event = json.loads(value)
            if event.get("commit") or event.get("event") == "end":
                await self.queue.put(None)
            else:
                await super().send(value)

    monkeypatch.setattr(streaming, "connect", lambda *_, **__: DroppedFinal(provider))
    with client.websocket_connect("/api/live", headers={"Origin": ORIGIN}) as socket:
        socket.send_json(config(provider, mode="live"))
        assert socket.receive_json()["type"] == "ready"
        socket.send_bytes(b"\0\0")
        assert socket.receive_json()["text"] == "Hello"
        socket.send_text('{"type":"stop"}')
        response = socket.receive_json()
        assert response["type"] == "error"
        assert "confirming final transcription" in response["message"]
    assert main.live_slots._value == 12


@pytest.mark.parametrize("provider", ["deepgram", "soniox"])
def test_live_pcm_interim_final_and_stop(client, monkeypatch, provider):
    upstream = FakeUpstream(provider)
    connector = Mock(return_value=upstream)
    monkeypatch.setattr(streaming, "connect", connector)
    with client.websocket_connect("/api/live", headers={"Origin": ORIGIN}) as socket:
        socket.send_json(config(provider, mode="live"))
        assert socket.receive_json()["type"] == "ready"
        socket.send_bytes(b"\x00\x00" * 2048)
        first = socket.receive_json()
        assert first["text"] == "Hello"
        if provider == "soniox":
            assert socket.receive_json()["text"] == " world"
        socket.send_text('{"type":"stop"}')
        final = socket.receive_json()
        assert final["final"] is True
        if provider == "soniox":
            assert final["text"] == " world"
            socket.receive_json()  # empty interim replacement
        assert socket.receive_json()["type"] == "done"
    assert b"\x00\x00" * 2048 in upstream.sent
    headers = connector.call_args.kwargs["additional_headers"]
    assert KEY in headers["Authorization"]
    assert KEY not in connector.call_args.args[0]


@pytest.mark.parametrize("invalid", [b"\x00", b"\x00" * 32770], ids=["odd-bytes", "oversized-frame"])
def test_live_rejects_bad_pcm_frames(client, monkeypatch, invalid):
    monkeypatch.setattr(streaming, "connect", lambda *_, **__: FakeUpstream("deepgram"))
    with client.websocket_connect("/api/live", headers={"Origin": ORIGIN}) as socket:
        socket.send_json(config("deepgram", mode="live"))
        assert socket.receive_json()["type"] == "ready"
        socket.send_bytes(invalid)
        assert socket.receive_json()["type"] == "error"


def test_live_provider_error_and_disconnect_release_slots(client, monkeypatch):
    monkeypatch.setattr(streaming, "connect", lambda *_, **__: FakeUpstream("deepgram", failure=True))
    with client.websocket_connect("/api/live", headers={"Origin": ORIGIN}) as socket:
        socket.send_json(config("deepgram", mode="live"))
        assert socket.receive_json()["type"] == "ready"
        socket.send_bytes(b"\0\0")
        response = socket.receive_json()
        assert response["type"] == "error"
        assert KEY not in response["message"]
    assert main.live_slots._value == 12


def test_azure_live_push_stream_and_final_drain(client, monkeypatch):
    import azure.cognitiveservices.speech as sdk

    class Signal:
        def connect(self, callback):
            self.callback = callback

        def emit(self, event):
            self.callback(event)

    recognizer = SimpleNamespace(
        recognizing=Signal(),
        recognized=Signal(),
        session_stopped=Signal(),
        canceled=Signal(),
        start_continuous_recognition_async=Mock(return_value=SimpleNamespace(get=lambda: None)),
        stop_continuous_recognition_async=Mock(return_value=SimpleNamespace(get=lambda: None)),
    )
    closed = False

    def write(_data):
        recognizer.recognizing.emit(SimpleNamespace(result=SimpleNamespace(text="Hello")))

    def close():
        nonlocal closed
        if closed:
            return
        closed = True
        recognizer.recognized.emit(
            SimpleNamespace(
                result=SimpleNamespace(
                    text="Hello world",
                    reason=sdk.ResultReason.RecognizedSpeech,
                    offset=0,
                    duration=100,
                )
            )
        )
        recognizer.session_stopped.emit(None)

    monkeypatch.setattr(sdk, "SpeechConfig", Mock())
    monkeypatch.setattr(sdk, "SpeechRecognizer", Mock(return_value=recognizer))
    monkeypatch.setattr(sdk.audio, "PushAudioInputStream", Mock(return_value=SimpleNamespace(write=write, close=close)))
    monkeypatch.setattr(sdk.audio, "AudioConfig", Mock())
    monkeypatch.setattr(sdk.audio, "AudioStreamFormat", Mock())
    with client.websocket_connect("/api/live", headers={"Origin": ORIGIN}) as socket:
        socket.send_json(config("azure", mode="live"))
        assert socket.receive_json()["type"] == "ready"
        socket.send_bytes(b"\x00\x00" * 100)
        assert socket.receive_json()["final"] is False
        socket.send_text('{"type":"stop"}')
        assert socket.receive_json()["text"] == "Hello world"
        assert socket.receive_json()["type"] == "done"
    recognizer.stop_continuous_recognition_async.assert_called_once()
