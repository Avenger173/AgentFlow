"""在隔离工作区验证指定 Provider 的文档助手真实闭环。

这个脚本与 ``verify_llm.py`` 的普通聊天连通性检查不同：它会验证 Document Agent 的
Tool Calling、JSON Guardrail 和来源映射。验收材料、SQLite 任务和 workspace 文档都写入临时
目录；API Key 仅在进程内从 DPAPI / 环境变量解析，绝不输出或保存到临时目录。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证指定模型 Provider 的文档助手真实闭环。")
    parser.add_argument(
        "--provider",
        default="",
        help="可选：deepseek、kimi、openai、anthropic、qwen 或 openai_compatible。默认使用当前配置。",
    )
    parser.add_argument(
        "--output-mode",
        choices=("requirements", "draft"),
        default="requirements",
        help="验收输出契约。draft 用于覆盖项目方案 PPT 前置草稿与格式修复路径。",
    )
    parser.add_argument(
        "--fixture",
        choices=("short", "planning_document"),
        default="short",
        help="验收材料。planning_document 使用项目初版规划，覆盖长材料草稿输出预算。",
    )
    return parser.parse_args()


def _source_data_dir() -> Path:
    """在切换到隔离 data_dir 前记住客户真实配置所在目录。"""

    configured = os.getenv("AGENTFLOW_DATA_DIR", "").strip()
    return Path(configured).resolve() if configured else PROJECT_ROOT / "data"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = _parse_args()
    source_data_dir = _source_data_dir()
    temporary_data_dir = Path(tempfile.mkdtemp(prefix="agentflow_document_llm_verify_"))
    os.environ["AGENTFLOW_DATA_DIR"] = str(temporary_data_dir)
    os.environ["AGENTFLOW_CHAT_MODE"] = "llm"
    sys.path.insert(0, str(BACKEND_ROOT))

    try:
        # 延迟导入：Settings 的 data_dir 是按环境变量动态计算的，先切换隔离目录才能保证任务、
        # workspace 和 SQLite 不落入客户日常数据。真实模型 Key 则通过显式 source path 读取。
        from app.core.config import settings
        from app.services import document_agent as document_agent_service
        from app.services.model_config_store import ModelConfigRepository
        from app.services.model_gateway import resolve_model_runtime_for_test

        source_config = ModelConfigRepository(path=source_data_dir / "model_config.json").load()
        provider = (args.provider or source_config.provider or settings.llm_provider).strip().lower()
        stored_key = (
            source_config.decrypt_api_key(provider)
            if source_config.api_key_configured_for(provider)
            else None
        )
        use_current_profile = source_config.provider == provider
        connection_runtime, key_source = resolve_model_runtime_for_test(
            provider=provider,
            base_url=source_config.base_url if use_current_profile else None,
            model=source_config.model if use_current_profile else None,
            thinking=source_config.thinking if use_current_profile else "disabled",
            api_key=stored_key,
        )
        # resolve_model_runtime_for_test 是模型页的轻量连通性入口，会有意把输出限制到 64 tokens。
        # 文档 Agent 的结构化 JSON 不能复用这个上限，否则 requirements 较多时会被测试工具本身
        # 截断。这里只恢复正式服务的输出预算，不改变用户保存的模型配置。
        runtime = replace(connection_runtime, max_tokens=settings.llm_max_tokens)
    except Exception:
        # Provider 未配置、DPAPI 读取失败等前置错误同样不能残留空的临时数据目录。
        shutil.rmtree(temporary_data_dir, ignore_errors=True)
        raise

    original_runtime_resolver = document_agent_service.resolve_model_runtime
    document_agent_service.resolve_model_runtime = lambda: runtime
    try:
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        fixture_filename = "document_agent_provider_verify.md"
        fixture_content = (
            "# 文档助手验收材料\n\n"
            "系统必须保留每项结论的来源位置。\n"
            "输出需要列出可验证的需求清单。\n"
            "不得修改用户导入的原始材料。\n"
        )
        if args.fixture == "planning_document":
            # 真实客户失败来自较大的项目规划材料。该夹具只复制到隔离 workspace，任务数据库和
            # 输出都在临时目录；脚本最终仅打印聚合验收指标，不回显文档正文。
            planning_path = PROJECT_ROOT / "AgentFlow_初版规划.md"
            fixture_filename = planning_path.name
            fixture_content = planning_path.read_text(encoding="utf-8")

        imported = client.post(
            "/api/workspace/documents",
            json={
                "filename": fixture_filename,
                "content": fixture_content,
            },
        )
        imported.raise_for_status()
        output_mode = args.output_mode
        task_goal = (
            "根据材料生成用于项目方案 PPT 的可核验草稿。"
            if output_mode == "draft"
            else "提取材料中的明确需求，并给出来源。"
        )
        response = client.post(
            "/api/agents/document_agent/run",
            json={
                "task_goal": task_goal,
                "document_refs": [imported.json()["relative_path"]],
                "output_mode": output_mode,
            },
        )
        response.raise_for_status()
        payload = response.json()
        tool_calls = client.get(f"/api/tasks/{payload['task_id']}/tool-calls")
        tool_calls.raise_for_status()
        context = payload["document_context"]
        tool_names = [item["tool_name"] for item in tool_calls.json()["tool_calls"]]

        assert payload["mode"] == "llm"
        assert payload["status"] == "completed", payload["reply"]
        assert payload["stop_reason"] == "completed", payload["stop_reason"]
        assert tool_names == ["document.read_text"], tool_names
        assert context["sources"], "最终结果缺少可追溯来源。"
        if output_mode == "draft":
            # PPT 只能消费已有来源、已完成的草稿。这里特意验证复杂草稿契约，防止普通
            # requirements 冒烟通过后，格式修复分支在客户实际点击 PPT 时才暴露问题。
            assert context["draft_sections"], "最终结果缺少可交付草稿章节。"
        else:
            assert context["requirements"], "最终结果缺少需求条目。"
        print(
            "Document Agent provider verification passed: "
            f"provider={runtime.provider} model={runtime.model} key_source={key_source} "
            f"fixture={args.fixture} output_mode={output_mode} tools={tool_names} sources={len(context['sources'])} "
            f"requirements={len(context['requirements'])} draft_sections={len(context['draft_sections'])}"
        )
    finally:
        document_agent_service.resolve_model_runtime = original_runtime_resolver
        shutil.rmtree(temporary_data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
