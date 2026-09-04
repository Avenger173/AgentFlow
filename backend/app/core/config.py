import os
from dataclasses import dataclass, field
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
_PLACEHOLDER_VALUES = {
    "replace_me",
    "your-model-name",
    "your-openai-model",
    "your-claude-model",
    "your-qwen-model",
}


def _load_local_env(env_path: Path) -> None:
    """加载本地 .env，但不覆盖系统环境变量。

    开发期 API Key 只放在本地 .env 或系统环境变量中，不能写进代码和文档。
    这里实现一个很小的解析器，避免为了读取几行配置额外引入运行时依赖。
    """

    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_local_env(BACKEND_ROOT / ".env")


def _first_env(*names: str, default: str = "") -> str:
    """按优先级读取环境变量。

    模型配置正在从 DeepSeek 单供应商过渡到多供应商。这里保留一个小工具，
    让通用 `AGENTFLOW_LLM_*` 字段优先，同时兼容早期已经写进本地 `.env`
    的 `DEEPSEEK_*` 别名。
    """

    for name in names:
        value = os.getenv(name)
        if value and value.strip().lower() not in _PLACEHOLDER_VALUES:
            return value
    return default


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("AGENTFLOW_APP_NAME", "AgentFlow Backend")
    app_version: str = os.getenv("AGENTFLOW_APP_VERSION", "0.1.0")
    environment: str = os.getenv("AGENTFLOW_ENVIRONMENT", "development")
    host: str = os.getenv("AGENTFLOW_HOST", "127.0.0.1")
    port: int = int(os.getenv("AGENTFLOW_PORT", "8765"))
    allowed_origins: list[str] = field(
        default_factory=lambda: os.getenv("AGENTFLOW_ALLOWED_ORIGINS", "*").split(",")
    )
    chat_mode: str = os.getenv("AGENTFLOW_CHAT_MODE", "mock").lower()
    llm_provider: str = _first_env("AGENTFLOW_LLM_PROVIDER", default="mock").lower()
    llm_base_url: str = _first_env(
        "AGENTFLOW_LLM_BASE_URL",
        "DEEPSEEK_BASE_URL",
    ).rstrip("/")
    llm_model: str = _first_env(
        "AGENTFLOW_LLM_MODEL",
        "DEEPSEEK_MODEL",
    )
    llm_thinking: str = _first_env(
        "AGENTFLOW_LLM_THINKING",
        "DEEPSEEK_THINKING",
        default="disabled",
    ).lower()
    llm_api_key: str = _first_env("AGENTFLOW_LLM_API_KEY", "DEEPSEEK_API_KEY")
    llm_max_tokens: int = int(os.getenv("AGENTFLOW_LLM_MAX_TOKENS", "2048"))
    llm_temperature: float = float(os.getenv("AGENTFLOW_LLM_TEMPERATURE", "0.3"))
    llm_timeout_seconds: float = float(os.getenv("AGENTFLOW_LLM_TIMEOUT_SECONDS", "60"))
    # Node 版 DeepSeek Harness 是可选执行后端。默认关闭，直到 Adapter、权限映射和
    # 只读试点均完成验收；启用开关本身不能绕过现有 Runtime 的治理边界。
    node_harness_enabled: bool = os.getenv("AGENTFLOW_NODE_HARNESS_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    # LGM 平台集成默认全部关闭。开关只允许准备已经验证的 Adapter，不能改变
    # Commander 的 action 准入、权限策略或客户任务的默认 Native Runtime 路径。
    mcp_enabled: bool = os.getenv("AGENTFLOW_MCP_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    langgraph_enabled: bool = os.getenv("AGENTFLOW_LANGGRAPH_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    langchain_adapters_enabled: bool = os.getenv(
        "AGENTFLOW_LANGCHAIN_ADAPTERS_ENABLED", "false"
    ).lower() in {"1", "true", "yes", "on"}
    node_harness_probe_timeout_seconds: float = float(
        os.getenv("AGENTFLOW_NODE_HARNESS_PROBE_TIMEOUT_SECONDS", "15")
    )
    # Profile 预检只组合并检查 Cordis 配置，不调用模型。独立超时避免 npm/磁盘异常时拖慢
    # 管理接口；真正任务的超时会在后续 Bridge 中按任务预算单独设置。
    node_harness_profile_timeout_seconds: float = float(
        os.getenv("AGENTFLOW_NODE_HARNESS_PROFILE_TIMEOUT_SECONDS", "20")
    )
    # headless CLI 没有独立的流式服务入口。首次 Bridge 只允许受控的单任务批处理，并且
    # 仍由 AgentFlow 的任务预算控制；这个值仅防止外部子进程无限等待。
    node_harness_task_timeout_seconds: float = float(
        os.getenv("AGENTFLOW_NODE_HARNESS_TASK_TIMEOUT_SECONDS", "180")
    )
    # 授权图库与模型供应商分离。素材 Key 只在导出阶段的受控 Provider 中读取，模型上下文、
    # Task 日志和 artifact 对外回执都不应接触该值。
    pexels_api_key: str = _first_env("AGENTFLOW_PEXELS_API_KEY", "PEXELS_API_KEY")
    # 即梦 Seedream 是图像生成 Provider，不是聊天 ModelGateway 的一个模型。环境变量仅作
    # 部署兜底；桌面端正常使用 DPAPI 保护的 provider 专属 Key。
    seedream_api_key: str = _first_env("AGENTFLOW_SEEDREAM_API_KEY", "SEEDREAM_API_KEY")
    seedream_base_url: str = _first_env(
        "AGENTFLOW_SEEDREAM_BASE_URL",
        default="https://ark.cn-beijing.volces.com/api/v3",
    ).rstrip("/")
    seedream_model: str = _first_env(
        "AGENTFLOW_SEEDREAM_MODEL",
        default="doubao-seedream-5-0-260128",
    )
    # 通用 PPT 数据研究优先使用可返回清洗正文的独立搜索服务；没有配置 Key 时会安全降级到
    # 已有 DeepSeek 原生搜索。它是 ResearchGateway 的工具配置，不属于聊天模型 provider。
    tavily_api_key: str = _first_env("AGENTFLOW_TAVILY_API_KEY", "TAVILY_API_KEY")
    presentation_research_search_provider: str = _first_env(
        "AGENTFLOW_PRESENTATION_RESEARCH_SEARCH_PROVIDER",
        default="auto",
    ).lower()
    # 不擅自绕过客户网络策略：environment 使用系统/进程代理，direct 只在客户明确配置后直连。
    presentation_research_network_mode: str = _first_env(
        "AGENTFLOW_PRESENTATION_RESEARCH_NETWORK_MODE",
        default="environment",
    ).lower()
    # Python 网络库未必读取 Windows WinINET 系统代理。这里仅接收用户显式提供的专用代理 URL；
    # 不持久化、打印或回传该值，避免将可能包含认证信息的 URL 泄露到任务审计。
    presentation_research_proxy_url: str = _first_env("AGENTFLOW_PRESENTATION_RESEARCH_PROXY_URL")

    @property
    def backend_root(self) -> Path:
        # config.py 位于 backend/app/core/，parents[2] 正好是 backend/。
        return BACKEND_ROOT

    @property
    def project_root(self) -> Path:
        # 项目根目录用于扫描用户安装的 agents/。打包后可用环境变量覆盖此路径。
        return Path(os.getenv("AGENTFLOW_PROJECT_ROOT", self.backend_root.parent)).resolve()

    @property
    def builtin_agents_dir(self) -> Path:
        # 内置 Agent 随后端代码发布，不允许用户插件覆盖。
        return self.backend_root / "app" / "agents" / "builtin"

    @property
    def user_agents_dir(self) -> Path:
        # 用户安装或调试中的 Agent manifest 放在项目根 agents/，后续 .afagent 会解压到这里。
        return Path(os.getenv("AGENTFLOW_USER_AGENTS_DIR", self.project_root / "agents")).resolve()

    @property
    def data_dir(self) -> Path:
        # 开发期默认写到项目根 data/，打包后可用环境变量切到用户数据目录。
        return Path(os.getenv("AGENTFLOW_DATA_DIR", self.project_root / "data")).resolve()

    @property
    def data_workspace_dir(self) -> Path:
        """返回数据工作台的受控导入目录。

        表格原件与文档工作区、正式交付物分别隔离：D1 只允许通过导入 API 写入这里，
        客户端和模型均不获得本机绝对路径。后续 D3 的分析工作簿会单独写入
        ``output/data_analysis``，绝不覆盖本目录中的源文件。
        """

        return Path(
            os.getenv("AGENTFLOW_DATA_WORKSPACE_DIR", self.data_dir / "data_workspace")
        ).resolve()

    @property
    def data_analysis_output_dir(self) -> Path:
        """返回数据工作台正式 Excel 交付物的固定输出目录。

        分析工作簿只会生成新文件并在回读验证后移动到这里；它与导入工作区隔离，避免任何
        导出路径或覆盖行为由客户端控制。环境变量仅用于打包部署和离线验证。
        """

        return Path(
            os.getenv(
                "AGENTFLOW_DATA_ANALYSIS_OUTPUT_DIR",
                self.project_root / "output" / "data_analysis",
            )
        ).resolve()

    @property
    def data_chart_output_dir(self) -> Path:
        """返回数据工作台 PNG 图表的固定交付目录。

        图表是基于客户已确认的 D2 聚合结果新建的交付物，不与 Excel 工作簿或导入源文件
        混放。环境变量只供打包部署与离线验证替换目录，客户端不能提交输出路径。
        """

        return Path(
            os.getenv(
                "AGENTFLOW_DATA_CHART_OUTPUT_DIR",
                self.project_root / "output" / "data_charts",
            )
        ).resolve()

    @property
    def data_transformation_output_dir(self) -> Path:
        """返回字段加工副本的固定交付目录。

        字段加工不覆盖导入工作区中的 CSV/XLSX，而是把确认后的派生表写入独立目录。这个
        分离也让任务历史可以只开放受控 artifact，不需要把客户源文件路径交给桌面端。
        """

        return Path(
            os.getenv(
                "AGENTFLOW_DATA_TRANSFORMATION_OUTPUT_DIR",
                self.project_root / "output" / "data_transformations",
            )
        ).resolve()

    @property
    def data_join_output_dir(self) -> Path:
        """返回多数据集合并副本的固定交付目录。"""

        return Path(
            os.getenv(
                "AGENTFLOW_DATA_JOIN_OUTPUT_DIR",
                self.project_root / "output" / "data_joins",
            )
        ).resolve()

    @property
    def node_harness_runtime_dir(self) -> Path:
        """返回随项目或安装目录携带的锁定 Node Harness 依赖目录。"""

        return Path(
            os.getenv(
                "AGENTFLOW_NODE_HARNESS_RUNTIME_DIR",
                self.backend_root / "runtime" / "deepseek_harness_node",
            )
        ).resolve()

    @property
    def node_harness_state_dir(self) -> Path:
        """保存 Harness 自己的 session/配置状态，和客户交付产物保持隔离。"""

        return Path(
            os.getenv(
                "AGENTFLOW_NODE_HARNESS_STATE_DIR",
                self.data_dir / "deepseek_harness",
            )
        ).resolve()

    @property
    def node_harness_launch_dir(self) -> Path:
        """返回 Harness 子进程的受控空启动目录。

        CLI 的启动目录会参与官方 `.env` 凭据回退，因此不能直接使用客户 workspace。
        后续若允许只读 workspace，仍由专门的 profile 字段传入，不能改变进程 cwd。
        """

        return (self.node_harness_state_dir / "launch").resolve()

    @property
    def knowledge_storage_dir(self) -> Path:
        """返回知识库私有受控副本根目录。

        它与 workspace 导入目录分开：workspace 只代表客户本次选择的材料，知识库需要保存
        可版本化副本以支持后续更新、删除和来源追溯。客户端始终只传相对文件名，不能指定
        该目录或其中的任何物理路径。
        """

        return Path(
            os.getenv(
                "AGENTFLOW_KNOWLEDGE_STORAGE_DIR",
                self.data_dir / "knowledge_bases",
            )
        ).resolve()

    @property
    def knowledge_vector_storage_dir(self) -> Path:
        """返回知识库 generation 隔离的 Chroma 本地目录。"""

        return Path(
            os.getenv(
                "AGENTFLOW_KNOWLEDGE_VECTOR_STORAGE_DIR",
                self.data_dir / "knowledge_vectors",
            )
        ).resolve()

    @property
    def knowledge_embedding_cache_dir(self) -> Path:
        """返回 FastEmbed 的专用模型缓存目录，避免隐式写入用户通用缓存。"""

        return Path(
            os.getenv(
                "AGENTFLOW_KNOWLEDGE_EMBEDDING_CACHE_DIR",
                self.data_dir / "knowledge_embedding_models",
            )
        ).resolve()

    @property
    def ocr_model_cache_dir(self) -> Path:
        """返回本地 OCR 候选模型的受控缓存目录。

        OCR 是 K7 的可选能力，缓存与知识库向量模型、客户资料和交付文件隔离。首次下载只允许
        由后续客户明确确认的准备动作触发；普通导入、预览和索引不得创建或联网写入本目录。
        """

        return Path(
            os.getenv(
                "AGENTFLOW_OCR_MODEL_CACHE_DIR",
                self.data_dir / "ocr_models",
            )
        ).resolve()

    @property
    def document_draft_output_dir(self) -> Path:
        """返回用户确认保存的 Markdown 草稿目录。

        文档助手的正式草稿属于客户可见交付物，而不是 Runtime 临时文件，因此默认落在项目
        根目录 ``output/document_drafts``。环境变量只服务于打包部署或离线验证；无论目录
        如何配置，API 都不会接受客户端提交的本机路径。
        """

        return Path(
            os.getenv(
                "AGENTFLOW_DOCUMENT_DRAFT_OUTPUT_DIR",
                self.project_root / "output" / "document_drafts",
            )
        ).resolve()

    @property
    def document_processing_output_dir(self) -> Path:
        """返回 PDF 等确定性文档处理工具的受控输出目录。

        该目录与需要用户二次确认的 Markdown 草稿分开：PDF 工具由用户在明确文件、操作与
        输出范围后主动发起，生成的始终是新副本。客户端不能指定路径、覆盖已有文件或写回
        workspace 原件。
        """

        return Path(
            os.getenv(
                "AGENTFLOW_DOCUMENT_PROCESSING_OUTPUT_DIR",
                self.project_root / "output" / "document_processing",
            )
        ).resolve()

    @property
    def document_presentation_output_dir(self) -> Path:
        """返回用户确认导出的项目方案演示文稿目录。

        演示文稿是从已核验的文档草稿确定性渲染出的独立交付物，不能和临时 Runtime 文件或
        PDF 整理工具的输出混放。客户端只提交文件名，根目录始终由后端固定。
        """

        return Path(
            os.getenv(
                "AGENTFLOW_DOCUMENT_PRESENTATION_OUTPUT_DIR",
                self.project_root / "output" / "document_presentations",
            )
        ).resolve()

    @property
    def knowledge_report_output_dir(self) -> Path:
        """返回客户确认导出的知识库深度报告目录。

        K4 报告来自某次冻结 generation 的已验证 checkpoint，属于独立交付物，不应与文档助手
        草稿、PPT 或运行期临时输出混放。客户端只提交文件名，实际根目录始终由后端控制。
        """

        return Path(
            os.getenv(
                "AGENTFLOW_KNOWLEDGE_REPORT_OUTPUT_DIR",
                self.project_root / "output" / "knowledge_reports",
            )
        ).resolve()

    @property
    def database_path(self) -> Path:
        # 任务状态第一版用 SQLite 单文件；后续如迁移 PostgreSQL，可保留上层仓储接口。
        return Path(os.getenv("AGENTFLOW_DATABASE_PATH", self.data_dir / "agentflow.db")).resolve()

    @property
    def any_llm_api_key(self) -> str:
        """返回任意已配置的模型 Key，用于 `chat_mode=auto` 的轻量判断。

        真正调用哪个 Key 仍交给 ModelGateway 按 provider 精确解析；这里不能暴露 Key，
        也不能在日志中打印它，只用于判断“是否看起来具备真实模型配置”。
        """

        return _first_env(
            "AGENTFLOW_LLM_API_KEY",
            "DEEPSEEK_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "CLAUDE_API_KEY",
            "QWEN_API_KEY",
            "DASHSCOPE_API_KEY",
        )


settings = Settings()
