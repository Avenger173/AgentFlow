"""多媒体工作区的受控项目、图片素材和修订版本 API 契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class MediaProjectCreateRequest(BaseModel):
    """创建一个只存放项目内副本的图片工作区。"""

    title: str = Field(default="未命名图片项目", min_length=1, max_length=80)


class MediaProjectInfo(BaseModel):
    project_id: str = Field(pattern=r"^mp_[0-9a-f]{16}$")
    title: str
    created_at: str
    updated_at: str
    asset_count: int = Field(ge=0)


class MediaProjectListResponse(BaseModel):
    total: int = Field(ge=0)
    projects: list[MediaProjectInfo] = Field(default_factory=list)


class MediaImageImportRequest(BaseModel):
    """客户端仅提交图片名称和 Base64 字节，不能提交本机文件路径。"""

    filename: str = Field(min_length=1, max_length=180)
    content_base64: str = Field(min_length=4, max_length=28_000_000)


class MediaAssetInfo(BaseModel):
    asset_id: str = Field(pattern=r"^ma_[0-9a-f]{16}$")
    name: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    size_bytes: int = Field(ge=1)
    created_at: str
    current_revision_id: str = Field(pattern=r"^mr_[0-9a-f]{16}$")
    revision_count: int = Field(ge=1)
    undo_available: bool = False
    redo_available: bool = False


class MediaImageRevisionInfo(BaseModel):
    revision_id: str = Field(pattern=r"^mr_[0-9a-f]{16}$")
    asset_id: str = Field(pattern=r"^ma_[0-9a-f]{16}$")
    parent_revision_id: str | None = Field(default=None, pattern=r"^mr_[0-9a-f]{16}$")
    operation: Literal[
        "import",
        "rotate_left",
        "rotate_right",
        "flip_horizontal",
        "grayscale",
        "adjust_color",
        "crop",
        "resize",
        "apply_rect_mask",
        "composite_raster_layer",
        "recompose_raster_layers",
    ]
    parameters: dict[str, object] = Field(default_factory=dict)
    mask_id: str | None = Field(default=None, pattern=r"^mm_[0-9a-f]{16}$")
    layer_id: str | None = Field(default=None, pattern=r"^ml_[0-9a-f]{16}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: Literal["image/png"] = "image/png"
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    size_bytes: int = Field(ge=1)
    created_at: str


class MediaProjectDetailResponse(BaseModel):
    project: MediaProjectInfo
    assets: list[MediaAssetInfo] = Field(default_factory=list)


class MediaAssetRevisionListResponse(BaseModel):
    asset: MediaAssetInfo
    revisions: list[MediaImageRevisionInfo] = Field(default_factory=list)


class MediaLayerStackStateRequest(BaseModel):
    """A single immutable composition-state entry, ordered from bottom to top."""

    layer_id: str = Field(pattern=r"^ml_[0-9a-f]{16}$")
    visible: bool = True


class MediaLayerStackItem(BaseModel):
    """A renderable raster layer resolved from a revision's stored stack snapshot."""

    layer_id: str = Field(pattern=r"^ml_[0-9a-f]{16}$")
    source_asset_id: str = Field(pattern=r"^ma_[0-9a-f]{16}$")
    source_name: str
    visible: bool
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    opacity: int = Field(ge=1, le=100)


class MediaLayerStackResponse(BaseModel):
    asset_id: str = Field(pattern=r"^ma_[0-9a-f]{16}$")
    revision_id: str = Field(pattern=r"^mr_[0-9a-f]{16}$")
    composition_root_revision_id: str | None = Field(default=None, pattern=r"^mr_[0-9a-f]{16}$")
    editable: bool = False
    layers: list[MediaLayerStackItem] = Field(default_factory=list)


