"""MM-0 图片编辑计划候选的受限解析与模型调用。"""

from __future__ import annotations

import json

from pydantic import ValidationError

from app.schemas.media_plan import MediaEditPlanCandidate
from app.services.model_gateway import ModelGatewayError, ModelRuntime


class MediaPlanningError(ValueError):
    """图片计划无法作为受限候选使用时的稳定错误。"""


async def resolve_media_edit_plan_candidate(
    *,
    runtime: ModelRuntime,
    user_message: str,
) -> MediaEditPlanCandidate:
    """只让模型在固定工具集内规划，不执行图片操作或扩大外发范围。"""

    return parse_media_edit_plan_candidate(
        await generate_media_edit_plan_content(runtime=runtime, user_message=user_message)
    )


async def generate_media_edit_plan_content(
    *,
    runtime: ModelRuntime,
    user_message: str,
) -> str:
    """生成候选原文，供受控评测保存模型输出或上层再做 Pydantic 解析。"""

    message = " ".join(user_message.split()).strip()
    if not message:
        raise MediaPlanningError("图片编辑请求不能为空。")
    try:
        return await runtime.chat_json(
            system_prompt=build_media_planning_system_prompt(),
            user_message=json.dumps({"request": message}, ensure_ascii=False, separators=(",", ":")),
            maximum_tokens=640,
        )
    except ModelGatewayError as exc:
        raise MediaPlanningError(str(exc)) from exc


def parse_media_edit_plan_candidate(content: str) -> MediaEditPlanCandidate:
    """提取首个合法 JSON 对象，兼容少数模型附带的 Markdown 围栏。"""

    decoder = json.JSONDecoder()
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            return MediaEditPlanCandidate.model_validate(payload)
        except ValidationError as exc:
            raise MediaPlanningError("模型图片编辑计划未通过固定契约校验。") from exc
    raise MediaPlanningError("模型没有返回合法的图片编辑计划 JSON。")


def build_media_planning_system_prompt() -> str:
    return (
        "你是 AgentFlow 图片编辑计划器。只返回一个 JSON 对象，不要 Markdown、解释、推理过程或额外字段。"
        "你不读取图片、不调用模型、不执行工具、不授权上传或导出。你只把用户自然语言请求映射到固定计划。\n"
        "可用工具只有："
        "image.adjust（裁剪、旋转、尺寸、全图明暗/色彩等确定性全局调整）；"
        "image.select_region（先选择人物、物体、背景或文字区域）；"
        "image.edit_region（在已选区域内删除、替换、生成、修补、模糊或改字）；"
        "image.export（导出候选结果）；clarify（用户目标或修改范围不清时提出一个问题）。\n"
        "规则：全局调整的 scope=global，步骤只能是 image.adjust 和最后的 image.export；"
        "局部修改、换背景、抠图、移除对象、换字、虚化背景等 scope=local，必须先 image.select_region，"
        "再 image.edit_region，最后 image.export，并在 target_description 写明目标；"
        "目标不明确时 scope=clarify，steps 只能是 clarify，clarification_question 必须非空，"
        "clarify 的 instruction 也必须简短说明“等待用户补充编辑目标”。"
        "不要编造文件路径、图片内容、尺寸、蒙版、模型名、费用或工具参数。\n"
        "JSON 契约："
        '{"version":"agentflow.media_edit_plan.v1","goal":"","scope":"global|local|clarify",'
        '"target_description":"","steps":[{"tool":"image.adjust|image.select_region|image.edit_region|image.export|clarify",'
        '"instruction":""}],"clarification_question":""}'
    )
