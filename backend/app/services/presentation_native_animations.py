"""为 AgentFlow 生成的 PPTX 写入克制、可回读的原生 PowerPoint 动效。

``python-pptx`` 目前没有公开的动画 API，但它保留了底层 PresentationML 树。这里把少量
受控 XML 收敛在一个模块中：交付层只声明“哪一页应有动效”，不需要了解 Timing 时间树细节。
所有动画都使用 Office 2007 起支持的 ``p:transition`` 和 ``p:animEffect``，不会把文本、
表格或图表栅格化，也不会引入自动翻页或不可编辑的视频替代物。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from xml.etree import ElementTree
from zipfile import ZipFile

from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Inches


_PRESENTATION_NAMESPACE = "http://schemas.openxmlformats.org/presentationml/2006/main"
_TIMING_TAG = qn("p:timing")
_TRANSITION_TAG = qn("p:transition")
_CLEAR_MAP_OVERRIDE_TAG = qn("p:clrMapOvr")
_COMMON_SLIDE_DATA_TAG = qn("p:cSld")
_EXTENSION_LIST_TAG = qn("p:extLst")
_MAX_ENTRANCE_EFFECTS_PER_SLIDE = 4


@dataclass(frozen=True)
class NativePresentationMotionSummary:
    """一次导出实际写入的原生动效摘要，供任务审计和回读验证复用。"""

    enabled: bool
    transition_slide_count: int
    entrance_slide_count: int
    entrance_effect_count: int
    warnings: tuple[str, ...] = ()

    def audit_metadata(self) -> dict[str, object]:
        """返回可安全写入 artifact 的摘要，不保存 XML、用户正文或绝对路径。"""

        return {
            "enabled": self.enabled,
            "transition": "fade" if self.enabled else "",
            "transition_slide_count": self.transition_slide_count,
            "entrance": "fade_on_click" if self.enabled else "",
            "entrance_slide_count": self.entrance_slide_count,
            "entrance_effect_count": self.entrance_effect_count,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class NativePresentationMotionInspection:
    """从已落盘 PPTX 回读的动效计数，不能只相信内存中的渲染意图。"""

    transition_slide_count: int
    entrance_slide_count: int
    entrance_effect_count: int
    invalid_target_count: int


def apply_native_presentation_motion(
    presentation: object,
    *,
    slide_roles: Sequence[str],
) -> NativePresentationMotionSummary:
    """给每页写入淡入转场，并让正文核心内容按点击顺序出现。

    封面和来源页只使用转场：首屏不能留给客户一页空白，来源页也更适合一次完整阅读。
    正文页最多四个核心对象，避免卡片、装饰线和页脚被拆成过多点击。任何 XML 注入异常都
    回退为无动效 PPTX；文件仍可编辑、可打开，不让视觉增强阻断实际交付。
    """

    slides = tuple(getattr(presentation, "slides", ()))
    if len(slides) != len(slide_roles):
        raise ValueError("PPT 动效页数与创作计划不一致。")

    try:
        entrance_slide_count = 0
        entrance_effect_count = 0
        for slide, role in zip(slides, slide_roles, strict=True):
            _replace_transition(slide)
            if role in {"cover", "sources"}:
                continue
            shape_ids = _entrance_shape_ids(slide)
            if not shape_ids:
                continue
            _replace_entrance_timing(slide, shape_ids)
            entrance_slide_count += 1
            entrance_effect_count += len(shape_ids)
        return NativePresentationMotionSummary(
            enabled=True,
            transition_slide_count=len(slides),
            entrance_slide_count=entrance_slide_count,
            entrance_effect_count=entrance_effect_count,
        )
    except Exception:
        # ``presentation`` 是本次新建且尚未保存的对象，清理不会影响客户历史文件。把半套
        # Timing 移除后再保存，避免 PowerPoint 打开时修复文件或把可交付 PPTX 变成假成功。
        for slide in slides:
            _clear_motion_elements(slide)
        return NativePresentationMotionSummary(
            enabled=False,
            transition_slide_count=0,
            entrance_slide_count=0,
            entrance_effect_count=0,
            warnings=("原生 PPT 动效写入未完成，已自动回退为无动画的可编辑文件。",),
        )


def inspect_native_presentation_motion(path: Path) -> NativePresentationMotionInspection:
    """从 ZIP 内的 slide XML 回读动效和目标对象，验证真实交付物而不是内存状态。"""

    namespace = {"p": _PRESENTATION_NAMESPACE}
    transition_slide_count = 0
    entrance_slide_count = 0
    entrance_effect_count = 0
    invalid_target_count = 0
    with ZipFile(path) as archive:
        slide_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
        for name in slide_names:
            root = ElementTree.fromstring(archive.read(name))
            if root.find("p:transition", namespace) is not None:
                transition_slide_count += 1
            shape_ids = {
                element.attrib.get("id", "")
                for element in root.findall(".//p:cNvPr", namespace)
            }
            effects = root.findall(".//p:animEffect[@transition='in']", namespace)
            if effects:
                entrance_slide_count += 1
            entrance_effect_count += len(effects)
            for effect in effects:
                target = effect.find(".//p:spTgt", namespace)
                if target is None or target.attrib.get("spid", "") not in shape_ids:
                    invalid_target_count += 1
    return NativePresentationMotionInspection(
        transition_slide_count=transition_slide_count,
        entrance_slide_count=entrance_slide_count,
        entrance_effect_count=entrance_effect_count,
        invalid_target_count=invalid_target_count,
    )


def _replace_transition(slide: object) -> None:
    """写入手动翻页的淡入转场，避免任何自动翻页扰乱客户讲解节奏。"""

    transition = parse_xml(
        f'<p:transition {nsdecls("p")} spd="med" advClick="1"><p:fade/></p:transition>'
    )
    _replace_slide_child(slide, _TRANSITION_TAG, transition, after_tags=(_COMMON_SLIDE_DATA_TAG, _CLEAR_MAP_OVERRIDE_TAG))


def _replace_entrance_timing(slide: object, shape_ids: tuple[int, ...]) -> None:
    """按 shape 的当前阅读顺序构造 on-click 淡入时间树。"""

    timing = parse_xml(_entrance_timing_xml(shape_ids))
    _replace_slide_child(slide, _TIMING_TAG, timing, after_tags=(_COMMON_SLIDE_DATA_TAG, _CLEAR_MAP_OVERRIDE_TAG, _TRANSITION_TAG))


def _replace_slide_child(slide: object, tag: str, child: object, *, after_tags: tuple[str, ...]) -> None:
    """保持 ``p:sld`` 子节点顺序，避免把 transition/timing 插到 ``extLst`` 之后。"""

    root = slide._element  # type: ignore[attr-defined]  # python-pptx 的稳定底层 slide XML 根节点。
    for existing in tuple(root):
        if existing.tag == tag:
            root.remove(existing)
    insertion_index = -1
    for index, existing in enumerate(root):
        if existing.tag in after_tags:
            insertion_index = index
        if existing.tag == _EXTENSION_LIST_TAG:
            break
    root.insert(insertion_index + 1, child)


def _clear_motion_elements(slide: object) -> None:
    """回退时只移除本模块负责的两个节点，不触碰页面正文、图表或关系部件。"""

    root = slide._element  # type: ignore[attr-defined]
    for existing in tuple(root):
        if existing.tag in {_TRANSITION_TAG, _TIMING_TAG}:
            root.remove(existing)


def _entrance_shape_ids(slide: object) -> tuple[int, ...]:
    """选择正文核心内容，主动排除标题、页脚和纯装饰形状。

    生成器没有为每个版式维护一份第二套动画清单，因而按几何位置和对象能力收敛：表格、
    原生 Chart、图片与正文文本优先；顶栏、页面标题、视觉方向和来源/页码永远稳定可见。
    这让新布局仍能自动获得合理的演示节奏，同时不会让每一根分割线占用一次点击。
    """

    candidates: list[tuple[int, int, int]] = []
    for order, shape in enumerate(getattr(slide, "shapes", ())):
        top = int(getattr(shape, "top", 0))
        height = int(getattr(shape, "height", 0))
        width = int(getattr(shape, "width", 0))
        if top < Inches(1.42) or top + height > Inches(6.25):
            continue
        if width < Inches(0.6) or height < Inches(0.2):
            continue
        text = str(getattr(shape, "text", "")).strip()
        if any(marker in text for marker in ("创作依据：", "事实边界：", "读取于", " / ")):
            continue
        # 不访问 ``shape.image``：非 Picture 的图形可能公开同名属性但在读取时抛出 ValueError，
        # 这类实现细节不能让一个普通卡片触发整份 PPT 的无动画降级。
        is_data_or_image = any(
            bool(getattr(shape, attribute, False))
            for attribute in ("has_table", "has_chart")
        ) or getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE
        if not is_data_or_image and not text:
            continue
        candidates.append((top, order, int(getattr(shape, "shape_id", 0))))
    candidates.sort()
    return tuple(
        shape_id
        for _, _, shape_id in candidates[:_MAX_ENTRANCE_EFFECTS_PER_SLIDE]
        if shape_id > 0
    )


def _entrance_timing_xml(shape_ids: tuple[int, ...]) -> str:
    """构造 Office 兼容的 root -> main sequence -> click effect 时间线。

    每个节点由独立 id 标识；``animEffect`` 的 ``transition=in`` 会让目标形状在其点击到来
    前保持隐藏，随后以 350ms fade 出现。``next/prev`` 条件交给幻灯片本身，保证所有内容
    出现后下一次点击仍按正常 PowerPoint 语义切换页面。
    """

    effect_xml: list[str] = []
    next_id = 3
    for shape_id in shape_ids:
        click_id, visible_id, effect_id = next_id, next_id + 1, next_id + 2
        next_id += 3
        effect_xml.append(
            f"""
            <p:par>
              <p:cTn id=\"{click_id}\" fill=\"hold\" nodeType=\"clickEffect\">
                <p:stCondLst><p:cond delay=\"indefinite\"/></p:stCondLst>
                <p:childTnLst>
                  <p:set>
                    <p:cBhvr override=\"childStyle\">
                      <p:cTn id=\"{visible_id}\" dur=\"1\" fill=\"hold\"/>
                      <p:tgtEl><p:spTgt spid=\"{shape_id}\"/></p:tgtEl>
                      <p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst>
                    </p:cBhvr>
                    <p:to><p:strVal val=\"visible\"/></p:to>
                  </p:set>
                  <p:animEffect transition=\"in\" filter=\"fade\">
                    <p:cBhvr override=\"childStyle\">
                      <p:cTn id=\"{effect_id}\" dur=\"350\" fill=\"hold\"/>
                      <p:tgtEl><p:spTgt spid=\"{shape_id}\"/></p:tgtEl>
                    </p:cBhvr>
                  </p:animEffect>
                </p:childTnLst>
              </p:cTn>
            </p:par>
            """
        )
    return f"""
    <p:timing {nsdecls('p')}>
      <p:tnLst>
        <p:par>
          <p:cTn id=\"1\" dur=\"indefinite\" restart=\"never\" nodeType=\"tmRoot\">
            <p:childTnLst>
              <p:seq concurrent=\"1\" nextAc=\"seek\">
                <p:cTn id=\"2\" dur=\"indefinite\" nodeType=\"mainSeq\">
                  <p:childTnLst>{''.join(effect_xml)}</p:childTnLst>
                </p:cTn>
                <p:prevCondLst>
                  <p:cond evt=\"onPrev\" delay=\"0\"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond>
                </p:prevCondLst>
                <p:nextCondLst>
                  <p:cond evt=\"onNext\" delay=\"0\"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond>
                </p:nextCondLst>
              </p:seq>
            </p:childTnLst>
          </p:cTn>
        </p:par>
      </p:tnLst>
    </p:timing>
    """
