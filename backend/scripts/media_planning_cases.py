"""MM-0 冻结的图片编辑意图集合，不含客户文件或真实图片。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MediaPlanningCase:
    case_id: str
    message: str
    expected_scope: str
    required_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...] = ()


MEDIA_PLANNING_CASES: tuple[MediaPlanningCase, ...] = (
    MediaPlanningCase("MP-01", "这张室内人像有点暗，整体提亮一点，皮肤别过曝。", "global", ("image.adjust", "image.export"), ("image.select_region", "image.edit_region")),
    MediaPlanningCase("MP-02", "把这张照片裁成 4:5 竖版，主体留在中间。", "global", ("image.adjust", "image.export"), ("image.select_region", "image.edit_region")),
    MediaPlanningCase("MP-03", "只把衣服胸口的红色 logo 改成深蓝色，别动衣服其他地方。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-04", "把左下角拍摄日期水印去掉，边上的景物不要变。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-05", "把产品后面的背景换成干净的纯白色，产品本身保留。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-06", "把桌面上乱七八糟的线缆清掉，电脑和杯子不要动。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-07", "给照片里的这个人加一顶黑色棒球帽。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-08", "人物别改，把人物身后的街景换成会议室。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-09", "只虚化背景，让前面的人保持清晰。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-10", "这张图整体调暖一点，对比度轻微加一点。", "global", ("image.adjust", "image.export"), ("image.select_region", "image.edit_region")),
    MediaPlanningCase("MP-11", "把背景里经过的路人删掉，前面拿产品的人不要改。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-12", "海报上“限时特惠”写错了，改成“限时优惠”，其他字不要变。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-13", "只把照片里的天空替换成傍晚日落，建筑物保持原样。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-14", "把人物眼镜上的反光修掉，脸部和五官不要动。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-15", "把照片顺时针转正，再导出一份高清 JPG。", "global", ("image.adjust", "image.export"), ("image.select_region", "image.edit_region")),
    MediaPlanningCase("MP-16", "把画面里的产品抠出来，做成透明背景的 PNG。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-17", "把整张图的饱和度降一点，看起来更自然。", "global", ("image.adjust", "image.export"), ("image.select_region", "image.edit_region")),
    MediaPlanningCase("MP-18", "帮我把这张照片修得高级一点。", "clarify", ("clarify",)),
    MediaPlanningCase("MP-19", "把图里的姓名、手机号和金额都打码，其他内容保留。", "local", ("image.select_region", "image.edit_region", "image.export")),
    MediaPlanningCase("MP-20", "把右上角的污点修掉，不要影响旁边的天空。", "local", ("image.select_region", "image.edit_region", "image.export")),
)
