"""用冻结意图集评测一个文本模型的图片编辑规划能力。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.config import settings
from app.services.media_planning import (
    MediaPlanningError,
    generate_media_edit_plan_content,
    parse_media_edit_plan_candidate,
)
from app.services.model_gateway import ModelGatewayError, resolve_model_runtime_for_test
from media_planning_cases import MEDIA_PLANNING_CASES, MediaPlanningCase


def _evaluate_case(case: MediaPlanningCase, plan: object) -> dict[str, object]:
    scope = str(getattr(plan, "scope", ""))
    tools = [str(getattr(item, "tool", "")) for item in getattr(plan, "steps", [])]
    expected_order = list(case.required_tools)
    ordered = all(
        tool in tools and tools.index(tool) >= (tools.index(expected_order[index - 1]) if index else 0)
        for index, tool in enumerate(expected_order)
    )
    forbidden = [tool for tool in case.forbidden_tools if tool in tools]
    passed = scope == case.expected_scope and ordered and not forbidden
    return {
        "case_id": case.case_id,
        "message": case.message,
        "expected_scope": case.expected_scope,
        "required_tools": expected_order,
        "actual_scope": scope,
        "actual_tools": tools,
        "forbidden_tools_present": forbidden,
        "passed": passed,
    }


async def _run_probe() -> dict[str, object]:
    # 这是模型选型探针而非正式 Agent Runtime：显式固定 qwen-plus，避免本地普通 Qwen
    # 文本配置被其它实验模型覆盖。Key 仍只从该 Provider 的安全存储/环境变量解析。
    runtime, key_source = resolve_model_runtime_for_test(
        provider="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus",
        thinking="disabled",
    )
    runtime = replace(runtime, max_tokens=640, temperature=0.0, timeout_seconds=45.0)
    probe_started = datetime.now(UTC)
    cases: list[dict[str, object]] = []
    for case in MEDIA_PLANNING_CASES:
        started = datetime.now(UTC)
        raw_response = ""
        try:
            raw_response = await generate_media_edit_plan_content(runtime=runtime, user_message=case.message)
            plan = parse_media_edit_plan_candidate(raw_response)
        except (MediaPlanningError, ModelGatewayError) as exc:
            cases.append(
                {
                    "case_id": case.case_id,
                    "message": case.message,
                    "expected_scope": case.expected_scope,
                    "required_tools": list(case.required_tools),
                    "passed": False,
                    "error": str(exc)[:240],
                    "raw_response": raw_response[:4_000],
                    "elapsed_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
                }
            )
            continue
        evaluated = _evaluate_case(case, plan)
        evaluated["plan"] = plan.model_dump(mode="json")
        evaluated["raw_response"] = raw_response[:4_000]
        evaluated["elapsed_ms"] = int((datetime.now(UTC) - started).total_seconds() * 1000)
        cases.append(evaluated)
    passed_count = sum(bool(item.get("passed")) for item in cases)
    return {
        "probe": "media_planning_intent_set_v1",
        "started_at": probe_started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": runtime.provider,
        "model": runtime.model,
        "api_key_source": key_source,
        "temperature": runtime.temperature,
        "maximum_tokens": runtime.max_tokens,
        "case_count": len(cases),
        "passed_count": passed_count,
        "pass_rate": round(passed_count / len(cases), 4),
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Media planning 模型选型探针")
    parser.add_argument("--execute", action="store_true", help="明确允许对冻结 20 条意图发起真实文本模型调用")
    parser.add_argument("--output-dir", type=Path, help="证据目录；默认写入 data/media_evaluations")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to evaluate qwen-plus on 20 frozen media planning intents.")
        return

    output_dir = args.output_dir or (
        settings.data_dir / "media_evaluations" / f"media_planning_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = asyncio.run(_run_probe())
    except ModelGatewayError as exc:
        print(f"Media planning probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    (output_dir / "run_manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "output_dir": str(output_dir), **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