class MediaImageOperationRequest(BaseModel):
    """首版仅暴露确定性、本地执行且可回滚的白名单编辑。"""

    operation: Literal[
        "rotate_left",
        "rotate_right",
        "flip_horizontal",
        "grayscale",
        "adjust_color",
        "crop",
        "resize",
        "apply_rect_mask",
        "composite_raster_layer",
        "recompose_raster_layers",
    ]
    base_revision_id: str = Field(
        pattern=r"^mr_[0-9a-f]{16}$",
        description="The current revision the caller inspected before creating this edit.",
    )
    brightness: int = Field(default=0, ge=-100, le=100)
    contrast: int = Field(default=0, ge=-100, le=100)
    saturation: int = Field(default=0, ge=-100, le=100)
    crop_x: int | None = Field(default=None, ge=0)
    crop_y: int | None = Field(default=None, ge=0)
    crop_width: int | None = Field(default=None, ge=1, le=10_000)
    crop_height: int | None = Field(default=None, ge=1, le=10_000)
    resize_width: int | None = Field(default=None, ge=1, le=10_000)
    resize_height: int | None = Field(default=None, ge=1, le=10_000)
    mask_x: int | None = Field(default=None, ge=0)
    mask_y: int | None = Field(default=None, ge=0)
    mask_width: int | None = Field(default=None, ge=1, le=10_000)
    mask_height: int | None = Field(default=None, ge=1, le=10_000)
    overlay_asset_id: str | None = Field(default=None, pattern=r"^ma_[0-9a-f]{16}$")
    layer_x: int | None = Field(default=None, ge=0)
    layer_y: int | None = Field(default=None, ge=0)
    layer_opacity: int | None = Field(default=None, ge=1, le=100)
    layer_stack: list[MediaLayerStackStateRequest] | None = None

    @model_validator(mode="after")
    def validate_operation_parameters(self) -> "MediaImageOperationRequest":
        color_values = (self.brightness, self.contrast, self.saturation)
        crop_values = (self.crop_x, self.crop_y, self.crop_width, self.crop_height)
        resize_values = (self.resize_width, self.resize_height)
        mask_values = (self.mask_x, self.mask_y, self.mask_width, self.mask_height)
        layer_values = (self.layer_x, self.layer_y, self.layer_opacity)

        if self.operation != "recompose_raster_layers" and self.layer_stack is not None:
            raise ValueError("只有图层重组操作可以提交图层栈状态。")
        if self.operation == "recompose_raster_layers":
            if self.overlay_asset_id is not None or any(value != 0 for value in color_values) or any(
                value is not None for value in (*crop_values, *resize_values, *mask_values, *layer_values)
            ):
                raise ValueError("图层重组请求只能包含图层栈状态。")
            if not self.layer_stack:
                raise ValueError("图层重组至少需要保留一个图层。")
            layer_ids = [item.layer_id for item in self.layer_stack]
            if len(layer_ids) != len(set(layer_ids)):
                raise ValueError("图层重组不能重复引用同一图层。")
            return self

        if self.operation == "adjust_color":
            if self.overlay_asset_id is not None or any(
                value is not None for value in (*crop_values, *resize_values, *mask_values, *layer_values)
            ):
                raise ValueError("色彩调整不能同时携带裁剪或缩放参数。")
            if not any(color_values):
                raise ValueError("请至少调整一个色彩参数。")
            return self
        if self.operation == "crop":
            if self.overlay_asset_id is not None or any(
                value != 0 for value in color_values
            ) or any(value is not None for value in (*resize_values, *mask_values, *layer_values)):
                raise ValueError("裁剪请求只能包含裁剪参数。")
            if any(value is None for value in crop_values):
                raise ValueError("裁剪需要 x、y、宽度和高度。")
            return self
        if self.operation == "resize":
            if self.overlay_asset_id is not None or any(
                value != 0 for value in color_values
            ) or any(value is not None for value in (*crop_values, *mask_values, *layer_values)):
                raise ValueError("缩放请求只能包含缩放参数。")
            if any(value is None for value in resize_values):
                raise ValueError("缩放需要目标宽度和高度。")
            return self
        if self.operation == "apply_rect_mask":
            if self.overlay_asset_id is not None or any(
                value != 0 for value in color_values
            ) or any(value is not None for value in (*crop_values, *resize_values, *layer_values)):
                raise ValueError("矩形蒙版请求只能包含蒙版参数。")
            if any(value is None for value in mask_values):
                raise ValueError("矩形蒙版需要 x、y、宽度和高度。")
            return self
        if self.operation == "composite_raster_layer":
            if any(value != 0 for value in color_values) or any(
                value is not None for value in (*crop_values, *resize_values, *mask_values)
            ):
                raise ValueError("栅格图层请求只能包含图层参数。")
            if self.overlay_asset_id is None or any(value is None for value in layer_values):
                raise ValueError("栅格图层需要素材、x、y 和不透明度。")
            return self
        if self.overlay_asset_id is not None or any(value != 0 for value in color_values) or any(
            value is not None for value in (*crop_values, *resize_values, *mask_values, *layer_values)
        ):
            raise ValueError("该编辑操作不接受额外参数。")
        return self

    def operation_parameters(self) -> dict[str, int]:
        if self.operation == "adjust_color":
            return {
                "brightness": self.brightness,
                "contrast": self.contrast,
                "saturation": self.saturation,
            }
        if self.operation == "crop":
            return {
                "x": int(self.crop_x),
                "y": int(self.crop_y),
                "width": int(self.crop_width),
                "height": int(self.crop_height),
            }
        if self.operation == "resize":
            return {
                "width": int(self.resize_width),
                "height": int(self.resize_height),
            }
        if self.operation == "apply_rect_mask":
            return {
                "x": int(self.mask_x),
                "y": int(self.mask_y),
                "width": int(self.mask_width),
                "height": int(self.mask_height),
            }
        if self.operation == "composite_raster_layer":
            return {
                "x": int(self.layer_x),
                "y": int(self.layer_y),
                "opacity": int(self.layer_opacity),
            }
        if self.operation == "recompose_raster_layers":
            return {"layer_stack": [item.model_dump() for item in self.layer_stack or []]}
        return {}

    def layer_source_asset_id(self) -> str | None:
        return self.overlay_asset_id if self.operation == "composite_raster_layer" else None


