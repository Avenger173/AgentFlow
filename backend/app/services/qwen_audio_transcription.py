"""DashScope Qwen Audio 的受控短媒体转写适配器。

该模块只负责将已由上层选择的音频字节转为带时间戳的转写结果。它不读取本地路径、不创建
媒体项目、不上传对象存储，也不把 Provider 的流式原文直接写入任务历史。长音频和长视频需
要受控切分与交付协议，属于后续 MM-4 步骤，不能以此适配器的 Base64 路径冒充已支持。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Literal

import httpx

from app.core.config import settings
from app.services.model_gateway import (
    AudioModelRuntime,
    ModelGatewayError,
    resolve_audio_model_runtime_for_route,
)


_MULTIMODAL_GENERATION_PATH = "/services/aigc/multimodal-generation/generation"
_MAX_INPUT_AUDIO_BYTES = 7 * 1024 * 1024
_MAX_LANGUAGE_HINTS = 4
_AUDIO_MIME_BY_FORMAT = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
}


@dataclass(frozen=True)
class QwenAudioTranscriptionInput:
    """受控内存音频。调用方不能以路径或远程 URL 替代内容。"""

    audio_bytes: bytes
    audio_format: Literal["wav", "mp3"]


@dataclass(frozen=True)
class QwenAudioWord:
    text: str
    begin_ms: int
    end_ms: int
    punctuation: str = ""


@dataclass(frozen=True)
class QwenAudioTranscriptSegment:
    sentence_id: int
    text: str
    begin_ms: int
    end_ms: int
    speaker_id: int | None = None
    words: tuple[QwenAudioWord, ...] = ()


@dataclass(frozen=True)
class QwenAudioTranscriptionResult:
    provider: str
    model: str
    request_id: str
    text: str
    segments: tuple[QwenAudioTranscriptSegment, ...]
    duration_seconds: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    usage_reported: bool = False


class QwenAudioTranscriptionProviderError(ModelGatewayError):
    """Provider 已明确拒绝本次请求，调用方可提示用户修复输入或稍后重试。"""

    def __init__(self, *, status_code: int, error_code: str = "", message: str = "") -> None:
        self.status_code = status_code
        self.error_code = error_code[:80]
        detail = " · ".join(part for part in (self.error_code, message[:180]) if part)
        suffix = f"（{detail}）" if detail else ""
        super().__init__(f"Qwen Audio 转写接口返回 HTTP {status_code}{suffix}。")


class QwenAudioTranscriptionOutcomeUnknownError(ModelGatewayError):
    """请求可能已到达 Provider，不能在未知结果下自动重复计费。"""

    def __init__(self, *, reason: Literal["request_timeout", "request_connection", "provider_server_error"], message: str = "") -> None:
        self.reason = reason
        self.outcome = "unknown"
        self.safe_to_retry_automatically = False
        suffix = f"（{message[:180]}）" if message else ""
        super().__init__(f"Qwen Audio 转写提交结果未知{suffix}；不会自动重试。")


async def transcribe_qwen_audio(
    *,
    audio: QwenAudioTranscriptionInput,
    language_hints: tuple[str, ...] = (),
    speaker_diarization: bool = False,
    runtime: AudioModelRuntime | None = None,
    client: httpx.AsyncClient | None = None,
) -> QwenAudioTranscriptionResult:
    """提交一次短媒体 SSE 转写，并只接收稳定句级时间戳。

    Qwen Audio 3.1 的 Base64 输入适合显式提交的短媒体。SSE 使一分钟以上内容逐句返回；
    非终态句子和未稳定词时间戳不会进入结果，避免后续字幕或 EDL 绑定到会漂移的时间点。
    """

    active_runtime = runtime or resolve_audio_model_runtime_for_route("media_transcription", validate=True).runtime
    if not isinstance(active_runtime, AudioModelRuntime):
        raise ModelGatewayError("语音转写路由未解析到音频模型运行时。")
    if active_runtime.provider != "qwen_audio" or active_runtime.transport != "dashscope_multimodal":
        raise ModelGatewayError("当前语音转写路由不是已接入的 Qwen Audio Provider。")

    clean_hints = _validate_input(audio, language_hints)
    parameters: dict[str, object] = {"format": audio.audio_format}
    if clean_hints:
        parameters["language_hints"] = list(clean_hints)
    if speaker_diarization:
        parameters["speaker_diarization_enabled"] = True
    payload: dict[str, object] = {
        "model": active_runtime.model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": _as_data_url(audio)},
                        }
                    ],
                }
            ]
        },
        "parameters": parameters,
    }
    headers = {
        "Authorization": f"Bearer {active_runtime.api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream,application/json",
        "X-DashScope-SSE": "enable",
    }
    url = f"{active_runtime.base_url.rstrip('/')}{_MULTIMODAL_GENERATION_PATH}"
    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(max(30.0, min(float(settings.llm_timeout_seconds), 180.0)), connect=10.0)
    )
    try:
        async with active_client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code >= 400:
                error_body = await response.aread()
                _raise_provider_error(response.status_code, error_body)
            return await _parse_transcription_response(response, runtime=active_runtime)
    except httpx.TimeoutException as exc:
        raise QwenAudioTranscriptionOutcomeUnknownError(
            reason="request_timeout",
            message="等待 Provider 流式转写结果超时",
        ) from exc
    except httpx.RequestError as exc:
        raise QwenAudioTranscriptionOutcomeUnknownError(
            reason="request_connection",
            message="提交或接收转写结果时连接中断",
        ) from exc
    finally:
        if owns_client:
            await active_client.aclose()


def _validate_input(
    audio: QwenAudioTranscriptionInput,
    language_hints: tuple[str, ...],
) -> tuple[str, ...]:
    if audio.audio_format not in _AUDIO_MIME_BY_FORMAT:
        raise ModelGatewayError("当前语音转写仅接收 WAV 或 MP3；视频需先经受控音轨提取。")
    if not audio.audio_bytes:
        raise ModelGatewayError("待转写音频不能为空。")
    if len(audio.audio_bytes) > _MAX_INPUT_AUDIO_BYTES:
        raise ModelGatewayError("首期本地语音转写仅接收不超过 7 MB 的短音频；请先切分或压缩后再提交。")
    hints = tuple(dict.fromkeys(value.strip().lower() for value in language_hints if value.strip()))
    if len(hints) > _MAX_LANGUAGE_HINTS:
        raise ModelGatewayError("语音转写最多接收 4 个语言提示。")
    if any(len(item) > 12 for item in hints):
        raise ModelGatewayError("语音转写语言提示格式不正确。")
    return hints


def _as_data_url(audio: QwenAudioTranscriptionInput) -> str:
    encoded = base64.b64encode(audio.audio_bytes).decode("ascii")
    return f"data:{_AUDIO_MIME_BY_FORMAT[audio.audio_format]};base64,{encoded}"


async def _parse_transcription_response(
    response: httpx.Response,
    *,
    runtime: AudioModelRuntime,
) -> QwenAudioTranscriptionResult:
    """兼容短音频 JSON 终态和长音频 SSE，统一只落稳定时间戳。"""

    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" not in content_type:
        try:
            payload = json.loads((await response.aread()).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelGatewayError("Qwen Audio 未返回可解析的 JSON 转写结果。") from exc
        if not isinstance(payload, dict):
            raise ModelGatewayError("Qwen Audio 转写结果不是 JSON object。")
        return _build_transcription_result(
            runtime=runtime,
            initial_request_id=response.headers.get("x-request-id", "").strip(),
            events=(_read_event(payload),),
        )

    segments: dict[int, QwenAudioTranscriptSegment] = {}
    final_text = ""
    request_id = response.headers.get("x-request-id", "").strip()
    usage: dict[str, object] = {}
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
            continue
        if line.strip():
            continue
        if not data_lines:
            continue
        payload = _decode_event("\n".join(data_lines))
        data_lines.clear()
        event_text, event_request_id, event_usage, event_segments = _read_event(payload)
        if event_text:
            final_text = event_text
        if event_request_id:
            request_id = event_request_id
        if event_usage:
            usage = event_usage
        for segment in event_segments:
            segments[segment.sentence_id] = segment
    if data_lines:
        payload = _decode_event("\n".join(data_lines))
        event_text, event_request_id, event_usage, event_segments = _read_event(payload)
        final_text = event_text or final_text
        request_id = event_request_id or request_id
        usage = event_usage or usage
        for segment in event_segments:
            segments[segment.sentence_id] = segment

    return _build_transcription_result(
        runtime=runtime,
        initial_request_id=request_id,
        events=((final_text, request_id, usage, tuple(segments[key] for key in sorted(segments))),),
    )


def _build_transcription_result(
    *,
    runtime: AudioModelRuntime,
    initial_request_id: str,
    events: tuple[tuple[str, str, dict[str, object], tuple[QwenAudioTranscriptSegment, ...]], ...],
) -> QwenAudioTranscriptionResult:
    segments: dict[int, QwenAudioTranscriptSegment] = {}
    final_text = ""
    request_id = initial_request_id
    usage: dict[str, object] = {}
    for event_text, event_request_id, event_usage, event_segments in events:
        if event_text:
            final_text = event_text
        if event_request_id:
            request_id = event_request_id
        if event_usage:
            usage = event_usage
        for segment in event_segments:
            segments[segment.sentence_id] = segment

    ordered_segments = tuple(segments[key] for key in sorted(segments))
    if not final_text:
        final_text = "".join(segment.text for segment in ordered_segments)
    if not final_text or not ordered_segments:
        raise ModelGatewayError("Qwen Audio 未返回可用于字幕的稳定句级时间戳。")
    return QwenAudioTranscriptionResult(
        provider=runtime.provider,
        model=runtime.model,
        request_id=request_id[:160],
        text=final_text,
        segments=ordered_segments,
        duration_seconds=_optional_int(usage.get("duration")),
        input_tokens=_optional_int(usage.get("input_tokens")),
        output_tokens=_optional_int(usage.get("output_tokens")),
        total_tokens=_optional_int(usage.get("total_tokens")),
        usage_reported=bool(usage),
    )


def _decode_event(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelGatewayError("Qwen Audio 流式响应包含无效 JSON。") from exc
    if not isinstance(value, dict):
        raise ModelGatewayError("Qwen Audio 流式响应事件不是 JSON object。")
    return value


def _read_event(
    payload: dict[str, object],
) -> tuple[str, str, dict[str, object], tuple[QwenAudioTranscriptSegment, ...]]:
    output = payload.get("output")
    if not isinstance(output, dict):
        return "", "", {}, ()
    text = str(output.get("text") or "").strip()
    request_id = str(payload.get("request_id") or payload.get("requestId") or "").strip()
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    raw_sentences = output.get("sentences")
    if not isinstance(raw_sentences, list):
        raw_sentence = output.get("sentence")
        raw_sentences = [raw_sentence] if isinstance(raw_sentence, dict) else []
    segments: list[QwenAudioTranscriptSegment] = []
    for raw_sentence in raw_sentences:
        if not isinstance(raw_sentence, dict) or not raw_sentence.get("sentence_end", False):
            continue
        segment = _parse_segment(raw_sentence)
        if segment is not None:
            segments.append(segment)
    return text, request_id, usage, tuple(segments)


def _parse_segment(raw: dict[str, object]) -> QwenAudioTranscriptSegment | None:
    sentence_id = _optional_int(raw.get("sentence_id"))
    begin_ms = _optional_int(raw.get("begin_time"))
    end_ms = _optional_int(raw.get("end_time"))
    text = str(raw.get("text") or "").strip()
    if sentence_id is None or begin_ms is None or end_ms is None or end_ms < begin_ms or not text:
        return None
    words: list[QwenAudioWord] = []
    raw_words = raw.get("words")
    if isinstance(raw_words, list):
        for item in raw_words:
            if not isinstance(item, dict) or not bool(item.get("fixed", True)):
                continue
            word_begin = _optional_int(item.get("begin_time"))
            word_end = _optional_int(item.get("end_time"))
            word_text = str(item.get("text") or "")
            if word_begin is None or word_end is None or word_end < word_begin or not word_text:
                continue
            words.append(
                QwenAudioWord(
                    text=word_text,
                    begin_ms=word_begin,
                    end_ms=word_end,
                    punctuation=str(item.get("punctuation") or "")[:8],
                )
            )
    speaker_id = _optional_int(raw.get("speaker_id"))
    return QwenAudioTranscriptSegment(
        sentence_id=sentence_id,
        text=text,
        begin_ms=begin_ms,
        end_ms=end_ms,
        speaker_id=speaker_id,
        words=tuple(words),
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _raise_provider_error(status_code: int, content: bytes) -> None:
    code = ""
    message = ""
    try:
        body = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = None
    if isinstance(body, dict):
        nested = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = str(body.get("code") or nested.get("code") or "").strip()[:80]
        message = str(body.get("message") or nested.get("message") or "").strip()[:180]
    if status_code >= 500 or status_code == 408:
        raise QwenAudioTranscriptionOutcomeUnknownError(
            reason="provider_server_error",
            message=" ".join(part for part in (code, message) if part),
        )
    raise QwenAudioTranscriptionProviderError(status_code=status_code, error_code=code, message=message)
