"""验证 Qwen Audio 短媒体转写适配器的请求与恢复边界。

测试只使用 HTTP MockTransport 和程序生成的少量字节，不读取本机音频、不会请求 Provider，
也不会写入模型配置或任务历史。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.model_gateway import AudioModelRuntime, ModelGatewayError
from app.services.qwen_audio_transcription import (
    QwenAudioTranscriptionInput,
    QwenAudioTranscriptionOutcomeUnknownError,
    QwenAudioTranscriptionProviderError,
    transcribe_qwen_audio,
)


RUNTIME = AudioModelRuntime(
    provider="qwen_audio",
    label="Qwen Audio / DashScope",
    transport="dashscope_multimodal",
    base_url="https://dashscope.example.test/api/v1",
    model="qwen-audio-3.1-asr-flash",
    api_key="fixture-qwen-key",
)
AUDIO = QwenAudioTranscriptionInput(audio_bytes=b"RIFFfixture-wav-bytes", audio_format="wav")


def _sse(payload: dict[str, object]) -> bytes:
    return f"event: result\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def _verify_success() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        events = b"".join(
            (
                _sse(
                    {
                        "output": {
                            "sentence": {
                                "sentence_id": 1,
                                "sentence_end": False,
                                "begin_time": 0,
                                "text": "不稳定片段",
                            }
                        }
                    }
                ),
                _sse(
                    {
                        "request_id": "req-audio-fixture",
                        "output": {
                            "text": "你好 AgentFlow",
                            "sentence": {
                                "sentence_id": 1,
                                "sentence_end": True,
                                "begin_time": 120,
                                "end_time": 1520,
                                "text": "你好",
                                "words": [
                                    {"text": "你", "begin_time": 120, "end_time": 560, "fixed": True},
                                    {"text": "好", "begin_time": 560, "end_time": 880, "fixed": True},
                                ],
                            },
                        },
                        "usage": {"duration": 2, "input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                    }
                ),
                _sse(
                    {
                        "output": {
                            "sentence": {
                                "sentence_id": 2,
                                "sentence_end": True,
                                "begin_time": 1530,
                                "end_time": 2280,
                                "text": "AgentFlow",
                            }
                        }
                    }
                ),
            )
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await transcribe_qwen_audio(
            audio=AUDIO,
            language_hints=("zh", "en", "zh"),
            runtime=RUNTIME,
            client=client,
        )

    assert captured["url"] == "https://dashscope.example.test/api/v1/services/aigc/multimodal-generation/generation"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["x-dashscope-sse"] == "enable"
    assert "fixture-qwen-key" not in json.dumps(captured["payload"], ensure_ascii=False)
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "qwen-audio-3.1-asr-flash"
    content = payload["input"]["messages"][0]["content"][0]
    assert content["type"] == "input_audio"
    assert content["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert "path" not in json.dumps(payload, ensure_ascii=False).lower()
    assert payload["parameters"] == {"format": "wav", "language_hints": ["zh", "en"]}
    assert result.request_id == "req-audio-fixture"
    assert result.text == "你好 AgentFlow"
    assert [(item.sentence_id, item.begin_ms, item.end_ms) for item in result.segments] == [(1, 120, 1520), (2, 1530, 2280)]
    assert [word.text for word in result.segments[0].words] == ["你", "好"]
    assert result.duration_seconds == 2
    assert (result.input_tokens, result.output_tokens, result.total_tokens) == (7, 3, 10)


async def _verify_short_audio_json_result() -> None:
    """官方对不足一分钟的音频只返回 JSON 终态，不能误判为 SSE 协议失败。"""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "x-request-id": "header-request-id"},
            json={
                "request_id": "json-audio-fixture",
                "output": {
                    "text": "短音频最终结果",
                    "sentence": {
                        "sentence_id": 1,
                        "sentence_end": True,
                        "begin_time": 0,
                        "end_time": 860,
                        "text": "短音频最终结果",
                        "words": [
                            {"text": "短音频", "begin_time": 0, "end_time": 400, "fixed": True},
                            {"text": "最终结果", "begin_time": 400, "end_time": 860, "fixed": True},
                        ],
                    },
                },
                "usage": {"duration": 1, "input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await transcribe_qwen_audio(audio=AUDIO, runtime=RUNTIME, client=client)

    assert result.request_id == "json-audio-fixture"
    assert result.text == "短音频最终结果"
    assert [(item.begin_ms, item.end_ms) for item in result.segments] == [(0, 860)]
    assert result.duration_seconds == 1
    assert result.total_tokens == 7


async def _verify_failures() -> None:
    async def rejected(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": "InvalidParameter", "message": "audio format rejected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(rejected)) as client:
        try:
            await transcribe_qwen_audio(audio=AUDIO, runtime=RUNTIME, client=client)
        except QwenAudioTranscriptionProviderError as exc:
            assert exc.status_code == 400
            assert exc.error_code == "InvalidParameter"
        else:  # pragma: no cover - 守住显式拒绝不能被标为成功。
            raise AssertionError("expected explicit provider rejection")

    async def server_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": "ServiceUnavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(server_error)) as client:
        try:
            await transcribe_qwen_audio(audio=AUDIO, runtime=RUNTIME, client=client)
        except QwenAudioTranscriptionOutcomeUnknownError as exc:
            assert exc.reason == "provider_server_error"
            assert exc.safe_to_retry_automatically is False
        else:  # pragma: no cover
            raise AssertionError("expected unknown provider outcome")

    called = False

    async def must_not_call(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    too_large = QwenAudioTranscriptionInput(audio_bytes=b"x" * (7 * 1024 * 1024 + 1), audio_format="wav")
    async with httpx.AsyncClient(transport=httpx.MockTransport(must_not_call)) as client:
        try:
            await transcribe_qwen_audio(audio=too_large, runtime=RUNTIME, client=client)
        except ModelGatewayError as exc:
            assert "7 MB" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected preflight size rejection")
    assert called is False


def main() -> None:
    asyncio.run(_verify_success())
    asyncio.run(_verify_short_audio_json_result())
    asyncio.run(_verify_failures())
    print("Qwen Audio transcription adapter verification passed.")


if __name__ == "__main__":
    main()
