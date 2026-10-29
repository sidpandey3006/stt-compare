import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

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
