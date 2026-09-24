"""执行一次受控的 Qwen Audio 短音频真实转写探针。

默认不联网。传入 ``--live`` 后，脚本只使用 Windows SAPI 合成一句公开英文测试语音，提交
一次 ``media_transcription`` 路由，并记录不含 Key、音频正文或 Provider 原始响应的摘要。
它只验证端点、短音频 JSON 终态、稳定时间戳和 usage；不构成视频、字幕或内容质量准入。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import wave
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings
from app.services.qwen_audio_transcription import (
    QwenAudioTranscriptionInput,
    transcribe_qwen_audio,
)


_FIXTURE_TEXT = "AgentFlow validates audio transcription."


def _synthesize_fixture(path: Path) -> dict[str, object]:
    """由本机 SAPI 生成固定英文 WAV，不读取用户录音或其它媒体文件。"""

    escaped_path = str(path.resolve()).replace("'", "''")
    escaped_text = _FIXTURE_TEXT.replace("'", "''")
    command = (
        "Add-Type -AssemblyName System.Speech; "
        "$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new(); "
        "$culture = [System.Globalization.CultureInfo]::GetCultureInfo('en-US'); "
        "$synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet, "
        "[System.Speech.Synthesis.VoiceAge]::NotSet, 0, $culture); "
        f"$synth.SetOutputToWaveFile('{escaped_path}'); "
        f"$synth.Speak('{escaped_text}'); "
        "$synth.Dispose();"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0 or not path.is_file():
        message = completed.stderr.strip().replace("\r", " ").replace("\n", " ")[:240]
        raise RuntimeError(f"Windows SAPI 未能生成受控音频夹具：{message or 'unknown error'}")
    with wave.open(str(path), "rb") as source:
        duration_seconds = source.getnframes() / float(source.getframerate())
        return {
            "format": "wav",
            "channels": source.getnchannels(),
            "sample_rate": source.getframerate(),
            "duration_seconds": round(duration_seconds, 3),
        }


async def _run_live_probe(output_dir: Path) -> dict[str, object]:
    fixture_path = output_dir / "generated_en_us_fixture.wav"
    fixture_metadata = _synthesize_fixture(fixture_path)
    audio_bytes = fixture_path.read_bytes()
    started = perf_counter()
    result = await transcribe_qwen_audio(
        audio=QwenAudioTranscriptionInput(audio_bytes=audio_bytes, audio_format="wav"),
        language_hints=("en",),
    )
    return {
        "probe": "qwen_audio_short_transcription_v1",
        "route": "media_transcription",
        "live_call_count": 1,
        "fixture": "windows_sapi_generated_en_us_wav",
        "fixture_text": _FIXTURE_TEXT,
        "fixture_sha256": hashlib.sha256(audio_bytes).hexdigest(),
        "fixture_bytes": len(audio_bytes),
        "fixture_metadata": fixture_metadata,
        "provider": result.provider,
        "model": result.model,
        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        "recognized_text": result.text,
        "stable_segment_count": len(result.segments),
        "word_timestamp_count": sum(len(segment.words) for segment in result.segments),
        "request_id_recorded": bool(result.request_id),
        "request_id_sha256": hashlib.sha256(result.request_id.encode("utf-8")).hexdigest()
        if result.request_id
        else None,
        "provider_usage": {
            "duration_seconds": result.duration_seconds,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.total_tokens,
            "usage_reported": result.usage_reported,
        },
        "billing_amount": "unknown",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Qwen Audio 短音频真实转写探针")
    parser.add_argument("--live", action="store_true", help="明确允许提交一次系统生成 WAV 的真实请求")
    parser.add_argument("--output-dir", type=Path, help="脱敏摘要目录；默认写入忽略的 data 目录")
    args = parser.parse_args()
    if not args.live:
        print("Dry run only. Pass --live to submit one Windows SAPI generated WAV.")
        return

    started_at = datetime.now(UTC)
    output_dir = args.output_dir or (
        settings.data_dir / "media_evaluations" / f"qwen_audio_transcription_{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = asyncio.run(_run_live_probe(output_dir))
    except Exception as exc:
        summary = {
            "probe": "qwen_audio_short_transcription_v1",
            "route": "media_transcription",
            "live_call_count": 1,
            "outcome": "failed",
            "failure_type": type(exc).__name__,
            "failure_message": str(exc)[:240],
            "billing_amount": "unknown",
        }
        exit_code = 1
    else:
        summary["outcome"] = "completed"
        exit_code = 0
    summary["started_at"] = started_at.isoformat(timespec="seconds")
    summary["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    (output_dir / "run_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(output_dir), **summary}, ensure_ascii=False))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
