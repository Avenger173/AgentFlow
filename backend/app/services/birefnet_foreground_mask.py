"""BiRefNet Tiny ONNX 的本地前景蒙版适配器。

它是 MM-0 的可执行技术探针，不是已经向用户开放的图片工作区能力。模型只能输出
显著前景 alpha 蒙版：不能理解文字指令、不能根据点/框选择任意对象，也不能生成新像素。
因此它不得替代后续 ``media_segmentation`` 的 SAM 路线。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from PIL import Image, ImageOps

from app.core.config import settings


_MODEL_ID = "birefnet-general-tiny-v1"
_WEIGHT_FILE_NAME = "BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx"
_INPUT_NAME = "input_image"
_OUTPUT_NAME = "output_image"
_MODEL_SIZE = 1024
_MAX_SOURCE_PIXELS = 40_000_000
_IMAGE_NET_MEAN = (0.485, 0.456, 0.406)
_IMAGE_NET_STD = (0.229, 0.224, 0.225)


class BiRefNetForegroundMaskError(RuntimeError):
    """本地前景蒙版无法安全执行时的稳定错误。"""


@dataclass(frozen=True)
class BiRefNetForegroundMaskReadiness:
    """只暴露本机可执行性事实，不把“权重存在”误报为质量通过。"""

    model_id: str
    ready: bool
    device: str
    model_path: Path
    reason: str = ""


@dataclass(frozen=True)
class BiRefNetForegroundMaskResult:
    """原图尺寸对齐的灰度 alpha 蒙版，不会写入文件或持久化客户图像。"""

    model_id: str
    alpha_mask: Image.Image
    source_size: tuple[int, int]
    elapsed_ms: int


def default_birefnet_model_path() -> Path:
    """返回受控模型缓存位置；权重从不随应用启动自动下载。"""

    return settings.data_dir / "media_model_cache" / "birefnet_tiny" / _WEIGHT_FILE_NAME


def inspect_birefnet_foreground_mask_readiness(
    *,
    model_path: Path | None = None,
) -> BiRefNetForegroundMaskReadiness:
    """检查依赖和权重，不加载会话或执行模型。"""

    resolved_path = (model_path or default_birefnet_model_path()).resolve()
    if not resolved_path.is_file():
        return BiRefNetForegroundMaskReadiness(
            model_id=_MODEL_ID,
            ready=False,
            device="cpu",
            model_path=resolved_path,
            reason="未找到 BiRefNet Tiny ONNX 权重，当前不能生成前景蒙版。",
        )
    try:
        _load_onnxruntime()
        _load_numpy()
    except BiRefNetForegroundMaskError as exc:
        return BiRefNetForegroundMaskReadiness(
            model_id=_MODEL_ID,
            ready=False,
            device="cpu",
            model_path=resolved_path,
            reason=str(exc),
        )
    return BiRefNetForegroundMaskReadiness(
        model_id=_MODEL_ID,
        ready=True,
        device="cpu",
        model_path=resolved_path,
    )


def create_birefnet_foreground_mask_session(*, model_path: Path | None = None) -> Any:
    """显式加载 CPU 会话，供后台 worker 复用；不在 UI 或请求线程预加载。"""

    readiness = inspect_birefnet_foreground_mask_readiness(model_path=model_path)
    if not readiness.ready:
        raise BiRefNetForegroundMaskError(readiness.reason)
    runtime = _load_onnxruntime()
    try:
        session = runtime.InferenceSession(
            str(readiness.model_path),
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:  # pragma: no cover - 外部 runtime 的具体错误随平台变化。
        raise BiRefNetForegroundMaskError("BiRefNet Tiny ONNX 权重无法在当前 CPU 环境加载。") from exc
    _validate_session_contract(session)
    return session


def create_birefnet_foreground_alpha_mask(
    image: Image.Image,
    *,
    session: Any | None = None,
    model_path: Path | None = None,
) -> BiRefNetForegroundMaskResult:
    """从单张图片生成与原图对齐的 alpha 蒙版。

    调用者持有输入与结果对象，函数自身不读取路径、不上传图片、不写入磁盘。生产接入时，
    只能从受控资产区读取图像，并把结果绑定到具体工程 revision。
    """

    source = _prepare_source_image(image)
    active_session = session or create_birefnet_foreground_mask_session(model_path=model_path)
    _validate_session_contract(active_session)
    tensor = _prepare_input_tensor(source)
    started = perf_counter()
    try:
        outputs = active_session.run([_OUTPUT_NAME], {_INPUT_NAME: tensor})
    except Exception as exc:  # pragma: no cover - 外部 runtime 的具体错误随平台变化。
        raise BiRefNetForegroundMaskError("BiRefNet Tiny ONNX 推理失败。") from exc
    elapsed_ms = max(0, round((perf_counter() - started) * 1000))
    alpha_mask = _restore_alpha_mask(outputs, source.size)
    return BiRefNetForegroundMaskResult(
        model_id=_MODEL_ID,
        alpha_mask=alpha_mask,
        source_size=source.size,
        elapsed_ms=elapsed_ms,
    )


def _prepare_source_image(image: Image.Image) -> Image.Image:
    if not isinstance(image, Image.Image):
        raise BiRefNetForegroundMaskError("前景蒙版输入必须是已解码的图片对象。")
    normalized = ImageOps.exif_transpose(image)
    width, height = normalized.size
    if width < 1 or height < 1 or width * height > _MAX_SOURCE_PIXELS:
        raise BiRefNetForegroundMaskError("图片尺寸无效或超过 4000 万像素的本地前景蒙版上限。")
    return normalized.convert("RGB")


def _prepare_input_tensor(image: Image.Image) -> Any:
    numpy = _load_numpy()
    resized = image.resize((_MODEL_SIZE, _MODEL_SIZE), Image.Resampling.BILINEAR)
    array = numpy.asarray(resized, dtype=numpy.float32) / 255.0
    mean = numpy.asarray(_IMAGE_NET_MEAN, dtype=numpy.float32)
    std = numpy.asarray(_IMAGE_NET_STD, dtype=numpy.float32)
    normalized = (array - mean) / std
    return numpy.ascontiguousarray(numpy.transpose(normalized, (2, 0, 1))[None, ...], dtype=numpy.float32)


def _restore_alpha_mask(outputs: object, source_size: tuple[int, int]) -> Image.Image:
    numpy = _load_numpy()
    if not isinstance(outputs, (list, tuple)) or len(outputs) != 1:
        raise BiRefNetForegroundMaskError("BiRefNet Tiny ONNX 没有返回预期的单通道蒙版。")
    logits = numpy.asarray(outputs[0])
    if logits.shape != (1, 1, _MODEL_SIZE, _MODEL_SIZE) or not numpy.isfinite(logits).all():
        raise BiRefNetForegroundMaskError("BiRefNet Tiny ONNX 返回的蒙版形状或数值无效。")
    # 官方推理在输出后做 sigmoid；先裁剪避免极端 logits 产生数值溢出。
    alpha = 1.0 / (1.0 + numpy.exp(-numpy.clip(logits[0, 0], -60.0, 60.0)))
    alpha_u8 = numpy.rint(alpha * 255.0).astype(numpy.uint8)
    mask = Image.fromarray(alpha_u8, mode="L")
    return mask.resize(source_size, Image.Resampling.BILINEAR)


def _validate_session_contract(session: Any) -> None:
    try:
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        input_item = inputs[0]
        output_item = outputs[0]
        input_shape = list(input_item.shape)
        output_shape = list(output_item.shape)
    except (AttributeError, IndexError, TypeError) as exc:
        raise BiRefNetForegroundMaskError("BiRefNet Tiny ONNX 会话缺少可校验的输入输出契约。") from exc
    if (
        len(inputs) != 1
        or len(outputs) != 1
        or input_item.name != _INPUT_NAME
        or output_item.name != _OUTPUT_NAME
        or getattr(input_item, "type", "") != "tensor(float)"
        or getattr(output_item, "type", "") != "tensor(float)"
        or input_shape != [1, 3, _MODEL_SIZE, _MODEL_SIZE]
        or output_shape != [1, 1, _MODEL_SIZE, _MODEL_SIZE]
    ):
        raise BiRefNetForegroundMaskError("BiRefNet Tiny ONNX 权重不符合已冻结的输入输出契约。")


def _load_numpy() -> Any:
    try:
        import numpy
    except ImportError as exc:  # pragma: no cover - 正式后端已通过间接依赖安装。
        raise BiRefNetForegroundMaskError("当前 Python 环境缺少 numpy，无法执行本地前景蒙版。") from exc
    return numpy


def _load_onnxruntime() -> Any:
    try:
        import onnxruntime
    except ImportError as exc:
        raise BiRefNetForegroundMaskError("当前 Python 环境缺少 onnxruntime，无法执行本地前景蒙版。") from exc
    return onnxruntime
