"""K7.1 本地 OCR 候选的隔离技术验证。

这个脚本不是客户功能，也不会导入 AgentFlow 的数据库、工作区或任何客户文件。默认模式只说明
验证边界；只有显式传入 ``--live-local`` 才会在系统临时目录生成一张固定的中英文字测试图，并
让当前 Python 环境中的 PaddleOCR 下载/初始化其本地模型。所有模型缓存、测试图片和临时输出都
由 ``PADDLE_PDX_CACHE_HOME`` 约束在本次临时目录，脚本结束后默认清理。

运行方式（仅 K7.1 选型验证，不要在日常 backend venv 中无意执行）：

    <isolated-python> backend/scripts/verify_ocr_technology_probe.py --live-local

脚本只输出脱敏的可用性、耗时、缓存体积与锚点数量，不打印识别全文、绝对缓存路径或模型下载 URL。
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import shutil
import statistics
import tempfile
import time


def _directory_size_bytes(directory: Path) -> int:
    """统计临时缓存大小；忽略竞态删除的文件，避免探针因清理时序失败。"""

    total = 0
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            # PaddleX 在下载结束时可能原子移动临时文件；该文件不影响选型结论。
            continue
    return total


def _create_synthetic_image(image_path: Path) -> None:
    """生成固定测试页，确保技术探针从不读取客户图片或文档。"""

    from PIL import Image, ImageDraw, ImageFont

    font_path = Path(r"C:\Windows\Fonts\msyh.ttc")
    if not font_path.is_file():
        raise RuntimeError("本机缺少 K7.1 合成中文图片所需的微软雅黑字体。")

    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(str(font_path), size=58)
    body_font = ImageFont.truetype(str(font_path), size=42)
    draw.text((100, 110), "AgentFlow OCR 技术验证", font=title_font, fill="#102a56")
    draw.text((100, 260), "扫描件验收编号 OCR-17", font=body_font, fill="#202124")
    draw.text((100, 360), "中文 English 123", font=body_font, fill="#202124")
    draw.text((100, 500), "仅用于本地候选模型验证", font=body_font, fill="#202124")
    image.save(image_path, format="PNG", optimize=True)


def _create_image_only_pdf(image_path: Path, pdf_path: Path, rendered_path: Path) -> None:
    """构造无文本层 PDF 并按页渲染，贴近未来 Adapter 的页码处理方式。"""

    fitz = _load_pymupdf()

    document = fitz.open()
    try:
        page = document.new_page(width=1600, height=900)
        page.insert_image(page.rect, filename=str(image_path))
        document.save(pdf_path)
    finally:
        document.close()

    opened = fitz.open(pdf_path)
    try:
        page = opened.load_page(0)
        if page.get_text("text").strip():
            raise AssertionError("K7.1 合成 PDF 意外拥有文本层，不能验证扫描件路径。")
        # 后续 OcrAdapter 需要自己持有 PDF 页码；OCR 只处理这一张受控渲染图。
        page.get_pixmap(alpha=False).save(rendered_path)
    finally:
        opened.close()


def _create_rotated_image(image_path: Path, rotated_path: Path) -> None:
    """构造 90 度旋转夹具；首期不宣称旋转图片一定可识别，只记录事实。"""

    from PIL import Image

    with Image.open(image_path) as source:
        source.rotate(90, expand=True).save(rotated_path, format="PNG", optimize=True)


def _assert_damaged_pdf_is_rejected(pdf_path: Path) -> None:
    """确认解析器能把损坏 PDF 作为可解释错误，而不是交给 OCR 盲目处理。"""

    fitz = _load_pymupdf()

    pdf_path.write_bytes(b"not-a-pdf")
    try:
        fitz.open(pdf_path)
    except Exception:  # PyMuPDF 的具体异常类型会随版本改变；只需确认坏文件被拒绝。
        return
    raise AssertionError("K7.1 损坏 PDF 夹具没有被解析器拒绝。")


def _load_pymupdf():
    """兼容当前与较早的 PyMuPDF 导入名，避免验证日志出现无关弃用提示。"""

    try:
        import pymupdf as fitz
    except ModuleNotFoundError:
        import fitz
    return fitz


def _process_rss_bytes() -> int | None:
    """读取当前进程 RSS；缺少可选观测依赖时保留未知，而不是虚构内存指标。"""

    try:
        import psutil
    except ModuleNotFoundError:
        return None
    return psutil.Process().memory_info().rss


def _extract_text_and_anchor_count(results: list[object]) -> tuple[str, int]:
    """兼容 PaddleOCR 3.x 的结果对象，只取最小验证字段。"""

    recognized_texts: list[str] = []
    anchor_count = 0
    for result in results:
        if not isinstance(result, dict):
            # PaddleX Result 在当前版本实现 Mapping；此分支保守支持未来的 to_dict API。
            converter = getattr(result, "to_dict", None)
            if not callable(converter):
                raise TypeError("PaddleOCR 返回了无法读取的结果对象。")
            result = converter()
        texts = result.get("rec_texts", [])
        polygons = result.get("rec_polys") or result.get("dt_polys") or []
        recognized_texts.extend(str(item) for item in texts if str(item).strip())
        anchor_count += len(polygons)
    return "\n".join(recognized_texts), anchor_count


def _create_engine(profile: str):
    """按固定候选档位创建 OCR 引擎，禁止隐式混用模型组合。"""

    from paddleocr import PaddleOCR

    common_options = {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "device": "cpu",
    }
    if profile == "server":
        return PaddleOCR(lang="ch", ocr_version="PP-OCRv5", **common_options)
    if profile == "mobile":
        # v5 默认中文路径使用 server 模型；K7.1 需显式换成更小的同代检测/识别组合。
        return PaddleOCR(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            **common_options,
        )
    if profile == "mobile-orientation":
        # 只增加方向分类器来处理拍照/扫描旋转，不引入表格、公式或版面理解模型。
        return PaddleOCR(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=True,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device="cpu",
        )
    raise ValueError(f"未知 OCR 候选档位：{profile}")


def _run_live_local_probe(keep_artifacts: bool, profile: str, warm_runs: int) -> None:
    """在隔离缓存中执行一次冷启动和一次热调用，输出无正文的选型事实。"""

    probe_root = Path(tempfile.mkdtemp(prefix="agentflow_k7_ocr_live_probe_"))
    cache_dir = probe_root / "paddlex_cache"
    image_path = probe_root / "synthetic_ocr_probe.png"
    pdf_path = probe_root / "synthetic_image_only.pdf"
    rendered_pdf_page_path = probe_root / "synthetic_image_only_page_1.png"
    rotated_image_path = probe_root / "synthetic_ocr_probe_rotated.png"
    damaged_pdf_path = probe_root / "synthetic_damaged.pdf"

    # 必须在导入 PaddleOCR 前设置：PaddleX 会在模块加载时读取这些环境变量。
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(cache_dir)
    os.environ["PADDLE_PDX_MODEL_SOURCE"] = "bos"
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    # Paddle 3.3.1 在当前 Windows CPU 的 oneDNN/PIR 组合会抛出不支持的属性转换错误。
    # K7 首期优先验证稳定的纯 Paddle CPU 路径；是否重新启用 oneDNN 是后续性能探针，而非
    # 客户导入正确性的前置条件。
    os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "False"
    # 技术探针只测单页 CPU 可用性，限制线程避免影响正在使用电脑的客户。
    os.environ["PADDLE_PDX_CPU_NUM_THREADS"] = "2"

    try:
        _create_synthetic_image(image_path)
        _create_image_only_pdf(image_path, pdf_path, rendered_pdf_page_path)
        _create_rotated_image(image_path, rotated_image_path)
        _assert_damaged_pdf_is_rejected(damaged_pdf_path)

        try:
            import paddleocr  # noqa: F401 - 仅在显式 live 探针中验证隔离依赖可用。
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "当前 Python 环境没有 PaddleOCR；请仅用 K7.1 隔离环境安装候选依赖。"
            ) from exc

        initialize_started = time.perf_counter()
        engine = _create_engine(profile)
        initialize_ms = round((time.perf_counter() - initialize_started) * 1000)

        cold_started = time.perf_counter()
        cold_results = engine.predict(str(image_path))
        cold_ms = round((time.perf_counter() - cold_started) * 1000)
        recognized_text, anchor_count = _extract_text_and_anchor_count(cold_results)

        warm_samples_ms: list[int] = []
        for _run_index in range(warm_runs):
            warm_started = time.perf_counter()
            warm_results = engine.predict(str(image_path))
            warm_samples_ms.append(round((time.perf_counter() - warm_started) * 1000))
            warm_text, warm_anchor_count = _extract_text_and_anchor_count(warm_results)
            if warm_text != recognized_text or warm_anchor_count != anchor_count:
                raise AssertionError("本地 OCR 热调用结果与首次调用不一致。")

        # 只验证固定标识符和至少一个中文提示，不把整段识别文字写进日志或文档。
        required_markers = ("OCR-17", "扫描")
        marker_match_count = sum(marker in recognized_text for marker in required_markers)
        if marker_match_count != len(required_markers):
            raise AssertionError("本地 OCR 未识别出 K7.1 合成样本的必要标识。")
        pdf_started = time.perf_counter()
        pdf_results = engine.predict(str(rendered_pdf_page_path))
        pdf_page_ms = round((time.perf_counter() - pdf_started) * 1000)
        pdf_text, pdf_anchor_count = _extract_text_and_anchor_count(pdf_results)
        pdf_marker_match_count = sum(marker in pdf_text for marker in required_markers)
        if pdf_marker_match_count != len(required_markers):
            raise AssertionError("本地 OCR 未识别出无文本层 PDF 渲染页的必要标识。")

        # 旋转图片的结果只作为选型事实。首期 Adapter 若没有方向元数据，不能假装它一定准确。
        rotated_results = engine.predict(str(rotated_image_path))
        rotated_text, _rotated_anchor_count = _extract_text_and_anchor_count(rotated_results)
        rotated_marker_match_count = sum(marker in rotated_text for marker in required_markers)
        if profile == "mobile-orientation" and rotated_marker_match_count != len(required_markers):
            raise AssertionError("方向分类候选未识别出旋转合成样本的必要标识。")

        sorted_warm_samples = sorted(warm_samples_ms)
        warm_p95_index = max(0, math.ceil(len(sorted_warm_samples) * 0.95) - 1)
        warm_median_ms = round(statistics.median(sorted_warm_samples))
        warm_p95_ms = sorted_warm_samples[warm_p95_index]
        process_rss_bytes = _process_rss_bytes()

        print(
            "K7.1 local OCR probe: "
            f"status=passed profile={profile} init_ms={initialize_ms} cold_page_ms={cold_ms} "
            f"warm_runs={warm_runs} warm_page_median_ms={warm_median_ms} "
            f"warm_page_p95_ms={warm_p95_ms} anchors={anchor_count} "
            f"required_markers={marker_match_count}/{len(required_markers)} "
            f"pdf_page_ms={pdf_page_ms} pdf_anchors={pdf_anchor_count} "
            f"pdf_required_markers={pdf_marker_match_count}/{len(required_markers)} "
            f"rotated_required_markers={rotated_marker_match_count}/{len(required_markers)} "
            "damaged_pdf=rejected "
            f"cache_bytes={_directory_size_bytes(cache_dir)} process_rss_bytes={process_rss_bytes}"
        )
    finally:
        if keep_artifacts:
            print("K7.1 local OCR probe: temporary artifacts retained by explicit request")
        else:
            shutil.rmtree(probe_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="K7.1 PaddleOCR 隔离技术验证")
    parser.add_argument(
        "--live-local",
        action="store_true",
        help="显式允许下载/初始化本地候选模型，并只识别脚本生成的测试图片。",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="仅用于人工排障时保留系统临时文件；默认会清理全部探针缓存与图片。",
    )
    parser.add_argument(
        "--profile",
        choices=("mobile", "mobile-orientation", "server"),
        default="server",
        help="候选模型档位；默认 server 保留 PaddleOCR 的中文默认行为。",
    )
    parser.add_argument(
        "--warm-runs",
        type=int,
        default=5,
        help="同一合成页的热调用次数，用于得出受控的中位数与 P95。",
    )
    args = parser.parse_args()

    if not args.live_local:
        print(
            "K7.1 local OCR probe: skipped "
            "(pass --live-local to explicitly prepare a temporary local candidate model)"
        )
        return
    if args.warm_runs < 2 or args.warm_runs > 10:
        raise ValueError("--warm-runs 必须在 2 到 10 之间，避免探针无界运行。")

    _run_live_local_probe(args.keep_artifacts, args.profile, args.warm_runs)


if __name__ == "__main__":
    main()
