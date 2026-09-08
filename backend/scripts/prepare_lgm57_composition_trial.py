"""列出或显式准备 LGM5.7 开发者试点 Runtime 对。

默认只列出脱敏候选身份；只有带 ``--confirm-prepare`` 时才创建两条试点 Runtime 检查点。
它绝不调用模型、读取材料正文、联网或执行专业 Agent。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.harness.langgraph_composition_trial_catalog import (
    list_composition_developer_trial_sources,
)
from app.harness.langgraph_composition_trial_preparation import (
    prepare_composition_developer_trial_pair,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="LGM5.7 开发者试点候选查看与成对 Runtime 准备。"
    )
    parser.add_argument(
        "--source-task-id",
        help="已完成 C6.4 dry-run 的任务 ID；省略时仅列出候选。",
    )
    parser.add_argument(
        "--confirm-prepare",
        action="store_true",
        help="明确允许写入两条内部 Runtime 检查点；不会调用模型或专业 Agent。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="仅列出候选时的最大数量，范围为 1-100。",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if not args.source_task_id:
        sources = list_composition_developer_trial_sources(limit=args.limit)
        if not sources:
            print("未找到已完成的 C6.4 多材料只读组合计划。")
            return
        print("可用于 LGM5.7 开发者试点的 C6.4 计划：")
        for source in sources:
            actions = ", ".join(source.specialist_actions)
            print(
                f"- task_id={source.task_id} plan_id={source.plan_id} "
                f"plan_digest={source.plan_digest[:16]}… actions={actions}"
            )
        return

    if not args.confirm_prepare:
        raise SystemExit(
            "拒绝创建试点 Runtime：请额外传入 --confirm-prepare。"
        )
    pair = prepare_composition_developer_trial_pair(args.source_task_id)
    print("LGM5.7 试点 Runtime 对已准备：")
    print(f"- source_task_id={pair.source_task_id}")
    print(f"- native_runtime_task_id={pair.native_runtime_task_id}")
    print(f"- graph_candidate_runtime_task_id={pair.graph_candidate_runtime_task_id}")
    print(f"- plan_digest={pair.plan_digest[:16]}…")
    print("未调用模型、未读取材料正文、未联网；下一步仍需单独真实试点授权。")


if __name__ == "__main__":
    main()
