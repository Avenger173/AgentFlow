"""BiRefNet 前景蒙版适配器的离线契约回归。"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.birefnet_foreground_mask import (  # noqa: E402
    BiRefNetForegroundMaskError,
    create_birefnet_foreground_alpha_mask,
    inspect_birefnet_foreground_mask_readiness,
)


@dataclass(frozen=True)
class _Node:
    name: str
    shape: list[int]
    type: str = "tensor(float)"


class _FakeSession:
    def __init__(self) -> None:
        self.received_shape: tuple[int, ...] | None = None

    @staticmethod
    def get_inputs() -> list[_Node]:
        return [_Node("input_image", [1, 3, 1024, 1024])]

    @staticmethod
    def get_outputs() -> list[_Node]:
        return [_Node("output_image", [1, 1, 1024, 1024])]

    def run(self, output_names: list[str], values: dict[str, object]) -> list[object]:
        import numpy

        assert output_names == ["output_image"]
        tensor = values["input_image"]
        self.received_shape = tuple(tensor.shape)  # type: ignore[union-attr]
        logits = numpy.full((1, 1, 1024, 1024), -12.0, dtype=numpy.float32)
        logits[:, :, 256:768, 256:768] = 12.0
        return [logits]


def main() -> None:
    missing = inspect_birefnet_foreground_mask_readiness(
        model_path=BACKEND_ROOT / "data" / "not-present.onnx"
    )
    assert missing.ready is False
    assert "未找到" in missing.reason

    session = _FakeSession()
    source = Image.new("RGB", (320, 180), color=(120, 120, 120))
    result = create_birefnet_foreground_alpha_mask(source, session=session)
    assert result.source_size == (320, 180)
    assert result.alpha_mask.mode == "L"
    assert result.alpha_mask.size == (320, 180)
    assert session.received_shape == (1, 3, 1024, 1024)
    assert result.alpha_mask.getpixel((160, 90)) > 250
    assert result.alpha_mask.getpixel((5, 5)) < 5

    try:
        create_birefnet_foreground_alpha_mask("not-an-image", session=session)  # type: ignore[arg-type]
    except BiRefNetForegroundMaskError as exc:
        assert "图片对象" in str(exc)
    else:  # pragma: no cover - 防止失败时静默通过。
        raise AssertionError("invalid source must be rejected")
    print("birefnet foreground-mask contracts passed")


if __name__ == "__main__":
    main()
