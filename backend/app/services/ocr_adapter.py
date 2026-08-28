"""K7.2 本地优先 OCR 的受控 Adapter。

本模块只定义 Runtime 可用的 OCR 协议，不注册 API、不读取 workspace 名称、不写数据库，也不在
导入时安装依赖或下载模型。调用方必须先把客户文件复制/验证到受控目录；Adapter 只接收该内部
``Path``，并仅把按页文字和区域锚点返回给解析层。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Literal

from app.core.config import settings


OCR_MODEL_PROFILE = "paddleocr_v5_mobile_orientation_cpu_v1"
_OCR_READY_MARKER_NAME = "paddleocr_v5_mobile_orientation.ready"
_OCR_REQUIRED_MODEL_DIRS = (
    "PP-OCRv5_mobile_det",
    "PP-OCRv5_mobile_rec",
    "PP-LCNet_x1_0_doc_ori",
)
_SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
_SUPPORTED_SUFFIXES = _SUPPORTED_IMAGE_SUFFIXES | {".pdf"}
_MAX_OCR_PAGES = 100
_OCR_DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 600

OcrErrorCode = Literal[
    "ocr_not_installed",
    "ocr_not_ready",
    "ocr_unsupported_document",
    "ocr_invalid_document",
    "ocr_all_pages_failed",
]


class OcrAdapterError(RuntimeError):
    """面向上层的脱敏 OCR 错误；底层异常和本机路径绝不透传。"""

    def __init__(self, code: OcrErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class LocalOcrConfirmationRequired(RuntimeError):
    """模型下载尚未获得客户确认，调用方不得改为后台自动准备。"""


class OcrDependencyInstallError(RuntimeError):
    """固定可选依赖安装未成功；消息不得含命令输出、本机路径或下载地址。"""


@dataclass(frozen=True)
class OcrCapability:
    """无路径、无正文的本地 OCR 能力快照。"""

    paddleocr_available: bool
    model_initialized: bool
    profile: str
    message: str


@dataclass(frozen=True)
class OcrTextRegion:
    """一段 OCR 文字及其局部区域；页码由 ``OcrPageResult`` 统一持有。"""

    ordinal: int
    text: str
    confidence: float | None
    bounding_box: tuple[tuple[int, int], ...] | None


@dataclass(frozen=True)
class OcrPageResult:
    """单页 OCR 结果，文本只在内存返回给受控解析器，不自动进入日志。"""

    page_number: int
    status: Literal["completed", "failed"]
    text: str
    regions: tuple[OcrTextRegion, ...]
    confidence_average: float | None
    failure_code: str = ""


@dataclass(frozen=True)
class OcrDocumentResult:
    """一份受控材料的 OCR 汇总；允许部分页面成功，拒绝把全失败伪装为可用文本。"""

    document_type: Literal["image", "pdf"]
    pages: tuple[OcrPageResult, ...]

    @property
    def successful_page_count(self) -> int:
        return sum(page.status == "completed" for page in self.pages)

    @property
    def failed_page_count(self) -> int:
        return sum(page.status == "failed" for page in self.pages)


def ocr_capability() -> OcrCapability:
    """检查可选依赖和本项目的 ready 标记，不导入 Paddle、不加载模型、更不联网。"""

    paddleocr_available = importlib.util.find_spec("paddleocr") is not None
    model_initialized = _model_files_ready() and _ready_marker_path().is_file()
    if not paddleocr_available:
        message = "本地 OCR 可选依赖未安装。"
    elif model_initialized:
        message = "本地 OCR 依赖与已确认模型均已准备。"
    else:
        message = "本地 OCR 依赖已准备，需由客户确认下载并初始化模型。"
    return OcrCapability(
        paddleocr_available=paddleocr_available,
        model_initialized=model_initialized,
        profile=OCR_MODEL_PROFILE,
        message=message,
    )


def install_local_ocr_dependencies() -> OcrCapability:
    """经客户确认后安装固定 OCR 可选依赖，不接受任意命令或 requirements 路径。

    普通诊断、导入、解析和索引绝不调用此函数。调用方必须已经展示联网/空间提示并取得本次
    安装许可；本函数只以当前 FastAPI 进程的 Python 执行固定 requirements，再进行 ``pip check``。
    过程不接收、读取或上传任何客户材料。
    """

    current_capability = ocr_capability()
    if current_capability.paddleocr_available:
        return current_capability

    requirements_path = settings.backend_root / "requirements-ocr.txt"
    if not requirements_path.is_file():
        raise OcrDependencyInstallError("本地 OCR 可选组件安装配置不可用。")

    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "-r",
        str(requirements_path),
    ]
    run_options: dict[str, object] = {
        "cwd": str(settings.backend_root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": _OCR_DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
        "check": False,
    }
    # Windows 桌面端不应在客户面前额外弹出命令行窗口；非 Windows 平台保持零值兼容。
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creation_flags:
        run_options["creationflags"] = creation_flags

    try:
        install_result = subprocess.run(command, **run_options)
    except subprocess.TimeoutExpired as exc:
        raise OcrDependencyInstallError("本地 OCR 可选组件安装超时，请检查网络后重试。") from exc
    except OSError as exc:
        raise OcrDependencyInstallError("本地 OCR 可选组件无法启动安装，请稍后重试。") from exc
    if install_result.returncode != 0:
        raise OcrDependencyInstallError("本地 OCR 可选组件安装未完成，请检查网络或磁盘空间后重试。")

    try:
        check_result = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "pip", "check"],
            **run_options,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise OcrDependencyInstallError("本地 OCR 可选组件安装后校验未完成，请稍后重试。") from exc
    if check_result.returncode != 0:
        raise OcrDependencyInstallError("本地 OCR 可选组件依赖校验未通过，请重新安装后再试。")

    importlib.invalidate_caches()
    installed_capability = ocr_capability()
    if not installed_capability.paddleocr_available:
        raise OcrDependencyInstallError("本地 OCR 可选组件安装后仍不可用，请重新安装后再试。")
    return installed_capability


def prepare_local_ocr_model(*, allow_download: bool) -> OcrCapability:
    """显式准备 K7.1 选定的模型；后续 API/UI 必须单独展示下载确认。

    只有准备入口允许 ``allow_download=True``。普通 OCR 调用在模型文件缺失时停在
    ``ocr_not_ready``，不能借 PaddleOCR 的内部默认行为重新发起网络下载。
    """

    capability = ocr_capability()
    if not capability.paddleocr_available:
        raise OcrAdapterError("ocr_not_installed", "本地 OCR 可选依赖未安装。")
    if not capability.model_initialized and not allow_download:
        raise LocalOcrConfirmationRequired(
            "本地 OCR 需要准备约 29MB 模型权重，请先在客户可见入口确认下载。"
        )

    _configure_paddle_cache()
    # 引擎构造会在模型不存在时由 PaddleX 下载；因此它只能出现在明确确认的准备函数内。
    _create_mobile_orientation_engine()
    if not _model_files_ready():
        raise OcrAdapterError("ocr_not_ready", "本地 OCR 模型准备不完整，请重新确认下载。")
    _ready_marker_path().write_text(OCR_MODEL_PROFILE, encoding="utf-8")
    return ocr_capability()


class OcrAdapter:
    """把 PaddleOCR 细节收束为按页、可部分失败、带区域锚点的受控接口。"""

    def __init__(
        self,
        *,
        engine_factory: Callable[[], object] | None = None,
        capability_provider: Callable[[], OcrCapability] = ocr_capability,
    ) -> None:
        # 依赖注入仅用于隔离回归：生产默认仍使用上面的真实能力诊断和固定候选引擎。
        self._engine_factory = engine_factory or _create_mobile_orientation_engine
        self._capability_provider = capability_provider
        self._engine: object | None = None

    def recognize_path(
        self,
        path: Path,
        *,
        page_numbers: Sequence[int] | None = None,
    ) -> OcrDocumentResult:
        """识别受控图片或 PDF 的完整内容，或指定 PDF 页。

        ``page_numbers`` 只服务于 K7.4 的一次页级恢复：解析层已经拥有其它成功页时，才会把
        临时失败页交回来重试。它不是面向 API 的任意页码读取接口，也不会让 Adapter 扫描、
        修改或泄露源文件路径。
        """

        suffix = path.suffix.lower()
        if suffix not in _SUPPORTED_SUFFIXES:
            raise OcrAdapterError("ocr_unsupported_document", "OCR 当前只支持 PNG、JPG、JPEG 和 PDF。")
        if not path.is_file():
            raise OcrAdapterError("ocr_invalid_document", "OCR 材料不可读取或已不存在。")
        self._require_ready_model()
        if suffix == ".pdf":
            return self._recognize_pdf(path, page_numbers=page_numbers)
        if page_numbers is not None:
            normalized_pages = tuple(sorted(set(page_numbers)))
            if normalized_pages not in {(), (1,)}:
                raise OcrAdapterError("ocr_invalid_document", "图片 OCR 不支持指定页面重试。")
        page = self._recognize_image_page(path, page_number=1)
        if page.status != "completed":
            raise OcrAdapterError("ocr_all_pages_failed", "OCR 未能识别该图片中的可用文字。")
        return OcrDocumentResult(document_type="image", pages=(page,))

    def _require_ready_model(self) -> None:
        capability = self._capability_provider()
        if not capability.paddleocr_available:
            raise OcrAdapterError("ocr_not_installed", "本地 OCR 可选依赖未安装。")
        if not capability.model_initialized:
            raise OcrAdapterError(
                "ocr_not_ready",
                "本地 OCR 模型尚未准备，请先在客户可见入口确认下载。",
            )

    def _recognize_pdf(
        self,
        path: Path,
        *,
        page_numbers: Sequence[int] | None,
    ) -> OcrDocumentResult:
        fitz = _load_pymupdf()
        try:
            document = fitz.open(path)
        except Exception as exc:
            raise OcrAdapterError("ocr_invalid_document", "PDF 无法打开，可能已损坏、加密或不是有效 PDF。") from exc

        try:
            page_count = document.page_count
            if page_count < 1 or page_count > _MAX_OCR_PAGES:
                raise OcrAdapterError(
                    "ocr_invalid_document",
                    f"OCR 当前只处理 1 至 {_MAX_OCR_PAGES} 页的 PDF。",
                )
            selected_page_numbers = _selected_pdf_page_numbers(
                page_count=page_count,
                page_numbers=page_numbers,
            )
            pages: list[OcrPageResult] = []
            with tempfile.TemporaryDirectory(prefix="agentflow_ocr_pdf_") as temp_dir:
                for page_number in selected_page_numbers:
                    image_path = Path(temp_dir) / f"page_{page_number}.png"
                    try:
                        # 由 PDF renderer 处理页级 rotation；原 PDF 不会被写回或替换。
                        document.load_page(page_number - 1).get_pixmap(alpha=False).save(image_path)
                        pages.append(self._recognize_image_page(image_path, page_number=page_number))
                    except OcrAdapterError:
                        raise
                    except Exception:
                        pages.append(
                            OcrPageResult(
                                page_number=page_number,
                                status="failed",
                                text="",
                                regions=(),
                                confidence_average=None,
                                failure_code="ocr_page_failed",
                            )
                        )
        finally:
            document.close()

        result = OcrDocumentResult(document_type="pdf", pages=tuple(pages))
        if result.successful_page_count == 0:
            raise OcrAdapterError("ocr_all_pages_failed", "OCR 未能识别该 PDF 中的可用文字。")
        return result

    def _recognize_image_page(self, image_path: Path, *, page_number: int) -> OcrPageResult:
        try:
            raw_results = self._engine_instance().predict(str(image_path))
            regions = _regions_from_results(raw_results)
        except Exception:
            return OcrPageResult(
                page_number=page_number,
                status="failed",
                text="",
                regions=(),
                confidence_average=None,
                failure_code="ocr_page_failed",
            )
        text = "\n".join(region.text for region in regions if region.text.strip()).strip()
        if not text:
            return OcrPageResult(
                page_number=page_number,
                status="failed",
                text="",
                regions=(),
                confidence_average=None,
                failure_code="ocr_no_text",
            )
        confidences = [region.confidence for region in regions if region.confidence is not None]
        return OcrPageResult(
            page_number=page_number,
            status="completed",
            text=text,
            regions=tuple(regions),
            confidence_average=(sum(confidences) / len(confidences)) if confidences else None,
        )

    def _engine_instance(self):
        if self._engine is None:
            self._engine = self._engine_factory()
        return self._engine


def _selected_pdf_page_numbers(
    *,
    page_count: int,
    page_numbers: Sequence[int] | None,
) -> tuple[int, ...]:
    """校验内部页级重试范围，避免坏的 Runtime 参数变成超范围 PDF 读取。"""

    if page_numbers is None:
        return tuple(range(1, page_count + 1))
    selected = tuple(sorted(set(page_numbers)))
    if not selected or any(not isinstance(page_number, int) for page_number in selected):
        raise OcrAdapterError("ocr_invalid_document", "OCR 指定页面范围无效。")
    if selected[0] < 1 or selected[-1] > page_count:
        raise OcrAdapterError("ocr_invalid_document", "OCR 指定页面超出 PDF 范围。")
    return selected


def _regions_from_results(raw_results: object) -> list[OcrTextRegion]:
    """归一化 PaddleOCR 3.x 的结果；仅保留客户解析所需的文字、置信度与区域。"""

    if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
        raise TypeError("OCR 返回结果不是页级序列。")
    regions: list[OcrTextRegion] = []
    for raw_result in raw_results:
        result = _result_mapping(raw_result)
        texts = list(result.get("rec_texts", []))
        scores = list(result.get("rec_scores", []))
        polygons = list(result.get("rec_polys") or result.get("dt_polys") or [])
        for index, raw_text in enumerate(texts, start=1):
            text = str(raw_text).strip()
            if not text:
                continue
            score = _float_or_none(scores[index - 1] if index <= len(scores) else None)
            polygon = polygons[index - 1] if index <= len(polygons) else None
            regions.append(
                OcrTextRegion(
                    ordinal=len(regions) + 1,
                    text=text,
                    confidence=score,
                    bounding_box=_normalize_polygon(polygon),
                )
            )
    return regions


def _result_mapping(raw_result: object) -> Mapping[str, object]:
    if isinstance(raw_result, Mapping):
        return raw_result
    converter = getattr(raw_result, "to_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return converted
    raise TypeError("OCR 返回了无法读取的结果对象。")


def _float_or_none(value: object) -> float | None:
    try:
        number = float(value)  # numpy 标量也会在这里安全归一化。
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _normalize_polygon(value: object) -> tuple[tuple[int, int], ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    points: list[tuple[int, int]] = []
    for point in value:
        if not isinstance(point, Sequence) or isinstance(point, (str, bytes)) or len(point) < 2:
            return None
        try:
            points.append((int(point[0]), int(point[1])))
        except (TypeError, ValueError):
            return None
    return tuple(points) if len(points) >= 4 else None


def _create_mobile_orientation_engine():
    """按 K7.1 选型创建唯一首期模型组合；此函数本身不应出现在普通导入链路。"""

    _configure_paddle_cache()
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:  # pragma: no cover - capability 已先行拦截。
        raise OcrAdapterError("ocr_not_installed", "本地 OCR 可选依赖未安装。") from exc
    return PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="PP-OCRv5_mobile_rec",
        use_doc_orientation_classify=True,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device="cpu",
    )


def _configure_paddle_cache() -> None:
    """在首次导入 PaddleX 前固定缓存与稳定 CPU 兼容配置。"""

    cache_dir = settings.ocr_model_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(cache_dir)
    os.environ["PADDLE_PDX_MODEL_SOURCE"] = "bos"
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    # K7.1 已实测当前 Windows/Paddle 组合的 oneDNN/PIR 路径不稳定，首期固定关闭。
    os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "False"
    os.environ.setdefault("PADDLE_PDX_CPU_NUM_THREADS", "2")


def _model_files_ready() -> bool:
    models_root = settings.ocr_model_cache_dir / "official_models"
    return all((models_root / model_name).is_dir() for model_name in _OCR_REQUIRED_MODEL_DIRS)


def _ready_marker_path() -> Path:
    return settings.ocr_model_cache_dir / _OCR_READY_MARKER_NAME


def _load_pymupdf():
    try:
        import pymupdf as fitz
    except ModuleNotFoundError:  # PyMuPDF 旧版本的兼容导入名。
        import fitz
    return fitz
