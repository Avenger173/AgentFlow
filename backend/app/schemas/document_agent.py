"""文档助手的稳定输入、输出与模型中间协议。

这里把模型生成的 ``source_ids`` 和最终展示给用户的 ``source_refs`` 分开：模型只能从
Runtime 已提供的来源 ID 中选择，后端再把它映射为文件名、行号和片段。这样可以减少模型
伪造文件路径或引用行号的机会，也让 Qt、任务历史和后续 Code/Report 可以复用同一结构。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.workflow import WorkflowRun


# ``cross_qa``、``synthesis`` 和 ``comparison`` 都读取多份材料，但交互目标不同：前者回答
# 用户问题，中间模式整合可兼容内容，后者主动整理共识与差异。分开建模可避免把所有跨文档
# 诉求都误导成“对比报告”或直接创作文件。
DocumentOutputMode = Literal[
    "auto",
    "requirements",
    "summary",
    "brief",
    "outline",
    "draft",
    "section_draft",
    "draft_review",
    "section_review",
    "section_revision",
    "section_revision_batch",
    "section_manual_revision",
    "draft_restore",
    "draft_template",
    "draft_merge",
    "qa",
    "cross_qa",
    "synthesis",
    "comparison",
]
DocumentRequirementCategory = Literal[
    "functional",
    "output",
    "constraint",
    "acceptance",
    "unknown",
]
DocumentPriority = Literal["must", "should", "could", "unknown"]
DocumentConfidence = Literal["low", "medium", "high"]
DocumentComparisonKind = Literal["common", "difference", "missing", "uncertain"]
DocumentRevisionSeverity = Literal["important", "suggestion"]
DocumentRevisionCategory = Literal["accuracy", "clarity", "consistency", "structure", "style"]
DocumentDraftVersionKind = Literal[
    "base_draft",
    "section_preview",
    "fact_review",
    "section_review",
    "revision_preview",
    "revision_batch_preview",
    "manual_revision_pending_review",
    "restored_preview",
    "template_preview",
    "merge_preview",
]
DocumentDraftVerificationState = Literal["verified", "requires_review", "reviewed_with_questions"]
DocumentTemplateId = Literal["project_proposal", "product_requirements", "meeting_minutes"]
DocumentDraftMergeConflictKind = Literal["title", "content", "deletion", "addition"]
DocumentDraftMergeResolutionChoice = Literal["primary", "secondary", "base"]
# “关键信息卡”先提供一个稳定、通用的项目材料模板。字段 key 是跨 UI、历史和后续
# Commander 可复用的机器协议；中文显示名由客户端负责，不让模型自行发明字段语义。
DocumentBriefFieldKey = Literal[
    "subject",
    "purpose",
    "scope",
    "stakeholders",
    "deliverables",
    "milestones",
    "risks",
]
DocumentAgentStatus = Literal[
    "completed",
    "needs_clarification",
    "insufficient_context",
    "failed",
    "max_turns_exceeded",
    "budget_exhausted",
]


class DocumentDraftSectionSeed(BaseModel):
    """派生章节预览所需的受控种子。

    此模型只在服务层从一个已完成草稿任务中构造，避免 Qt 把富文本或任意本机内容直接当作
    新的事实输入。模型仍必须重新读取 workspace 材料，并用本轮 Runtime 分配的来源引用。
    """

    source_task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    section_id: str = Field(min_length=1, max_length=80)
    heading: str = Field(min_length=1, max_length=160)
    current_body: str = Field(min_length=1, max_length=1_500)
    instruction: str = Field(min_length=1, max_length=1_200)


class DocumentDraftTemplateSeed(BaseModel):
    """从已核验草稿建立模板化交付预览时使用的受控身份。

    模板只负责重新组织已验证章节，不允许客户端借“套模板”传入正文、文件路径或自由模板
    定义。这样第一版能提供正式文档结构，又不会把模板入口变成未审计的自由编辑器。
    """

    source_task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    source_version_id: str = Field(min_length=1, max_length=120)
    template_id: DocumentTemplateId


class DocumentDraftMergeResolution(BaseModel):
    """用户对一个三方合并冲突做出的显式选择。

    ``conflict_id`` 只能来自后端预先计算的合并计划；服务层会在真正创建预览前重新计算计划并
    校验集合完整性。这样客户端不能通过伪造章节正文或未知冲突 ID 绕过版本边界。
    """

    model_config = ConfigDict(extra="forbid")

    conflict_id: str = Field(min_length=1, max_length=120)
    choice: DocumentDraftMergeResolutionChoice


class DocumentDraftMergeSeed(BaseModel):
    """建立章节合并预览时的受控版本身份与用户已确认的冲突选择。"""

    primary_task_id: str = Field(min_length=1, max_length=120)
    secondary_task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    primary_version_id: str = Field(min_length=1, max_length=120)
    secondary_version_id: str = Field(min_length=1, max_length=120)
    resolutions: list[DocumentDraftMergeResolution] = Field(default_factory=list, max_length=9)


class DocumentAgentRunRequest(BaseModel):
    """用户从文档助手页发起的一次只读分析请求。"""

    task_goal: str = Field(min_length=1, max_length=2_000)
    document_refs: list[str] = Field(default_factory=list, max_length=4)
    query: str = Field(default="", max_length=200)
    output_mode: DocumentOutputMode = "auto"
    constraints: list[str] = Field(default_factory=list, max_length=12)
    # 仅 ``section_draft`` 使用。正式入口会先校验来源任务与章节 ID，再由服务端填入该种子。
    section_draft: DocumentDraftSectionSeed | None = None
    # 仅 ``draft_review`` 使用。Runtime 从已完成草稿恢复该快照，禁止客户端提交待核验正文。
    draft_review: DocumentDraftReviewSeed | None = None
    # 仅 ``section_review`` 使用。Runtime 从已完成草稿恢复章节及完整草稿快照，禁止客户端
    # 通过普通 /run 注入待审校正文或其他文件范围。
    section_review: DocumentDraftSectionReviewSeed | None = None
    # 仅 ``section_revision`` 使用。它不是自由改写入口，而是从已完成的本章审校任务中恢复
    # 稳定 suggestion_id，再生成可对比、可另存的独立版本预览。
    section_revision: DocumentDraftSectionRevisionSeed | None = None
    # 仅 ``section_revision_batch`` 使用。它和单条预览一样只传稳定身份，但会在服务端检查
    # 每个原文片段唯一且区间互不重叠，拒绝任何依赖“猜测应用顺序”的批量修改。
    section_revision_batch: DocumentDraftSectionBatchRevisionSeed | None = None
    # 仅 ``draft_restore`` 使用。客户端只能指定历史任务身份；正文和来源仍由服务端从已完成
    # 的 SQLite 快照恢复，不能把任意文本伪装成“旧版本”。
    draft_restore: DocumentDraftRestoreSeed | None = None
    # 仅 ``section_manual_revision`` 使用。客户端提交的正文必须由服务端重新绑定到同一份
    # 已完成草稿快照；它会产生待核验预览，不能直接保存或伪装成模型/材料已验证的结论。
    section_manual_revision: DocumentDraftSectionManualRevisionSeed | None = None
    # 仅 ``draft_template`` 使用。模板交付只允许引用已验证草稿快照，正文与来源均由服务端
    # 恢复；该过程不会调用模型、Tool 或文件写入。
    draft_template: DocumentDraftTemplateSeed | None = None
    # 仅 ``draft_merge`` 使用。合并只允许同一根草稿下的已核验完整快照；正文、来源、共同
    # 祖先和冲突结果都由服务端重新恢复与计算，客户端只能提交已知冲突的选择。
    draft_merge: DocumentDraftMergeSeed | None = None


class DocumentSourceRef(BaseModel):
    """一个经过 Runtime 校验的可展示引用。"""

    source_id: str
    relative_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    # start/end_line 为已有历史与 Qt 客户端保留；source_locator 才是 PDF/DOCX 面向用户的
    # 真实定位文字，例如“第 2 页”或“第 5-7 段”。
    source_kind: Literal["line", "page", "paragraph", "table", "mixed"] = "line"
    source_locator: str = Field(default="", max_length=120)
    excerpt: str = Field(default="", max_length=360)


class DocumentFinding(BaseModel):
    """面向用户的非需求类结论，例如待办、实体或待确认问题。"""

    text: str = Field(min_length=1, max_length=1_200)
    source_refs: list[DocumentSourceRef] = Field(default_factory=list)
    confidence: DocumentConfidence = "medium"


class DocumentRequirement(BaseModel):
    """文档助手固定输出的需求条目。

    分类和优先级使用稳定枚举，UI 可以显示中文标签，后续 Code/Report 也能按机器可读字段
    做筛选。``unknown`` 对应“待确认/尚未判断”，避免强迫模型虚构优先级。
    """

    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=1_200)
    category: DocumentRequirementCategory = "unknown"
    priority: DocumentPriority = "unknown"
    source_refs: list[DocumentSourceRef] = Field(default_factory=list)
    confidence: DocumentConfidence = "medium"


class DocumentComparison(BaseModel):
    """一项跨文档结论，必须能同时回溯到至少两份材料。"""

    dimension: str = Field(min_length=1, max_length=120)
    kind: DocumentComparisonKind
    summary: str = Field(min_length=1, max_length=1_200)
    source_refs: list[DocumentSourceRef] = Field(min_length=2, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentBriefField(BaseModel):
    """关键信息卡中的一项已验证字段。

    这不是让模型凭空填写项目表单：每一项都必须保留来源，材料没有明确表达时宁可不输出，
    再通过 ``open_questions`` 提醒用户补充。
    """

    key: DocumentBriefFieldKey
    value: str = Field(min_length=1, max_length=1_200)
    source_refs: list[DocumentSourceRef] = Field(min_length=1, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentOutlineSection(BaseModel):
    """供用户审阅的只读文档大纲章节。

    大纲是后续正式撰写前的结构蓝图，不表示系统已经创建、覆盖或导出任何文件。每个章节
    都必须回溯到当前材料，避免模型把常见模板当作原文事实。
    """

    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    intent: str = Field(min_length=1, max_length=600)
    key_points: list[str] = Field(min_length=1, max_length=8)
    source_refs: list[DocumentSourceRef] = Field(min_length=1, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentDraftSection(BaseModel):
    """一段供用户审阅、尚未落盘的 Markdown 兼容草稿正文。

    ``body`` 是后续保存 Markdown 时可直接复用的章节内容，但当前协议只保存到任务结果和
    SQLite 审计链，不代表已经创建任何文件。章节来源独立保留，方便用户先核对事实再确认
    是否进入写入步骤。
    """

    id: str = Field(min_length=1, max_length=80)
    heading: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=1_500)
    source_refs: list[DocumentSourceRef] = Field(min_length=1, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentDraftReviewSeed(BaseModel):
    """派生事实核验所需的已验证草稿快照。

    草稿章节必须先通过原任务的来源校验，才会由服务端组装进该种子。把它放在
    ``DocumentDraftSection`` 后面可保持运行时类型直接可解析，也让协议阅读顺序与业务顺序一致。
    """

    source_task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    draft_title: str = Field(min_length=1, max_length=240)
    draft_sections: list[DocumentDraftSection] = Field(min_length=1, max_length=8)
    focus: str = Field(default="", max_length=1_200)
    # 手动编辑后的正文需要重新读取材料再核验；普通已验证草稿的核验不改变它的可保存状态。
    requires_reverification: bool = False


class DocumentDraftSectionReviewSeed(BaseModel):
    """派生单章节审校所需的受控快照。

    ``draft_sections`` 只用于在派生结果中原样恢复用户已审阅的草稿；模型输入只提供当前
    ``section_id`` 对应的正文，避免无关章节占用上下文，也避免审校任务扩大为整篇重写。
    """

    source_task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    draft_title: str = Field(min_length=1, max_length=240)
    draft_sections: list[DocumentDraftSection] = Field(min_length=1, max_length=8)
    section_id: str = Field(min_length=1, max_length=80)
    heading: str = Field(min_length=1, max_length=160)
    current_body: str = Field(min_length=1, max_length=1_500)
    focus: str = Field(default="", max_length=1_200)


class DocumentDraftSectionRevisionSeed(BaseModel):
    """从本章审校结果派生修订预览所需的最小、稳定身份。

    不把原章节正文、候选文本或文件路径再次交给 Qt：服务端会按 ``source_review_task_id``
    重取已完成审校任务，并验证 suggestion/章节关系后才执行一次精确替换。这让“应用建议”
    不会退化成任意富文本写入接口。
    """

    source_review_task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    section_id: str = Field(min_length=1, max_length=80)
    suggestion_id: str = Field(min_length=1, max_length=80)


class DocumentDraftSectionBatchRevisionSeed(BaseModel):
    """从同一份本章审校结果派生多建议预览的最小身份集合。

    客户端只选择已展示的稳定 ID；正文、建议文本和文件路径仍然一律从 SQLite 中原任务快照
    恢复。最多六条是为了让差异预览保持可读，也防止一次确认隐含过多改动。
    """

    source_review_task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    section_id: str = Field(min_length=1, max_length=80)
    suggestion_ids: list[str] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def validate_unique_suggestion_ids(self) -> "DocumentDraftSectionBatchRevisionSeed":
        normalized = [item.strip() for item in self.suggestion_ids]
        if any(not item for item in normalized):
            raise ValueError("批量修订建议 ID 不能为空。")
        if len(set(normalized)) != len(normalized):
            raise ValueError("批量修订不能重复选择同一条建议。")
        self.suggestion_ids = normalized
        return self


class DocumentDraftRestoreSeed(BaseModel):
    """从已完成草稿快照恢复“新的独立预览”所需的最小身份。

    恢复不是原地回滚：它不接受正文、文件名或输出路径，只记录被恢复任务及其版本身份。
    Runtime 会再次读取该任务的已验证结果，并将其作为新任务的直接父版本。
    """

    source_task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    source_version_id: str = Field(min_length=1, max_length=120)


class DocumentDraftSectionManualRevisionSeed(BaseModel):
    """用户手动修改一章时的受控快照与版本绑定。

    ``revised_body`` 是唯一允许的用户文本输入。它不是模型结论或新的来源事实，Runtime 会在
    运行前再次验证原草稿版本与 ``original_body``，随后把结果标记为待事实核验的独立预览。
    """

    source_task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    source_version_id: str = Field(min_length=1, max_length=120)
    section_id: str = Field(min_length=1, max_length=80)
    heading: str = Field(min_length=1, max_length=160)
    original_body: str = Field(min_length=1, max_length=1_500)
    revised_body: str = Field(min_length=1, max_length=1_500)


class DocumentRevisionSuggestion(BaseModel):
    """一条不自动应用的章节审校建议。

    ``original_excerpt`` 是用户选定章节中的实际片段，``suggested_text`` 仅为候选表达；二者
    都不会回写到草稿或文件。每条建议仍需保留材料来源，方便客户判断是否接受。
    """

    id: str = Field(min_length=1, max_length=80)
    severity: DocumentRevisionSeverity = "suggestion"
    category: DocumentRevisionCategory
    original_excerpt: str = Field(min_length=1, max_length=600)
    suggested_text: str = Field(min_length=1, max_length=1_200)
    reason: str = Field(min_length=1, max_length=800)
    source_refs: list[DocumentSourceRef] = Field(min_length=1, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentDraftSectionRevisionPreview(BaseModel):
    """一个尚未落盘的章节修订差异快照。

    ``original_body`` 与 ``revised_body`` 只服务于详情页的差异阅读；文件写入仍必须另走
    ``save-draft`` 的命名与二次确认。原草稿任务和任何既有 Markdown 都不会被这个预览修改。
    """

    source_review_task_id: str = Field(min_length=1, max_length=120)
    suggestion_id: str = Field(min_length=1, max_length=80)
    # 保留 ``suggestion_id`` 兼容旧的单条预览消费者；批量预览以该列表作为真实选择集合。
    suggestion_ids: list[str] = Field(default_factory=list, max_length=6)
    section_id: str = Field(min_length=1, max_length=80)
    heading: str = Field(min_length=1, max_length=160)
    original_body: str = Field(min_length=1, max_length=1_500)
    revised_body: str = Field(min_length=1, max_length=1_500)


class DocumentDraftSectionManualRevisionPreview(BaseModel):
    """用户手动编辑产生的前后差异，仅用于审阅与后续事实核验。"""

    source_task_id: str = Field(min_length=1, max_length=120)
    section_id: str = Field(min_length=1, max_length=80)
    heading: str = Field(min_length=1, max_length=160)
    original_body: str = Field(min_length=1, max_length=1_500)
    revised_body: str = Field(min_length=1, max_length=1_500)


class DocumentDraftTemplatePreview(BaseModel):
    """一次模板化交付预览的可展示元数据。

    ``missing_sections`` 只表示模板要求但当前草稿没有可归类章节的部分，不代表系统已经
    填充了这些内容。它让客户在保存前看到交付完整度，而不是得到一份看似完整的空壳文件。
    """

    source_task_id: str = Field(min_length=1, max_length=120)
    source_version_id: str = Field(min_length=1, max_length=120)
    template_id: DocumentTemplateId
    template_name: str = Field(min_length=1, max_length=80)
    missing_sections: list[str] = Field(default_factory=list, max_length=8)


class DocumentDraftMergeConflict(BaseModel):
    """三方合并中无法由共同祖先自动裁决的一项差异。"""

    conflict_id: str = Field(min_length=1, max_length=120)
    kind: DocumentDraftMergeConflictKind
    section_id: str = Field(default="", max_length=80)
    heading: str = Field(default="", max_length=160)
    base_text: str = Field(default="", max_length=1_500)
    primary_text: str = Field(default="", max_length=1_500)
    secondary_text: str = Field(default="", max_length=1_500)


class DocumentDraftMergePreview(BaseModel):
    """可保存合并预览的来源、共同祖先和自动/人工裁决摘要。"""

    primary_task_id: str = Field(min_length=1, max_length=120)
    secondary_task_id: str = Field(min_length=1, max_length=120)
    common_ancestor_task_id: str = Field(min_length=1, max_length=120)
    automatic_section_count: int = Field(ge=0, le=16)
    resolved_conflict_count: int = Field(ge=0, le=9)
    conflicts: list[DocumentDraftMergeConflict] = Field(default_factory=list, max_length=9)


class DocumentDraftMergeCandidate(BaseModel):
    """当前草稿可选择的同根合并候选，不携带正文与来源。"""

    task_id: str = Field(min_length=1, max_length=120)
    version_id: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=160)
    kind: DocumentDraftVersionKind
    draft_title: str = Field(min_length=1, max_length=240)


class DocumentDraftMergeCandidateListResponse(BaseModel):
    """当前草稿同根、已核验的完整版本候选列表。"""

    task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    candidates: list[DocumentDraftMergeCandidate] = Field(default_factory=list, max_length=40)


class DocumentDraftMergePlanResponse(BaseModel):
    """只读三方合并计划，供 Qt 在创建新任务前展示冲突与选择。"""

    primary_task_id: str = Field(min_length=1, max_length=120)
    secondary_task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    common_ancestor_task_id: str = Field(min_length=1, max_length=120)
    primary_label: str = Field(min_length=1, max_length=160)
    secondary_label: str = Field(min_length=1, max_length=160)
    automatic_section_count: int = Field(ge=0, le=16)
    conflicts: list[DocumentDraftMergeConflict] = Field(default_factory=list, max_length=9)
    warnings: list[str] = Field(default_factory=list, max_length=6)


class DocumentDraftVersionInfo(BaseModel):
    """当前草稿快照在受控任务链中的轻量身份。

    正文继续只保存在各任务已有的 ``draft_sections`` 中；版本信息只连接任务快照，不提供
    原地覆盖能力。用户所谓“回退”应当是回看或另存旧快照，而不是悄悄改写已保存文件。
    """

    version_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    parent_task_id: str = Field(default="", max_length=120)
    kind: DocumentDraftVersionKind
    label: str = Field(min_length=1, max_length=160)
    change_summary: str = Field(default="", max_length=600)


class DocumentDraftVersionDiffSection(BaseModel):
    """当前草稿与其直接父版本中同一章节的只读差异。"""

    id: str = Field(min_length=1, max_length=80)
    heading: str = Field(min_length=1, max_length=160)
    change_kind: Literal["unchanged", "modified", "added", "removed"]
    parent_body: str = Field(default="", max_length=1_500)
    current_body: str = Field(default="", max_length=1_500)


class DocumentDraftVersionDiffResponse(BaseModel):
    """版本链中当前快照与直接父快照的受控只读比较结果。"""

    task_id: str = Field(min_length=1, max_length=120)
    parent_task_id: str = Field(min_length=1, max_length=120)
    root_task_id: str = Field(min_length=1, max_length=120)
    parent_title: str = Field(min_length=1, max_length=240)
    current_title: str = Field(min_length=1, max_length=240)
    title_changed: bool = False
    summary: str = Field(min_length=1, max_length=800)
    sections: list[DocumentDraftVersionDiffSection] = Field(min_length=1, max_length=16)
    warnings: list[str] = Field(default_factory=list, max_length=6)


class DocumentContext(BaseModel):
    """文档助手交给用户、历史页和后续 Agent 的受控结构化上下文。"""

    schema_version: str = "agentflow.document_context.v1"
    documents: list[str] = Field(default_factory=list)
    summary: str = Field(default="", max_length=4_000)
    requirements: list[DocumentRequirement] = Field(default_factory=list)
    comparisons: list[DocumentComparison] = Field(default_factory=list)
    brief_fields: list[DocumentBriefField] = Field(default_factory=list)
    outline_sections: list[DocumentOutlineSection] = Field(default_factory=list)
    draft_title: str = Field(default="", max_length=240)
    draft_sections: list[DocumentDraftSection] = Field(default_factory=list)
    review_target_title: str = Field(default="", max_length=280)
    revision_target_title: str = Field(default="", max_length=280)
    revision_target_section_id: str = Field(default="", max_length=80)
    revision_suggestions: list[DocumentRevisionSuggestion] = Field(default_factory=list)
    revision_preview: DocumentDraftSectionRevisionPreview | None = None
    manual_revision_preview: DocumentDraftSectionManualRevisionPreview | None = None
    template_preview: DocumentDraftTemplatePreview | None = None
    merge_preview: DocumentDraftMergePreview | None = None
    draft_version: DocumentDraftVersionInfo | None = None
    # ``requires_review`` 不能保存，防止用户编辑后的文字继承旧来源后被误认为已验证；
    # ``reviewed_with_questions`` 同样保留为不可交付状态，直到用户解决待确认事实。
    draft_verification_state: DocumentDraftVerificationState = "verified"
    constraints: list[DocumentFinding] = Field(default_factory=list)
    todos: list[DocumentFinding] = Field(default_factory=list)
    entities: list[DocumentFinding] = Field(default_factory=list)
    open_questions: list[DocumentFinding] = Field(default_factory=list)
    sources: list[DocumentSourceRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_context: list[str] = Field(default_factory=list)
    confidence: DocumentConfidence = "low"


class DocumentAgentRunResponse(BaseModel):
    """文档助手单独运行后的 API 响应。"""

    task_id: str
    mode: Literal["mock", "llm"] = "mock"
    status: DocumentAgentStatus
    stop_reason: str
    reply: str
    document_context: DocumentContext
    workflow_run: WorkflowRun


DocumentAgentTaskState = Literal[
    "queued",
    "running",
    "completed",
    "needs_clarification",
    "insufficient_context",
    "failed",
    "max_turns_exceeded",
    "budget_exhausted",
]


class DocumentAgentTaskStartResponse(BaseModel):
    """异步文档任务的立即受理回执。"""

    task_id: str
    status: Literal["queued"] = "queued"


class DocumentAgentTaskResultResponse(BaseModel):
    """文档页轮询终态时使用的包装协议。"""

    task_id: str
    status: DocumentAgentTaskState
    result: DocumentAgentRunResponse | None = None


class DocumentDraftSaveRequest(BaseModel):
    """用户确认把已验证的 Markdown 草稿保存为本地产物的请求。"""

    filename: str = Field(default="", max_length=120)
    # 写入不是模型工具调用，仍要求客户端明确表达这一次保存确认，避免预览被误当成导出。
    confirmed: bool = False


class DocumentDraftSaveResponse(BaseModel):
    """保存成功后的稳定回执；只返回相对位置，不暴露后端绝对目录。"""

    task_id: str
    artifact_id: str
    filename: str
    relative_path: str
    artifact_uri: str
    message: str


class DocumentDraftSectionRequest(BaseModel):
    """用户请求派生一份单章节创作预览时的最小输入。"""

    section_id: str = Field(min_length=1, max_length=80)
    instruction: str = Field(min_length=1, max_length=1_200)


class DocumentDraftReviewRequest(BaseModel):
    """用户发起只读草稿事实核验时可选的关注点。"""

    focus: str = Field(default="", max_length=1_200)


class DocumentDraftSectionReviewRequest(BaseModel):
    """用户请求审校一个已验证草稿章节时的最小输入。"""

    section_id: str = Field(min_length=1, max_length=80)
    focus: str = Field(default="", max_length=1_200)


class DocumentDraftSectionManualRevisionRequest(BaseModel):
    """用户建立一份待重新核验的手动章节修订预览时的最小输入。"""

    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(min_length=1, max_length=80)
    revised_body: str = Field(min_length=1, max_length=1_500)

    @model_validator(mode="after")
    def normalize_revised_body(self) -> "DocumentDraftSectionManualRevisionRequest":
        self.revised_body = self.revised_body.strip()
        if not self.revised_body:
            raise ValueError("手动修订后的章节正文不能为空。")
        return self


class DocumentDraftSectionRevisionRequest(BaseModel):
    """用户从已完成审校结果中明确选择的一条候选建议。"""

    suggestion_id: str = Field(min_length=1, max_length=80)


class DocumentDraftSectionBatchRevisionRequest(BaseModel):
    """用户从同一份审校结果勾选的多条候选建议。"""

    suggestion_ids: list[str] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def validate_unique_suggestion_ids(self) -> "DocumentDraftSectionBatchRevisionRequest":
        normalized = [item.strip() for item in self.suggestion_ids]
        if any(not item for item in normalized):
            raise ValueError("批量修订建议 ID 不能为空。")
        if len(set(normalized)) != len(normalized):
            raise ValueError("批量修订不能重复选择同一条建议。")
        self.suggestion_ids = normalized
        return self


class DocumentDraftRestoreRequest(BaseModel):
    """用户从结果详情或历史任务发起恢复预览时的空白受控请求。

    路径中的 task_id 已是唯一允许的输入身份；不提供正文、路径和覆盖选项，避免“恢复”变成
    任意文件写入或自由文本编辑接口。
    """

    # 恢复预览只接受路径中的历史 task_id。拒绝多余字段，避免客户端误以为正文、文件路径
    # 或覆盖选项会参与恢复，进而把只读快照恢复误用成不透明的编辑接口。
    model_config = ConfigDict(extra="forbid")


class DocumentDraftTemplatePreviewRequest(BaseModel):
    """用户从已核验草稿选择一个内置交付模板时的最小输入。"""

    # 模板 ID 是唯一允许的请求体字段；任何正文、文件名、路径或自由配置都会被拒绝。
    model_config = ConfigDict(extra="forbid")

    template_id: DocumentTemplateId


class DocumentDraftMergePreviewRequest(BaseModel):
    """从当前草稿合并另一个同根版本的最小请求。"""

    model_config = ConfigDict(extra="forbid")

    other_task_id: str = Field(min_length=1, max_length=120)
    resolutions: list[DocumentDraftMergeResolution] = Field(default_factory=list, max_length=9)

    @model_validator(mode="after")
    def validate_unique_conflict_ids(self) -> "DocumentDraftMergePreviewRequest":
        normalized = [item.conflict_id.strip() for item in self.resolutions]
        if any(not item for item in normalized):
            raise ValueError("合并冲突身份不能为空。")
        if len(set(normalized)) != len(normalized):
            raise ValueError("同一合并冲突只能选择一次处理方式。")
        for item, conflict_id in zip(self.resolutions, normalized, strict=True):
            item.conflict_id = conflict_id
        self.other_task_id = self.other_task_id.strip()
        if not self.other_task_id:
            raise ValueError("请选择另一个版本后再建立合并预览。")
        return self


# 以下三个模型只用于 Model -> Runner 的中间 JSON，不直接作为 API 输出。模型只能提交
# source_ids，Runner 会拒绝未知 ID 并映射成上面的 DocumentSourceRef。
class DocumentDraftFinding(BaseModel):
    text: str = Field(min_length=1, max_length=1_200)
    source_ids: list[str] = Field(default_factory=list, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentDraftRequirement(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=1_200)
    category: DocumentRequirementCategory = "unknown"
    priority: DocumentPriority = "unknown"
    source_ids: list[str] = Field(default_factory=list, min_length=1, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentDraftComparison(BaseModel):
    """模型提交的跨文档比较中间项，只能引用 Runtime 分配的 source_id。"""

    dimension: str = Field(min_length=1, max_length=120)
    kind: DocumentComparisonKind
    summary: str = Field(min_length=1, max_length=1_200)
    source_ids: list[str] = Field(min_length=2, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentDraftBriefField(BaseModel):
    """模型提交的关键信息卡字段，只能引用本次 Tool 分配的来源 ID。"""

    key: DocumentBriefFieldKey
    value: str = Field(min_length=1, max_length=1_200)
    source_ids: list[str] = Field(min_length=1, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentDraftOutlineSection(BaseModel):
    """模型提交的大纲章节，只能引用本次 Tool 返回的 source_id。"""

    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    intent: str = Field(min_length=1, max_length=600)
    key_points: list[str] = Field(min_length=1, max_length=8)
    source_ids: list[str] = Field(min_length=1, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentDraftPreviewSection(BaseModel):
    """模型提交的草稿章节，只能引用本次 Tool 返回的 source_id。"""

    id: str = Field(min_length=1, max_length=80)
    heading: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=1_500)
    source_ids: list[str] = Field(min_length=1, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentDraftRevisionSuggestion(BaseModel):
    """模型提交的章节审校建议，只能引用本轮 Tool 返回的 source_id。"""

    id: str = Field(min_length=1, max_length=80)
    severity: DocumentRevisionSeverity = "suggestion"
    category: DocumentRevisionCategory
    original_excerpt: str = Field(min_length=1, max_length=600)
    suggested_text: str = Field(min_length=1, max_length=1_200)
    reason: str = Field(min_length=1, max_length=800)
    source_ids: list[str] = Field(min_length=1, max_length=4)
    confidence: DocumentConfidence = "medium"


class DocumentModelOutput(BaseModel):
    """要求模型在工具循环结束后返回的严格 JSON。"""

    answer: str = Field(min_length=1, max_length=4_000)
    answer_source_ids: list[str] = Field(min_length=1, max_length=4)
    summary: str = Field(default="", max_length=4_000)
    requirements: list[DocumentDraftRequirement] = Field(default_factory=list, max_length=24)
    comparisons: list[DocumentDraftComparison] = Field(default_factory=list, max_length=16)
    brief_fields: list[DocumentDraftBriefField] = Field(default_factory=list, max_length=7)
    outline_sections: list[DocumentDraftOutlineSection] = Field(default_factory=list, max_length=12)
    draft_title: str = Field(default="", max_length=240)
    draft_sections: list[DocumentDraftPreviewSection] = Field(default_factory=list, max_length=8)
    revision_suggestions: list[DocumentDraftRevisionSuggestion] = Field(default_factory=list, max_length=8)
    constraints: list[DocumentDraftFinding] = Field(default_factory=list, max_length=16)
    todos: list[DocumentDraftFinding] = Field(default_factory=list, max_length=16)
    entities: list[DocumentDraftFinding] = Field(default_factory=list, max_length=16)
    open_questions: list[DocumentDraftFinding] = Field(default_factory=list, max_length=16)
    confidence: DocumentConfidence = "medium"

    @model_validator(mode="before")
    @classmethod
    def normalize_compact_finding_lists(cls, value: object) -> object:
        """兼容模型把非需求清单简写成字符串数组的常见格式。

        简写条目只能继承模型已经提交的 ``answer_source_ids``，不会由后端猜测来源；缺少
        答案来源时，后续字段校验和来源映射仍会拒绝该结果。
        """

        if not isinstance(value, dict):
            return value

        normalized = dict(value)
        answer_source_ids = normalized.get("answer_source_ids")
        source_ids = answer_source_ids if isinstance(answer_source_ids, list) else []

        for field_name in ("constraints", "todos", "entities", "open_questions"):
            items = normalized.get(field_name)
            if not isinstance(items, list):
                continue
            compact_items: list[object] = []
            for item in items:
                if isinstance(item, str) and item.strip():
                    compact_items.append(
                        {
                            "text": item.strip(),
                            "source_ids": source_ids,
                            "confidence": "low",
                        }
                    )
                else:
                    compact_items.append(item)
            normalized[field_name] = compact_items

        requirements = normalized.get("requirements")
        if isinstance(requirements, list):
            compact_requirements: list[object] = []
            for index, item in enumerate(requirements, start=1):
                if isinstance(item, str) and item.strip():
                    compact_requirements.append(
                        {
                            "id": f"req_{index:02d}",
                            "text": item.strip(),
                            "category": "unknown",
                            "priority": "unknown",
                            "source_ids": source_ids,
                            "confidence": "low",
                        }
                    )
                elif isinstance(item, dict):
                    normalized_item = dict(item)
                    # 模型常把“范围、接口、性能”等具体主题误写进 category。它们不是本协议
                    # 的稳定分类，但也不意味着整条有来源的需求无效；降级为 unknown，保留
                    # 原文、来源和待后续人工/专用规则判断的空间，而不是擅自改成其他类别。
                    if normalized_item.get("category") not in {
                        "functional",
                        "output",
                        "constraint",
                        "acceptance",
                        "unknown",
                    }:
                        normalized_item["category"] = "unknown"
                    if normalized_item.get("priority") not in {"must", "should", "could", "unknown"}:
                        normalized_item["priority"] = "unknown"
                    normalized_item.setdefault("source_ids", source_ids)
                    compact_requirements.append(normalized_item)
                else:
                    compact_requirements.append(item)
            normalized["requirements"] = compact_requirements

        # 部分 provider 倾向把固定字段写成 {"purpose": "..."}。兼容这种紧凑写法，
        # 但仍为每一项补齐可校验的 source_ids；未知 key 会继续由 Pydantic 拒绝。
        brief_fields = normalized.get("brief_fields")
        if isinstance(brief_fields, dict):
            compact_brief_fields: list[object] = []
            for key, item in brief_fields.items():
                if isinstance(item, str) and item.strip():
                    compact_brief_fields.append(
                        {
                            "key": key,
                            "value": item.strip(),
                            "source_ids": source_ids,
                            "confidence": "low",
                        }
                    )
                elif isinstance(item, dict):
                    normalized_item = dict(item)
                    normalized_item.setdefault("key", key)
                    if "value" not in normalized_item and isinstance(normalized_item.get("text"), str):
                        normalized_item["value"] = normalized_item["text"].strip()
                    normalized_item.setdefault("source_ids", source_ids)
                    compact_brief_fields.append(normalized_item)
            normalized["brief_fields"] = compact_brief_fields
        elif isinstance(brief_fields, list):
            normalized_brief_fields: list[object] = []
            for item in brief_fields:
                if isinstance(item, dict):
                    normalized_item = dict(item)
                    if "value" not in normalized_item and isinstance(normalized_item.get("text"), str):
                        normalized_item["value"] = normalized_item["text"].strip()
                    normalized_item.setdefault("source_ids", source_ids)
                    # 关键信息卡只承诺固定的 7 个字段。模型偶尔会额外写入 acceptance 等
                    # 合理但不属于本卡片契约的标签；不把它错误映射为交付物或节点，也不因
                    # 此阻断已经带来源的草稿，直接忽略未知字段即可。
                    field_key = normalized_item.get("key")
                    if isinstance(field_key, str) and field_key and field_key not in {
                        "subject",
                        "purpose",
                        "scope",
                        "stakeholders",
                        "deliverables",
                        "milestones",
                        "risks",
                    }:
                        continue
                    normalized_brief_fields.append(normalized_item)
                else:
                    normalized_brief_fields.append(item)
            normalized["brief_fields"] = normalized_brief_fields

        # 大纲字段兼容少量常见的紧凑命名，但仍要求章节标题、写作意图、要点和来源都能通过
        # 正式模型校验。这里不会把自然语言段落强拆成章节，避免错误地伪装为已验证大纲。
        outline_sections = normalized.get("outline_sections")
        if isinstance(outline_sections, list):
            normalized_sections: list[object] = []
            for index, item in enumerate(outline_sections, start=1):
                if not isinstance(item, dict):
                    normalized_sections.append(item)
                    continue
                normalized_item = dict(item)
                normalized_item.setdefault("id", f"section_{index:02d}")
                if "intent" not in normalized_item and isinstance(normalized_item.get("description"), str):
                    normalized_item["intent"] = normalized_item["description"].strip()
                if "key_points" not in normalized_item and isinstance(normalized_item.get("points"), list):
                    normalized_item["key_points"] = normalized_item["points"]
                normalized_item.setdefault("source_ids", source_ids)
                normalized_sections.append(normalized_item)
            normalized["outline_sections"] = normalized_sections

        # Provider 偶尔把草稿正文写为 content、markdown 或 text。这里只做字段兼容和来源
        # 补齐，不会尝试从任意自然语言回答拼装章节，避免把未验证内容伪装成创作草稿。
        draft_sections = normalized.get("draft_sections")
        if isinstance(draft_sections, list):
            normalized_sections = []
            for index, item in enumerate(draft_sections, start=1):
                if not isinstance(item, dict):
                    normalized_sections.append(item)
                    continue
                normalized_item = dict(item)
                normalized_item.setdefault("id", f"draft_{index:02d}")
                if "heading" not in normalized_item and isinstance(normalized_item.get("title"), str):
                    normalized_item["heading"] = normalized_item["title"].strip()
                if "body" not in normalized_item:
                    for compatible_key in ("content", "markdown", "text"):
                        compatible_value = normalized_item.get(compatible_key)
                        if isinstance(compatible_value, str) and compatible_value.strip():
                            normalized_item["body"] = compatible_value.strip()
                            break
                normalized_item.setdefault("source_ids", source_ids)
                normalized_sections.append(normalized_item)
            normalized["draft_sections"] = normalized_sections
        return normalized


class DocumentChunkSummary(BaseModel):
    """长文档单个连续分块的内部压缩结果。

    它不是最终 API 协议，也不允许模型填写来源 ID。Runtime 已经知道该摘要对应的原文件和
    行号范围，最终汇总时才把这些稳定来源 ID 放入 ``DocumentModelOutput``，避免中间模型
    伪造跨块引用。
    """

    summary: str = Field(min_length=1, max_length=1_200)
    key_points: list[str] = Field(default_factory=list, max_length=8)
    requirement_candidates: list[str] = Field(default_factory=list, max_length=12)
    open_questions: list[str] = Field(default_factory=list, max_length=8)
    confidence: DocumentConfidence = "medium"

    @model_validator(mode="before")
    @classmethod
    def normalize_compact_lists(cls, value: object) -> object:
        """限制中间摘要的体积，并兼容模型偶尔返回空白或非字符串项目。"""

        if not isinstance(value, dict):
            return value

        normalized = dict(value)
        for field_name in ("key_points", "requirement_candidates", "open_questions"):
            items = normalized.get(field_name)
            if not isinstance(items, list):
                continue
            normalized[field_name] = [
                item.strip()
                for item in items
                if isinstance(item, str) and item.strip()
            ]
        return normalized
