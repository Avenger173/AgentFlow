"""离线验证 MM-0 图片计划契约与冻结意图集。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.schemas.media_plan import MediaEditPlanCandidate
from app.services.media_planning import (
    MediaPlanningError,
    build_media_planning_system_prompt,
    parse_media_edit_plan_candidate,
    resolve_media_edit_plan_candidate,
)
from media_planning_cases import MEDIA_PLANNING_CASES


def _valid_global() -> str:
    return json.dumps(
        {
            "version": "agentflow.media_edit_plan.v1",
            "goal": "整体提亮照片",
            "scope": "global",
            "target_description": "",
            "steps": [
                {"tool": "image.adjust", "instruction": "整体提亮并限制高光"},
                {"tool": "image.export", "instruction": "导出候选结果"},
            ],
            "clarification_question": "",
        },
        ensure_ascii=False,
    )


def _valid_local() -> str:
    return json.dumps(
        {
            "version": "agentflow.media_edit_plan.v1",
            "goal": "移除日期水印",
            "scope": "local",
            "target_description": "左下角日期水印",
            "steps": [
                {"tool": "image.select_region", "instruction": "选择左下角日期水印"},
                {"tool": "image.edit_region", "instruction": "仅移除选中水印并修补背景"},
                {"tool": "image.export", "instruction": "导出候选结果"},
            ],
            "clarification_question": "",
        },
        ensure_ascii=False,
    )


def _verify_schema_boundaries() -> None:
    assert parse_media_edit_plan_candidate("```json\n" + _valid_global() + "\n```").scope == "global"
    assert parse_media_edit_plan_candidate(_valid_local()).scope == "local"

    invalid_local = json.loads(_valid_local())
    invalid_local["steps"] = [
        {"tool": "image.edit_region", "instruction": "移除水印"},
        {"tool": "image.export", "instruction": "导出"},
    ]
    try:
        MediaEditPlanCandidate.model_validate(invalid_local)
    except ValueError:
        pass
    else:
        raise AssertionError("局部编辑跳过选区不应通过契约。")

    invalid_tool = json.loads(_valid_global())
    invalid_tool["steps"][0]["tool"] = "shell.exec"
    try:
        parse_media_edit_plan_candidate(json.dumps(invalid_tool, ensure_ascii=False))
    except MediaPlanningError:
        pass
    else:
        raise AssertionError("未知工具不应通过契约。")


class _CapturingRuntime:
    def __init__(self) -> None:
        self.system_prompt = ""
        self.user_payload: dict[str, object] = {}

    async def chat_json(self, *, system_prompt: str, user_message: str, maximum_tokens: int) -> str:
        self.system_prompt = system_prompt
        self.user_payload = json.loads(user_message)
        assert maximum_tokens == 640
        return _valid_global()


def _verify_model_boundary() -> None:
    runtime = _CapturingRuntime()
    result = asyncio.run(resolve_media_edit_plan_candidate(runtime=runtime, user_message="  整体  提亮  一点  "))
    assert result.scope == "global"
    assert runtime.user_payload == {"request": "整体 提亮 一点"}
    assert "shell" not in runtime.system_prompt
    assert "image.select_region" in runtime.system_prompt
    assert "image.edit_region" in runtime.system_prompt
    assert "等待用户补充编辑目标" in build_media_planning_system_prompt()


def _verify_frozen_cases() -> None:
    assert len(MEDIA_PLANNING_CASES) == 20
    assert len({item.case_id for item in MEDIA_PLANNING_CASES}) == 20
    for item in MEDIA_PLANNING_CASES:
        assert item.message
        assert item.expected_scope in {"global", "local", "clarify"}
        assert item.required_tools
        if item.expected_scope == "clarify":
            assert item.required_tools == ("clarify",)


def main() -> None:
    _verify_schema_boundaries()
    _verify_model_boundary()
    _verify_frozen_cases()
    print("Media planning contract verification passed.")


if __name__ == "__main__":
    main()
