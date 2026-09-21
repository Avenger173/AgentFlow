"""局部生成编辑的蒙版合成与硬保护区离线回归。"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.media_image_composition import (  # noqa: E402
    MediaImageCompositionError,
    compose_local_generated_edit,
    verify_hard_protected_pixels,
)


def main() -> None:
    source = Image.new("RGB", (48, 32), color=(22, 75, 150))
    generated = Image.new("RGB", source.size, color=(228, 68, 55))
    mask = Image.new("L", source.size, color=0)
    ImageDraw.Draw(mask).rectangle((12, 8, 35, 23), fill=255)

    composed = compose_local_generated_edit(
        source_image=source,
        generated_image=generated,
        edit_mask=mask,
    )
    assert composed.image.mode == "RGBA"
    assert composed.image.getpixel((4, 4))[:3] == (22, 75, 150)
    assert composed.image.getpixel((20, 12))[:3] == (228, 68, 55)
    assert composed.protected_pixel_count > 0
    assert composed.editable_pixel_count > 0
    check = verify_hard_protected_pixels(
        source_image=source,
        result_image=composed.image,
        edit_mask=mask,
    )
    assert check.passed
    assert check.protected_pixel_count == composed.protected_pixel_count

    feathered = mask.filter(ImageFilter.GaussianBlur(radius=3))
    feathered_composed = compose_local_generated_edit(
        source_image=source,
        generated_image=generated,
        edit_mask=feathered,
    )
    feathered_check = verify_hard_protected_pixels(
        source_image=source,
        result_image=feathered_composed.image,
        edit_mask=feathered,
    )
    assert feathered_check.passed
    assert feathered_check.protected_pixel_count < composed.protected_pixel_count

    tampered = composed.image.copy()
    tampered.putpixel((4, 4), (1, 2, 3, 255))
    tampered_check = verify_hard_protected_pixels(
        source_image=source,
        result_image=tampered,
        edit_mask=mask,
    )
    assert not tampered_check.passed
    assert tampered_check.changed_pixel_count == 1

    try:
        compose_local_generated_edit(
            source_image=source,
            generated_image=Image.new("RGB", (47, 32)),
            edit_mask=mask,
        )
    except MediaImageCompositionError as exc:
        assert "尺寸" in str(exc)
    else:  # pragma: no cover - 防止不同尺寸被悄悄缩放。
        raise AssertionError("mismatched local-edit images must be rejected")
    print("media image composition contracts passed")


if __name__ == "__main__":
    main()
