import asyncio
import json
import logging
import re
import sys
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

MAX_AUDIO = 24 * 1024 * 1024
MODELS = {
    "azure": {"batch": ["azure-transcribe"], "live": ["azure-realtime"]},
    "openai": {"batch": ["gpt-transcribe", "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"], "live": []},
    "deepgram": {"batch": ["nova-3", "nova-2"], "live": ["nova-3", "nova-2"]},
    "soniox": {"batch": ["stt-async-v5", "stt-async-v4"], "live": ["stt-rt-v5", "stt-rt-v4"]},
}
logger = logging.getLogger("stt.providers")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: Literal["azure", "openai", "deepgram", "soniox"]
    api_key: SecretStr = Field(min_length=1, max_length=4096)
    mode: Literal["batch", "live"] = "batch"
    model: str = Field(max_length=80)
    language: str = Field(default="en-US", pattern=r"^(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8})?)$")
    region: str = Field(default="eastus", pattern=r"^[a-z0-9]{2,40}$")
    resource: str = Field(default="", pattern=r"^([a-zA-Z0-9][a-zA-Z0-9-]{0,62})?$")
    diarize: bool = False
    punctuate: bool = True
    endpoint_ms: int = Field(default=1000, ge=500, le=3000)
    prompt: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def supported(self):
        if self.model not in MODELS[self.provider][self.mode]:
            raise ValueError("This model does not support the selected mode.")
        if not self.api_key.get_secret_value().strip():
            raise ValueError("An API key is required.")
        if re.search(r"[\r\n\x00]", self.api_key.get_secret_value()):
            raise ValueError("Invalid API key.")
        if self.provider == "azure":
            if self.mode == "batch" and not self.resource:
                raise ValueError("Azure file transcription requires your resource name.")
            if self.mode == "live" and self.language == "auto":
                raise ValueError("Choose an explicit locale for Azure live transcription.")
            if self.mode == "live" and self.diarize:
                raise ValueError("Speaker labels are not available in this Azure live adapter.")
        if self.provider == "openai" and self.diarize:
            raise ValueError("Speaker labels are not supported by these OpenAI models.")
        if self.prompt and self.provider not in {"openai", "soniox"}:
            raise ValueError("Context is supported only by OpenAI and Soniox adapters.")
        return self

    @property
    def key(self) -> str:
        return self.api_key.get_secret_value()

    @property
    def lang(self) -> str:
        return self.language.split("-")[0]


class ProviderError(Exception):
    pass


