"""局部生成编辑的确定性蒙版合成与保护区校验。

云端图片模型不提供可验证的像素保护承诺时，不能把它的整张返回图直接登记为局部编辑结果。
本模块只允许在已绑定当前源图 revision 的蒙版内取用候选图颜色，蒙版外的原图像素保持逐点一致。
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageOps


_MAX_SOURCE_PIXELS = 40_000_000


class MediaImageCompositionError(RuntimeError):
    """局部编辑候选无法安全合成时的稳定错误。"""


@dataclass(frozen=True)
class LocalGeneratedEditComposition:
    """可进入后续 revision 提交前校验的内存结果。"""

    image: Image.Image
    source_size: tuple[int, int]
    editable_pixel_count: int
    protected_pixel_count: int


@dataclass(frozen=True)
class ProtectedRegionCheck:
    """仅统计 alpha 为零的硬保护区；羽化带属于预先声明的有效蒙版。"""

    protected_pixel_count: int
    changed_pixel_count: int
    max_channel_delta: int

    @property
    def passed(self) -> bool:
        return self.changed_pixel_count == 0 and self.max_channel_delta == 0


def compose_local_generated_edit(
    *,
    source_image: Image.Image,
    generated_image: Image.Image,
    edit_mask: Image.Image,
) -> LocalGeneratedEditComposition:
    """把模型候选严格限制到既定蒙版，返回与原图同尺寸的 RGBA 图像。

    该函数没有网络、文件写入或路径输入。调用方必须在更上层把 ``edit_mask`` 绑定到 source
    revision，并在原子提交 artifact 前再次确认 revision 没有变化。
    """

    source = _prepare_color_image(source_image, field_name="源图")
    generated = _prepare_color_image(generated_image, field_name="生成候选图")
    mask = _prepare_mask(edit_mask)
    if generated.size != source.size or mask.size != source.size:
        raise MediaImageCompositionError("局部编辑的源图、候选图和蒙版尺寸必须完全一致。")

    source_rgb = source.convert("RGB")
    generated_rgb = generated.convert("RGB")
    # Pillow 在 alpha=0 时直接取 source 像素；后续检查仍作为回归保护，避免实现改动破坏约束。
    composed_rgb = Image.composite(generated_rgb, source_rgb, mask)
    result = composed_rgb.convert("RGBA")
    result.putalpha(source.getchannel("A"))
    protected_check = verify_hard_protected_pixels(
        source_image=source,
        result_image=result,
        edit_mask=mask,
    )
    if not protected_check.passed:  # pragma: no cover - Pillow 行为变化时的防御性失败。
        raise MediaImageCompositionError("局部编辑结果改变了既定蒙版之外的受保护像素。")
    histogram = mask.histogram()
    protected_pixels = histogram[0]
    editable_pixels = sum(histogram[1:])
    return LocalGeneratedEditComposition(
        image=result,
        source_size=source.size,
        editable_pixel_count=editable_pixels,
        protected_pixel_count=protected_pixels,
    )


def verify_hard_protected_pixels(
    *,
    source_image: Image.Image,
    result_image: Image.Image,
    edit_mask: Image.Image,
) -> ProtectedRegionCheck:
    """验证硬保护区逐点不变；只用于无损、未发生全局几何变换的同尺寸图像。"""

    source = _prepare_color_image(source_image, field_name="源图")
    result = _prepare_color_image(result_image, field_name="结果图")
    mask = _prepare_mask(edit_mask)
    if result.size != source.size or mask.size != source.size:
        raise MediaImageCompositionError("保护区校验要求源图、结果图和蒙版尺寸完全一致。")
    # 仅在 mask=0 处比较。羽化带已经是用户批准的有效编辑范围的一部分，不能事后当作越界。
    protected_binary = mask.point(lambda value: 255 if value == 0 else 0, mode="L")
    protected_pixels = sum(1 for value in protected_binary.getdata() if value)
    if not protected_pixels:
        return ProtectedRegionCheck(protected_pixel_count=0, changed_pixel_count=0, max_channel_delta=0)
    difference = ImageChops.difference(source, result)
    difference.putalpha(protected_binary)
    changed_pixel_count = 0
    max_channel_delta = 0
    for red, green, blue, alpha in difference.getdata():
        if not alpha:
            continue
        channel_delta = max(red, green, blue)
        if channel_delta:
            changed_pixel_count += 1
            max_channel_delta = max(max_channel_delta, channel_delta)
    return ProtectedRegionCheck(
        protected_pixel_count=protected_pixels,
        changed_pixel_count=changed_pixel_count,
        max_channel_delta=max_channel_delta,
    )


def _prepare_color_image(image: Image.Image, *, field_name: str) -> Image.Image:
    if not isinstance(image, Image.Image):
        raise MediaImageCompositionError(f"{field_name}必须是已解码的图片对象。")
    normalized = ImageOps.exif_transpose(image)
    width, height = normalized.size
    if width < 1 or height < 1 or width * height > _MAX_SOURCE_PIXELS:
        raise MediaImageCompositionError(f"{field_name}尺寸无效或超过 4000 万像素上限。")
    return normalized.convert("RGBA")


def _prepare_mask(mask: Image.Image) -> Image.Image:
    if not isinstance(mask, Image.Image):
        raise MediaImageCompositionError("编辑蒙版必须是已解码的图片对象。")
    normalized = ImageOps.exif_transpose(mask)
    width, height = normalized.size
    if width < 1 or height < 1 or width * height > _MAX_SOURCE_PIXELS:
        raise MediaImageCompositionError("编辑蒙版尺寸无效或超过 4000 万像素上限。")
    return normalized.convert("L")
