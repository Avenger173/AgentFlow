"""K7.2 OcrAdapter 的完全离线边界回归。

该脚本不会安装 PaddleOCR、下载模型、读取客户文件或调用 LLM。它用临时 PNG/PDF 与假引擎
覆盖未准备、页码/区域、无文本层 PDF、单页失败、旋转图片和损坏文件边界，确保 K7.2 只是
受控 Adapter 协议而不是提前开放客户功能。
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile

import fitz
from PIL import Image, ImageDraw, ImageFont


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_ocr_adapter_verify_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.ocr_adapter import (  # noqa: E402
    OCR_MODEL_PROFILE,
    OcrAdapter,
    OcrAdapterError,
    OcrCapability,
)


class _FakeOcrEngine:
    """只返回固定的结构化 OCR 夹具，不依赖真实本地模型或网络。"""

    def predict(self, image_path: str) -> list[dict[str, object]]:
        if Path(image_path).name == "page_2.png":
            raise RuntimeError("synthetic page failure")
        return [
            {
                "rec_texts": ["扫描件验收", "OCR-17"],
                "rec_scores": [0.98, 0.96],
                "rec_polys": [
                    [[10, 10], [150, 10], [150, 48], [10, 48]],
                    [[10, 70], [150, 70], [150, 108], [10, 108]],
                ],
            }
        ]


def _ready_capability() -> OcrCapability:
    return OcrCapability(True, True, OCR_MODEL_PROFILE, "synthetic ready")


def _not_ready_capability() -> OcrCapability:
    return OcrCapability(True, False, OCR_MODEL_PROFILE, "synthetic not ready")


def _create_image(path: Path, *, rotated: bool = False) -> None:
    font_path = Path(r"C:\Windows\Fonts\msyh.ttc")
    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.text((48, 70), "扫描件验收 OCR-17", font=ImageFont.truetype(font_path, 34), fill="black")
    if rotated:
        image = image.rotate(90, expand=True)
    image.save(path, format="PNG")


def _create_image_pdf(path: Path, image_path: Path) -> None:
    document = fitz.open()
    try:
        for _index in range(2):
            page = document.new_page(width=640, height=360)
            page.insert_image(page.rect, filename=str(image_path))
        document.save(path)
    finally:
        document.close()


def _expect_error(action, expected_code: str) -> None:
    try:
        action()
    except OcrAdapterError as exc:
        assert exc.code == expected_code, exc.code
        assert "\\" not in str(exc) and ":\\" not in str(exc), str(exc)
        return
    raise AssertionError(f"expected OcrAdapterError({expected_code})")


def main() -> None:
    try:
        image_path = VERIFY_ROOT / "synthetic.png"
        rotated_path = VERIFY_ROOT / "synthetic_rotated.png"
        pdf_path = VERIFY_ROOT / "synthetic_image_only.pdf"
        damaged_pdf_path = VERIFY_ROOT / "damaged.pdf"
        _create_image(image_path)
        _create_image(rotated_path, rotated=True)
        _create_image_pdf(pdf_path, image_path)
        damaged_pdf_path.write_bytes(b"not-a-pdf")
        original_image_bytes = image_path.read_bytes()

        not_ready = OcrAdapter(
            engine_factory=_FakeOcrEngine,
            capability_provider=_not_ready_capability,
        )
        _expect_error(lambda: not_ready.recognize_path(image_path), "ocr_not_ready")

        adapter = OcrAdapter(
            engine_factory=_FakeOcrEngine,
            capability_provider=_ready_capability,
        )
        image_result = adapter.recognize_path(image_path)
        assert image_result.document_type == "image"
        assert image_result.successful_page_count == 1
        assert image_result.pages[0].page_number == 1
        assert image_result.pages[0].confidence_average == 0.97
        assert len(image_result.pages[0].regions[0].bounding_box or ()) == 4
        assert image_path.read_bytes() == original_image_bytes

        rotated_result = adapter.recognize_path(rotated_path)
        assert rotated_result.successful_page_count == 1

        pdf_result = adapter.recognize_path(pdf_path)
        assert pdf_result.document_type == "pdf"
        assert pdf_result.successful_page_count == 1
        assert pdf_result.failed_page_count == 1
        assert [page.page_number for page in pdf_result.pages] == [1, 2]
        assert pdf_result.pages[1].failure_code == "ocr_page_failed"
        _expect_error(lambda: adapter.recognize_path(damaged_pdf_path), "ocr_invalid_document")
        _expect_error(lambda: adapter.recognize_path(VERIFY_ROOT / "unsupported.docx"), "ocr_unsupported_document")

        print(
            "K7.2 OCR adapter verification passed: "
            "not-ready/image/rotation/image-only-pdf-partial-failure/damaged/unsupported boundaries."
        )
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
