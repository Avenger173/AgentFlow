"""K7 本地 OCR 的人工授权真实验收。

默认不读取任何文件，也不安装 PaddleOCR、更不下载模型。只有客户已在知识库页面明确准备可选
组件与本地模型后，才可显式传入 ``--live-local --input <材料>`` 运行。脚本会把每份指定材料
复制到系统临时目录，再复用产品的受控解析入口验证实际路由；不会输出识别正文、文件名、绝对路径、
坐标、模型目录或凭据，也不会修改原材料、workspace、资料库或任务历史。

示例（由客户自行选择本机测试材料）：

    backend\\.venv\\Scripts\\python.exe backend\\scripts\\verify_live_ocr_acceptance.py \\
        --live-local --input C:\\scan.pdf --input C:\\scan.jpg --input C:\\text-layer.pdf

建议至少选择一份扫描 PDF、一张 PNG/JPG/JPEG 与一份可复制文本 PDF。扫描材料应显示 OCR 页级
统计和来源片段数；文本 PDF 应显示 ``text_pdf_skipped``，证明它没有被无谓地送入 OCR。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.ocr_adapter import ocr_capability
from app.services.workspace_documents import WorkspaceDocumentError, parse_controlled_document


SUPPORTED_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg"})


def _parse_arguments() -> argparse.Namespace:
    """声明显式执行开关，防止日常回归误读客户本机材料。"""

    parser = argparse.ArgumentParser(description="运行 K7 本地 OCR 的脱敏真实验收。")
    parser.add_argument(
        "--live-local",
        action="store_true",
        help="确认仅在本机读取指定材料副本；不联网、不安装组件、不下载模型。",
    )
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="PATH",
        help="客户明确选择的 PDF、PNG、JPG 或 JPEG 验收材料；可重复传入。",
    )
    return parser.parse_args()


def _require_ready_capability() -> None:
    """在复制客户材料前完成无副作用预检，缺失时给出 UI 中的下一步。"""

    capability = ocr_capability()
    print(
        "OCR capability: "
        f"dependency={'ready' if capability.paddleocr_available else 'missing'} "
        f"model={'ready' if capability.model_initialized else 'missing'} "
        f"profile={capability.profile}"
    )
    if capability.paddleocr_available and capability.model_initialized:
        return
    raise SystemExit(
        "K7 real acceptance skipped: open AgentFlow Knowledge Base, confirm local OCR preparation, "
        "then rerun this command. This script never installs dependencies or downloads models."
    )


def _material_path(raw_path: str) -> Path:
    """校验用户显式给出的输入；错误不回显客户路径，避免日志泄露。"""

    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise ValueError("指定的验收材料不可读取。")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("验收材料只支持 PDF、PNG、JPG 或 JPEG。")
    return path


def _route_name(*, suffix: str, ocr_page_count: int) -> str:
    """只输出产品可解释的路由事实，不泄露材料文本或内部文件信息。"""

    if ocr_page_count:
        return "local_ocr"
    if suffix == ".pdf":
        return "text_pdf_skipped"
    return "non_ocr_parser"


def _run_materials(raw_inputs: list[str]) -> tuple[int, int]:
    """在临时副本上复用产品解析链路，验收来源锚点与页级统计。"""

    failures = 0
    local_ocr_routes = 0
    with tempfile.TemporaryDirectory(prefix="agentflow_live_ocr_acceptance_") as temporary_directory:
        temporary_root = Path(temporary_directory)
        for index, raw_path in enumerate(raw_inputs, start=1):
            try:
                source_path = _material_path(raw_path)
                # 使用通用文件名隔离客户命名信息；copy2 只读取源材料，不改动它。
                controlled_copy = temporary_root / f"material_{index}{source_path.suffix.lower()}"
                shutil.copy2(source_path, controlled_copy)
                parsed = parse_controlled_document(controlled_copy)
            except (OSError, ValueError, WorkspaceDocumentError):
                # OSError 的字符串可能包含客户路径；验收日志只保留可行动的脱敏原因码。
                print(f"material={index} status=failed reason=unreadable_or_parse_rejected")
                failures += 1
                continue

            route = _route_name(
                suffix=source_path.suffix.lower(),
                ocr_page_count=parsed.ocr_page_count,
            )
            if route == "local_ocr":
                local_ocr_routes += 1
            print(
                f"material={index} status=completed route={route} type={parsed.document_type} "
                f"anchors={len(parsed.segments)} ocr_pages={parsed.ocr_completed_page_count}/"
                f"{parsed.ocr_page_count} failed_pages={parsed.ocr_failed_page_count} "
                f"retried_pages={parsed.ocr_retried_page_count}"
            )
    return failures, local_ocr_routes


def main() -> None:
    """执行无副作用预检或经客户确认的、本地临时副本验收。"""

    args = _parse_arguments()
    if not args.live_local:
        raise SystemExit(
            "This script reads locally selected materials. Pass --live-local and at least one --input PATH."
        )
    if not args.input:
        raise SystemExit("Pass at least one --input PATH after --live-local.")
    _require_ready_capability()
    failures, local_ocr_routes = _run_materials(args.input)
    if failures:
        raise SystemExit("K7 real acceptance failed: one or more selected materials were rejected.")
    if not local_ocr_routes:
        raise SystemExit(
            "K7 real acceptance incomplete: no selected material entered local OCR; "
            "include at least one scan PDF or PNG/JPG/JPEG."
        )
    print("K7 live OCR acceptance passed: local OCR routing and source anchors are available.")


if __name__ == "__main__":
    main()