def checked(response: httpx.Response) -> dict:
    if not response.is_success:
        code = response.status_code
        hint = {
            400: "Check the audio format, language, and model settings.",
            401: "Check your API key.",
            403: "Check account permissions, region, and model access.",
            404: "Check the model and Azure resource name.",
            413: "The provider rejected the audio size.",
            429: "Quota or rate limit reached. Check your provider account.",
        }.get(code, "The provider could not process this request.")
        raise ProviderError(f"Provider returned HTTP {code}. {hint}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError("Provider returned an invalid JSON response.") from exc


def soniox_text(tokens: list[dict]) -> str:
    return "".join(t.get("text", "") for t in tokens if t.get("text") not in {"<end>", "<fin>"})


def deepgram_options(s: Settings) -> dict:
    result = {
        "model": s.model,
        "punctuate": str(s.punctuate).lower(),
        "smart_format": str(s.punctuate).lower(),
        "diarize": str(s.diarize).lower(),
    }
    if s.language == "auto":
        if s.mode == "batch":
            result["detect_language"] = "true"
        else:
            result["language"] = "multi"
    else:
        result["language"] = s.language
    return result


async def transcribe(s: Settings, audio: bytes, filename: str, content_type: str) -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(150, connect=15), follow_redirects=False) as client:
        if s.provider == "openai":
            data = {"model": s.model, "response_format": "json"}
            if s.language != "auto":
                data["languages[]" if s.model == "gpt-transcribe" else "language"] = s.lang
            if s.prompt:
                data["prompt"] = s.prompt
            result = checked(
                await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {s.key}"},
                    files={"file": (filename, audio, content_type)},
                    data=data,
                )
            )
            return {"text": result["text"], "segments": result.get("segments", []), "warnings": []}
        if s.provider == "azure":
            definition = {"diarization": {"enabled": s.diarize}}
            if s.language != "auto":
                definition["locales"] = [s.language]
            result = checked(
                await client.post(
                    f"https://{s.resource}.cognitiveservices.azure.com/speechtotext/transcriptions:transcribe",
                    params={"api-version": "2025-10-15"},
                    headers={"Ocp-Apim-Subscription-Key": s.key},
                    files={"audio": (filename, audio, content_type)},
                    data={"definition": json.dumps(definition)},
                )
            )
            return {
                "text": "\n".join(p["text"] for p in result.get("combinedPhrases", [])),
                "segments": result.get("phrases", []),
                "warnings": [],
            }
        if s.provider == "deepgram":
            result = checked(
                await client.post(
                    "https://api.deepgram.com/v1/listen",
                    params=deepgram_options(s),
                    headers={"Authorization": f"Token {s.key}", "Content-Type": content_type},
                    content=audio,
                )
            )
            alternative = result["results"]["channels"][0]["alternatives"][0]
            return {"text": alternative["transcript"], "segments": alternative.get("words", []), "warnings": []}
        return await soniox_batch(client, s, audio, filename, content_type)


async def soniox_batch(client: httpx.AsyncClient, s: Settings, audio: bytes, filename: str, content_type: str) -> dict:
    headers = {"Authorization": f"Bearer {s.key}"}
    base = "https://api.soniox.com/v1"
    file_id = None
    job_id = None
    warnings = []
    try:
        uploaded = checked(
            await client.post(f"{base}/files", headers=headers, files={"file": (filename, audio, content_type)})
        )
        file_id = quote(uploaded["id"], safe="")
        body = {"model": s.model, "file_id": uploaded["id"], "enable_speaker_diarization": s.diarize}
        if s.language != "auto":
            body["language_hints"] = [s.lang]
        if s.prompt:
            body["context"] = {"text": s.prompt}
        job = checked(await client.post(f"{base}/transcriptions", headers=headers, json=body))
        job_id = quote(job["id"], safe="")
        async with asyncio.timeout(240):
            while True:
                status = checked(await client.get(f"{base}/transcriptions/{job_id}", headers=headers))
                if status["status"] == "completed":
                    break
                if status["status"] == "error":
                    raise ProviderError("Soniox could not transcribe this audio. Check its format and model access.")
                await asyncio.sleep(1)
            transcript = checked(await client.get(f"{base}/transcriptions/{job_id}/transcript", headers=headers))
        tokens = transcript.get("tokens", [])
        result = {"text": soniox_text(tokens).strip(), "segments": tokens, "warnings": warnings}
    finally:
        # Delete only resources created by this request, including on timeout.
        for kind, identifier in (("transcriptions", job_id), ("files", file_id)):
            if identifier:
                try:
                    response = await client.delete(f"{base}/{kind}/{identifier}", headers=headers, timeout=10)
                    if not response.is_success and response.status_code != 404:
                        warnings.append(
                            f"Soniox {kind} cleanup failed (HTTP {response.status_code}). Delete it in your Soniox console."
                        )
                        logger.warning("Soniox %s cleanup failed: HTTP %s", kind, response.status_code)
                except httpx.HTTPError:
                    warnings.append(f"Soniox {kind} cleanup could not be confirmed. Check your Soniox console.")
                    logger.warning("Soniox %s cleanup connection failed", kind)
        if warnings and sys.exc_info()[0] is not None:
            raise ProviderError(
                "Transcription failed and Soniox cleanup could not be confirmed. Check your Soniox console."
            ) from None
    return result