class MediaImageEditTaskStartResponse(BaseModel):
    """确定性图片编辑的异步受理回执。"""

    task_id: str = Field(pattern=r"^task_media_edit_[0-9a-f]{12}$")
    status: Literal["queued"] = "queued"


class MediaImageEditTaskResultResponse(BaseModel):
    """从统一 Runtime 历史读取图片编辑终态。"""

    task_id: str = Field(pattern=r"^task_media_edit_[0-9a-f]{12}$")
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    summary: str
    message: str
    conflict: bool = False
    revision: MediaImageRevisionInfo | None = None


class MediaHistoryNavigationRequest(BaseModel):
    """Bind undo or redo to the revision visible when the user initiated it."""

    base_revision_id: str = Field(pattern=r"^mr_[0-9a-f]{16}$")


class MediaImageExportRequest(BaseModel):
    """导出始终创建一个新 PNG 文件，文件名不可以包含路径。"""

    filename: str = Field(default="edited-image.png", min_length=1, max_length=180)


class MediaImageExportInfo(BaseModel):
    export_id: str = Field(pattern=r"^me_[0-9a-f]{16}$")
    project_id: str = Field(pattern=r"^mp_[0-9a-f]{16}$")
    asset_id: str = Field(pattern=r"^ma_[0-9a-f]{16}$")
    revision_id: str = Field(pattern=r"^mr_[0-9a-f]{16}$")
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: Literal["image/png"] = "image/png"
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    size_bytes: int = Field(ge=1)
    created_at: str


class MediaImageExportTaskStartResponse(BaseModel):
    """图片导出任务的受理回执。

    图片版本在受控工作区内已经不可变；这里的异步任务只会将客户选择的版本复制为新的
    PNG 交付物，并把验证结果登记进统一任务历史。
    """

    task_id: str = Field(pattern=r"^task_media_export_[0-9a-f]{12}$")
    status: Literal["queued"] = "queued"


class MediaImageExportTaskResultResponse(BaseModel):
    """从统一任务历史恢复的图片导出终态。"""

    task_id: str = Field(pattern=r"^task_media_export_[0-9a-f]{12}$")
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    summary: str
    message: str
    export: MediaImageExportInfo | None = None
