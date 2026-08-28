from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Callable

from app.core.config import settings


_INIT_LOCK = Lock()
_INITIALIZED_PATHS: set[Path] = set()


@dataclass(frozen=True)
class _SchemaMigration:
    """一条只向前应用的 SQLite migration 定义。

    旧表仍保留在 ``_create_tables`` 的兼容初始化中；从知识库 K1 开始，新增持久化结构都
    必须登记在这里。``signature`` 是人工可读的稳定 DDL 摘要，改表却忘记新建 migration
    时会触发 checksum 不一致，避免不同桌面端对同一数据库作出不同解释。
    """

    migration_id: str
    signature: str
    apply: Callable[[sqlite3.Connection], None]
    # 只有需要重建含 CHECK 约束的历史表时才允许临时关闭外键。普通 migration 仍保持
    # SQLite 的默认外键保护；重建完成前会执行 foreign_key_check，避免把损坏事实提交。
    requires_foreign_keys_disabled: bool = False

    @property
    def checksum(self) -> str:
        return sha256(self.signature.encode("utf-8")).hexdigest()


def get_connection() -> sqlite3.Connection:
    """创建 SQLite 连接。

    当前请求量和写入量都很小，先使用“按操作短连接”的方式，避免全局连接在 Uvicorn
    reload、线程切换或测试客户端中产生生命周期问题。
    """

    _ensure_database()
    connection = sqlite3.connect(settings.database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _ensure_database() -> None:
    database_path = settings.database_path
    with _INIT_LOCK:
        if database_path in _INITIALIZED_PATHS:
            return

        database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database_path) as connection:
            # migration runner 需要按字段名读取已应用记录；初始化连接也必须与业务短连接使用
            # 同一 Row 协议，否则首启写入成功、下一次冷启动校验 checksum 时会退化成 tuple。
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            _create_tables(connection)
            _apply_schema_migrations(connection)
        _INITIALIZED_PATHS.add(database_path)


def _create_tables(connection: sqlite3.Connection) -> None:
    """创建当前阶段需要的最小任务表。

    表字段偏向稳定协议：run_json/event_json/plan_json 保存 Pydantic JSON，方便当前快速迭代；
    status、mode、created_at 这类常用字段单独列出，后续做列表页和索引时不用解析 JSON。
    """

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS workflow_runs (
            task_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            max_risk_level TEXT NOT NULL,
            requires_confirmation INTEGER NOT NULL,
            run_json TEXT NOT NULL,
            plan_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS workflow_events (
            task_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            step_id TEXT,
            level TEXT NOT NULL,
            event_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (task_id, sequence),
            FOREIGN KEY (task_id) REFERENCES workflow_runs(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_workflow_events_task_id
        ON workflow_events(task_id, sequence);

        CREATE TABLE IF NOT EXISTS workflow_steps (
            task_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            step_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            requires_confirmation INTEGER NOT NULL,
            step_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (task_id, step_id),
            FOREIGN KEY (task_id) REFERENCES workflow_runs(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_workflow_steps_task_order
        ON workflow_steps(task_id, step_index);

        CREATE INDEX IF NOT EXISTS idx_workflow_steps_status
        ON workflow_steps(task_id, status, risk_level);

        CREATE TABLE IF NOT EXISTS workflow_artifacts (
            task_id TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            summary TEXT NOT NULL,
            uri TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            artifact_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (task_id, artifact_id),
            FOREIGN KEY (task_id) REFERENCES workflow_runs(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_workflow_artifacts_task_kind
        ON workflow_artifacts(task_id, kind, created_at ASC);

        CREATE INDEX IF NOT EXISTS idx_workflow_artifacts_step
        ON workflow_artifacts(task_id, step_id);

        CREATE TABLE IF NOT EXISTS workflow_tool_calls (
            task_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            permission_required INTEGER NOT NULL,
            tool_call_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (task_id, call_id),
            FOREIGN KEY (task_id) REFERENCES workflow_runs(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_workflow_tool_calls_task_status
        ON workflow_tool_calls(task_id, status, created_at ASC);

        CREATE INDEX IF NOT EXISTS idx_workflow_tool_calls_step
        ON workflow_tool_calls(task_id, step_id);

        CREATE INDEX IF NOT EXISTS idx_workflow_runs_list_filters
        ON workflow_runs(
            status,
            mode,
            max_risk_level,
            requires_confirmation,
            updated_at DESC,
            created_at DESC
        );

        -- 计划本体的历史快照与 workflow_runs.plan_json 分离。后者只表示“当前准备执行哪一版”，
        -- 此表保证客户在修订后仍能回看旧版本，且不需要把步骤编辑权限交给客户端。
        CREATE TABLE IF NOT EXISTS workflow_plan_versions (
            task_id TEXT NOT NULL,
            plan_version INTEGER NOT NULL,
            plan_id TEXT NOT NULL,
            parent_plan_id TEXT NOT NULL DEFAULT '',
            user_goal TEXT NOT NULL,
            change_summary TEXT NOT NULL DEFAULT '',
            plan_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (task_id, plan_version),
            UNIQUE (task_id, plan_id),
            FOREIGN KEY (task_id) REFERENCES workflow_runs(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_workflow_plan_versions_task
        ON workflow_plan_versions(task_id, plan_version DESC);

        CREATE TABLE IF NOT EXISTS runtime_permission_requests (
            request_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            permissions_json TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            summary TEXT NOT NULL,
            details_json TEXT NOT NULL,
            request_json TEXT NOT NULL,
            decision TEXT NOT NULL DEFAULT 'pending',
            decided_by TEXT NOT NULL DEFAULT '',
            decided_at TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES workflow_runs(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_runtime_permission_task_decision
        ON runtime_permission_requests(task_id, decision, updated_at DESC, created_at DESC);

        -- 控制请求与执行线程解耦：API 只写短小、可恢复的暂停/取消信号，Runtime 在安全边界
        -- 消费它。不能用进程内 bool 充当唯一状态，否则客户端刷新或服务重启后会丢失意图。
        CREATE TABLE IF NOT EXISTS runtime_execution_controls (
            task_id TEXT PRIMARY KEY,
            pause_requested INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES workflow_runs(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_runtime_execution_controls_pending
        ON runtime_execution_controls(pause_requested, cancel_requested, updated_at DESC);

        -- 长期记忆只保存客户确认过的短事实；任务事件仍是会话/执行态的唯一事实来源。
        CREATE TABLE IF NOT EXISTS long_term_memories (
            memory_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_task_id TEXT NOT NULL DEFAULT '',
            user_confirmed INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_long_term_memories_scope_enabled
        ON long_term_memories(scope, enabled, updated_at DESC, created_at DESC);
        """
    )


def _apply_schema_migrations(connection: sqlite3.Connection) -> None:
    """以顺序、可校验的方式应用 K1 之后的 SQLite 结构变更。

    SQLite 没有跨文件迁移框架；这里保持最小实现：每条 migration 在一个短事务中完成 DDL
    和已应用记录写入。派生 FTS/Chroma 数据不在此事务内，后续只能通过 index job 与
    generation 状态补偿，不能把它们伪装成同一数据库事务。
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()

    for migration in _SCHEMA_MIGRATIONS:
        row = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE migration_id = ?",
            (migration.migration_id,),
        ).fetchone()
        if row is not None:
            if str(row["checksum"]) != migration.checksum:
                raise RuntimeError(
                    "SQLite migration checksum 不一致："
                    f"{migration.migration_id}。数据库结构可能被非受控修改，请勿继续启动。"
                )
            continue

        foreign_keys_disabled = migration.requires_foreign_keys_disabled
        try:
            if foreign_keys_disabled:
                # SQLite 不能在事务中切换该 PRAGMA，因此必须在 BEGIN 前完成。这个分支仅服务
                # 于受控表重建，绝不作为业务写入绕过外键的一般手段。
                connection.commit()
                connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            migration.apply(connection)
            if foreign_keys_disabled:
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError("SQLite OCR 契约迁移后的外键校验失败，已拒绝提交。")
            connection.execute(
                "INSERT INTO schema_migrations (migration_id, checksum) VALUES (?, ?)",
                (migration.migration_id, migration.checksum),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if foreign_keys_disabled:
                connection.execute("PRAGMA foreign_keys=ON")


def _apply_knowledge_foundation_v1(connection: sqlite3.Connection) -> None:
    """建立 K1.1 的资料库事实表，不包含原文、分块或任何派生索引。"""

    # Index Profile 是 generation 的可重建算法快照。它与 Chroma/FastEmbed 的物理文件
    # 分离，低配设备尚未下载本地模型时也能安全创建空资料库。
    connection.execute(
        """
        CREATE TABLE knowledge_index_profiles (
            profile_id TEXT PRIMARY KEY,
            keyword_backend TEXT NOT NULL CHECK (keyword_backend = 'sqlite_fts5'),
            keyword_profile_version TEXT NOT NULL,
            splitter_profile_version TEXT NOT NULL,
            vector_backend TEXT NOT NULL CHECK (
                vector_backend IN ('chroma_persistent', 'qdrant_local', 'disabled')
            ),
            embedding_provider TEXT NOT NULL CHECK (embedding_provider IN ('fastembed', 'disabled')),
            embedding_model TEXT NOT NULL,
            embedding_profile_version TEXT NOT NULL,
            rerank_mode TEXT NOT NULL CHECK (rerank_mode IN ('disabled', 'optional')),
            profile_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # 资料库状态只表示产品事实；真正的可查询来源由后续 generation 指针和验证结果共同
    # 决定。deleted 采用软状态保留脱敏审计，避免删除后失去谁在何时执行过清理的线索。
    connection.execute(
        """
        CREATE TABLE knowledge_bases (
            knowledge_base_id TEXT PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (
                status IN ('empty', 'indexing', 'ready', 'partial_failure', 'failed', 'deleting', 'deleted')
            ),
            default_index_profile_id TEXT NOT NULL,
            active_index_generation INTEGER NOT NULL DEFAULT 0 CHECK (active_index_generation >= 0),
            active_document_version_count INTEGER NOT NULL DEFAULT 0
                CHECK (active_document_version_count >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (default_index_profile_id) REFERENCES knowledge_index_profiles(profile_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_knowledge_bases_list
        ON knowledge_bases(status, updated_at DESC, created_at DESC)
        """
    )

    # 审计表不设置 knowledge_bases 外键：资料库在最终清理阶段可能移除业务记录，但脱敏
    # 事件仍需保留。details_json 只允许存稳定 ID、计数和 profile，不允许写入客户原文。
    connection.execute(
        """
        CREATE TABLE knowledge_audit_events (
            event_id TEXT PRIMARY KEY,
            knowledge_base_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_knowledge_audit_events_base_time
        ON knowledge_audit_events(knowledge_base_id, created_at DESC, event_id DESC)
        """
    )


def _apply_knowledge_document_versions_v1(connection: sqlite3.Connection) -> None:
    """建立 K1.2 的逻辑文档与不可变受控副本版本事实表。"""

    # display_name 在同一资料库内是稳定的逻辑文档身份。客户用同名 workspace 文件重新导入
    # 时创建新 version，而不是把同一份文件拆成多个无法追踪的“同名资料”。
    connection.execute(
        """
        CREATE TABLE knowledge_documents (
            document_id TEXT PRIMARY KEY,
            knowledge_base_id TEXT NOT NULL,
            display_name TEXT NOT NULL COLLATE NOCASE,
            document_type TEXT NOT NULL CHECK (document_type IN ('text', 'pdf', 'docx', 'image')),
            active_version_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (knowledge_base_id, display_name),
            FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_knowledge_documents_base_updated
        ON knowledge_documents(knowledge_base_id, updated_at DESC, document_id DESC)
        """
    )

    # storage_ref 是随机不透明标识，实际文件路径由内部服务从 knowledge_base_id + storage_ref
    # 推导。版本表不保存 workspace 绝对路径，源文件永远不会被移动、覆盖或删除。
    connection.execute(
        """
        CREATE TABLE knowledge_document_versions (
            document_version_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            knowledge_base_id TEXT NOT NULL,
            version_number INTEGER NOT NULL CHECK (version_number >= 1),
            storage_ref TEXT NOT NULL UNIQUE,
            source_sha256 TEXT NOT NULL,
            document_type TEXT NOT NULL CHECK (document_type IN ('text', 'pdf', 'docx', 'image')),
            parser_profile_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'queued', 'parsing', 'parsed', 'indexing', 'ready', 'partial_failure',
                    'failed', 'superseded', 'deleting', 'deleted'
                )
            ),
            extracted_char_count INTEGER NOT NULL DEFAULT 0 CHECK (extracted_char_count >= 0),
            parent_chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (parent_chunk_count >= 0),
            child_chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (child_chunk_count >= 0),
            failure_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (document_id, version_number),
            UNIQUE (document_id, source_sha256),
            FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
            FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_knowledge_document_versions_document_version
        ON knowledge_document_versions(document_id, version_number DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_knowledge_document_versions_base_status
        ON knowledge_document_versions(knowledge_base_id, status, updated_at DESC)
        """
    )


def _apply_knowledge_chunks_v1(connection: sqlite3.Connection) -> None:
    """建立 K1.3 的来源可追溯父子分块事实表。"""

    # K1.2 的 document_type 只区分 text/pdf/docx，不能无歧义还原 TXT 与 Markdown 的实际
    # 文件后缀。K1.3 保存受控副本后缀；已有早期候选仍可由 Repository 在私有 sources 目录
    # 中严格回读一次并补齐，而不会向客户端泄露物理路径。
    connection.execute(
        """
        ALTER TABLE knowledge_document_versions
        ADD COLUMN storage_suffix TEXT NOT NULL DEFAULT ''
        """
    )

    # 父块保持章节/页/段落等足量上下文；正文只存在受控 SQLite 内容列，不进入 audit、任务
    # 日志或普通列表接口。heading_path_json 保存稳定结构路径，之后引用与 Map-Reduce 可复用。
    connection.execute(
        """
        CREATE TABLE knowledge_parent_chunks (
            parent_chunk_id TEXT PRIMARY KEY,
            document_version_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            knowledge_base_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            heading_path_json TEXT NOT NULL DEFAULT '[]',
            source_kind TEXT NOT NULL CHECK (source_kind IN ('line', 'page', 'paragraph', 'table', 'region', 'mixed')),
            source_locator TEXT NOT NULL,
            start_char INTEGER NOT NULL CHECK (start_char >= 0),
            end_char INTEGER NOT NULL CHECK (end_char > start_char),
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            splitter_profile_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (document_version_id, ordinal),
            FOREIGN KEY (document_version_id) REFERENCES knowledge_document_versions(document_version_id)
                ON DELETE CASCADE,
            FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
            FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_knowledge_parent_chunks_version_ordinal
        ON knowledge_parent_chunks(document_version_id, ordinal)
        """
    )

    # 子块服务精确命中；前后指针以稳定 ID 记录，后续检索在命中子块时可扩展有限邻接上下文
    # 而不扫描整篇正文。指针逻辑由同一批写入一次性生成，属于可确定回读的事实。
    connection.execute(
        """
        CREATE TABLE knowledge_child_chunks (
            child_chunk_id TEXT PRIMARY KEY,
            parent_chunk_id TEXT NOT NULL,
            document_version_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            knowledge_base_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            previous_child_chunk_id TEXT NOT NULL DEFAULT '',
            next_child_chunk_id TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL CHECK (source_kind IN ('line', 'page', 'paragraph', 'table', 'region', 'mixed')),
            source_locator TEXT NOT NULL,
            start_char INTEGER NOT NULL CHECK (start_char >= 0),
            end_char INTEGER NOT NULL CHECK (end_char > start_char),
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            splitter_profile_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (document_version_id, ordinal),
            FOREIGN KEY (parent_chunk_id) REFERENCES knowledge_parent_chunks(parent_chunk_id)
                ON DELETE CASCADE,
            FOREIGN KEY (document_version_id) REFERENCES knowledge_document_versions(document_version_id)
                ON DELETE CASCADE,
            FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
            FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_knowledge_child_chunks_version_ordinal
        ON knowledge_child_chunks(document_version_id, ordinal)
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_knowledge_child_chunks_parent_ordinal
        ON knowledge_child_chunks(parent_chunk_id, ordinal)
        """
    )


def _apply_knowledge_keyword_jobs_v1(connection: sqlite3.Connection) -> None:
    """建立 K1.4 的可恢复关键词索引 generation 与后台任务事实表。"""

    connection.execute(
        """
        CREATE TABLE knowledge_index_generations (
            index_generation_id TEXT PRIMARY KEY,
            knowledge_base_id TEXT NOT NULL,
            generation_number INTEGER NOT NULL CHECK (generation_number >= 1),
            status TEXT NOT NULL CHECK (status IN ('building', 'ready', 'superseded', 'deleting', 'deleted', 'failed')),
            index_profile_json TEXT NOT NULL,
            keyword_index_mode TEXT NOT NULL CHECK (keyword_index_mode IN ('fts5_cjk', 'disabled')),
            vector_index_mode TEXT NOT NULL CHECK (vector_index_mode IN ('pending', 'ready', 'disabled', 'failed')),
            failure_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            activated_at TEXT NOT NULL DEFAULT '',
            UNIQUE (knowledge_base_id, generation_number),
            FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE knowledge_generation_documents (
            index_generation_id TEXT NOT NULL,
            document_version_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            PRIMARY KEY (index_generation_id, document_version_id),
            UNIQUE (index_generation_id, ordinal),
            FOREIGN KEY (index_generation_id) REFERENCES knowledge_index_generations(index_generation_id)
                ON DELETE CASCADE,
            FOREIGN KEY (document_version_id) REFERENCES knowledge_document_versions(document_version_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_knowledge_generation_documents_version
        ON knowledge_generation_documents(document_version_id, index_generation_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE knowledge_index_jobs (
            index_job_id TEXT PRIMARY KEY,
            knowledge_base_id TEXT NOT NULL,
            index_generation_id TEXT NOT NULL,
            target_generation_number INTEGER NOT NULL CHECK (target_generation_number >= 1),
            status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'partial_failure', 'failed', 'cancelled')),
            stage TEXT NOT NULL CHECK (stage IN ('queued', 'parsing', 'ocr_recognizing', 'chunking', 'keyword_indexing', 'vector_indexing', 'verifying', 'activating', 'completed', 'partial_failure', 'failed', 'cancelled')),
            total_document_count INTEGER NOT NULL CHECK (total_document_count >= 1),
            parsed_document_count INTEGER NOT NULL DEFAULT 0 CHECK (parsed_document_count >= 0),
            indexed_document_count INTEGER NOT NULL DEFAULT 0 CHECK (indexed_document_count >= 0),
            failed_document_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_document_count >= 0),
            reused_parsed_document_count INTEGER NOT NULL DEFAULT 0 CHECK (reused_parsed_document_count >= 0),
            parse_and_chunk_elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK (parse_and_chunk_elapsed_ms >= 0),
            vector_index_elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK (vector_index_elapsed_ms >= 0),
            keyword_index_elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK (keyword_index_elapsed_ms >= 0),
            total_elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK (total_elapsed_ms >= 0),
            vector_indexed_child_count INTEGER NOT NULL DEFAULT 0 CHECK (vector_indexed_child_count >= 0),
            reused_vector_child_count INTEGER NOT NULL DEFAULT 0 CHECK (reused_vector_child_count >= 0),
            embedded_child_count INTEGER NOT NULL DEFAULT 0 CHECK (embedded_child_count >= 0),
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            failure_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            UNIQUE (knowledge_base_id, target_generation_number),
            FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id),
            FOREIGN KEY (index_generation_id) REFERENCES knowledge_index_generations(index_generation_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_knowledge_index_jobs_base_status
        ON knowledge_index_jobs(knowledge_base_id, status, updated_at DESC)
        """
    )

    # FTS 是按 generation 隔离的可重建派生索引。正文事实仍在 child_chunks；FTS 损坏时可以
    # 从已经解析的版本重建，不能反过来作为唯一原文来源。
    connection.execute(
        """
        CREATE VIRTUAL TABLE knowledge_child_chunks_fts USING fts5(
            child_chunk_id UNINDEXED,
            knowledge_base_id UNINDEXED,
            index_generation_id UNINDEXED,
            content,
            cjk_shadow,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )


def _apply_knowledge_index_job_metrics_v1(connection: sqlite3.Connection) -> None:
    """为既有 K1.4 索引任务补充无正文阶段耗时与解析复用事实。

    新安装会由 ``_create_tables`` 直接创建完整字段；已有资料库则经这条前向 migration
    增列。逐列检查让两种路径都保持幂等，也不会重建任何客户索引或读取受控材料。
    """

    existing_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(knowledge_index_jobs)").fetchall()
    }
    columns = (
        ("reused_parsed_document_count", "INTEGER NOT NULL DEFAULT 0 CHECK (reused_parsed_document_count >= 0)"),
        ("parse_and_chunk_elapsed_ms", "INTEGER NOT NULL DEFAULT 0 CHECK (parse_and_chunk_elapsed_ms >= 0)"),
        ("vector_index_elapsed_ms", "INTEGER NOT NULL DEFAULT 0 CHECK (vector_index_elapsed_ms >= 0)"),
        ("keyword_index_elapsed_ms", "INTEGER NOT NULL DEFAULT 0 CHECK (keyword_index_elapsed_ms >= 0)"),
        ("total_elapsed_ms", "INTEGER NOT NULL DEFAULT 0 CHECK (total_elapsed_ms >= 0)"),
    )
    for name, definition in columns:
        if name not in existing_columns:
            connection.execute(f"ALTER TABLE knowledge_index_jobs ADD COLUMN {name} {definition}")


def _apply_knowledge_index_vector_reuse_metrics_v1(connection: sqlite3.Connection) -> None:
    """为既有索引任务补充 K5.6 向量复用计量，不保存任何向量值。

    旧任务没有向量复用事实，因此零值表示“历史任务未采用这条 K5.6 路径”，不能被 UI
    解读为语义索引一定未建成。迁移只增列，既不读取资料，也不扫描或复制 Chroma 目录。
    """

    existing_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(knowledge_index_jobs)").fetchall()
    }
    columns = (
        ("vector_indexed_child_count", "INTEGER NOT NULL DEFAULT 0 CHECK (vector_indexed_child_count >= 0)"),
        ("reused_vector_child_count", "INTEGER NOT NULL DEFAULT 0 CHECK (reused_vector_child_count >= 0)"),
        ("embedded_child_count", "INTEGER NOT NULL DEFAULT 0 CHECK (embedded_child_count >= 0)"),
    )
    for name, definition in columns:
        if name not in existing_columns:
            connection.execute(f"ALTER TABLE knowledge_index_jobs ADD COLUMN {name} {definition}")


def _apply_knowledge_ocr_contract_v1(connection: sqlite3.Connection) -> None:
    """扩展既有知识库的文档/来源枚举，保留所有版本、分块与 generation 事实。

    SQLite 不能直接修改旧表的 CHECK 约束。这里采用受控的“新表复制 -> 反向依赖顺序替换”
    迁移，并由 migration runner 在提交前执行 ``foreign_key_check``。它不读取源文件、不重建
    索引、不重新 OCR，也不改变任何既有 document/version/chunk ID。
    """

    suffix = "_ocr_contract_new"
    _create_knowledge_ocr_contract_tables(connection, suffix=suffix)

    # 保持 stable ID 与全部业务字段不变。新表只放宽 image/region 两个受控枚举值，不能借
    # 这条 schema migration 改写客户正文、来源坐标或已有 index generation。
    connection.execute(
        f"""
        INSERT INTO knowledge_documents{suffix} (
            document_id, knowledge_base_id, display_name, document_type, active_version_id,
            created_at, updated_at
        )
        SELECT document_id, knowledge_base_id, display_name, document_type, active_version_id,
               created_at, updated_at
        FROM knowledge_documents
        """
    )
    connection.execute(
        f"""
        INSERT INTO knowledge_document_versions{suffix} (
            document_version_id, document_id, knowledge_base_id, version_number, storage_ref,
            storage_suffix, source_sha256, document_type, parser_profile_version, status,
            extracted_char_count, parent_chunk_count, child_chunk_count, failure_summary,
            created_at, updated_at
        )
        SELECT document_version_id, document_id, knowledge_base_id, version_number, storage_ref,
               storage_suffix, source_sha256, document_type, parser_profile_version, status,
               extracted_char_count, parent_chunk_count, child_chunk_count, failure_summary,
               created_at, updated_at
        FROM knowledge_document_versions
        """
    )
    connection.execute(
        f"""
        INSERT INTO knowledge_parent_chunks{suffix} (
            parent_chunk_id, document_version_id, document_id, knowledge_base_id, ordinal,
            heading_path_json, source_kind, source_locator, start_char, end_char, content,
            content_sha256, splitter_profile_version, created_at
        )
        SELECT parent_chunk_id, document_version_id, document_id, knowledge_base_id, ordinal,
               heading_path_json, source_kind, source_locator, start_char, end_char, content,
               content_sha256, splitter_profile_version, created_at
        FROM knowledge_parent_chunks
        """
    )
    connection.execute(
        f"""
        INSERT INTO knowledge_child_chunks{suffix} (
            child_chunk_id, parent_chunk_id, document_version_id, document_id, knowledge_base_id,
            ordinal, previous_child_chunk_id, next_child_chunk_id, source_kind, source_locator,
            start_char, end_char, content, content_sha256, splitter_profile_version, created_at
        )
        SELECT child_chunk_id, parent_chunk_id, document_version_id, document_id, knowledge_base_id,
               ordinal, previous_child_chunk_id, next_child_chunk_id, source_kind, source_locator,
               start_char, end_char, content, content_sha256, splitter_profile_version, created_at
        FROM knowledge_child_chunks
        """
    )
    connection.execute(
        f"""
        INSERT INTO knowledge_generation_documents{suffix} (
            index_generation_id, document_version_id, ordinal
        )
        SELECT index_generation_id, document_version_id, ordinal
        FROM knowledge_generation_documents
        """
    )

    # 先拆最末端引用，再替换父表。FTS 是可重建派生索引，未声明外键且只持有 stable child ID，
    # 因此不搬运其全文副本，也不会在这条契约迁移中放大 I/O。
    connection.execute("DROP TABLE knowledge_generation_documents")
    connection.execute("DROP TABLE knowledge_child_chunks")
    connection.execute("DROP TABLE knowledge_parent_chunks")
    connection.execute("DROP TABLE knowledge_document_versions")
    connection.execute("DROP TABLE knowledge_documents")

    connection.execute(f"ALTER TABLE knowledge_documents{suffix} RENAME TO knowledge_documents")
    connection.execute(
        f"ALTER TABLE knowledge_document_versions{suffix} RENAME TO knowledge_document_versions"
    )
    connection.execute(
        f"ALTER TABLE knowledge_parent_chunks{suffix} RENAME TO knowledge_parent_chunks"
    )
    connection.execute(
        f"ALTER TABLE knowledge_child_chunks{suffix} RENAME TO knowledge_child_chunks"
    )
    connection.execute(
        f"ALTER TABLE knowledge_generation_documents{suffix} RENAME TO knowledge_generation_documents"
    )
    _create_knowledge_ocr_contract_indexes(connection)


def _apply_knowledge_ocr_page_metrics_v1(connection: sqlite3.Connection) -> None:
    """为 K7.4.2 保存不含正文的 OCR 页级结果统计。

    这条迁移只扩展受控版本的处理状态：页数、成功/失败数和本轮自动重试数。它不扫描私有
    副本、不触发 OCR、不写图片或文字；旧版本零值表示“历史版本没有采用 K7.4.2 页级统计”。
    """

    existing_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(knowledge_document_versions)").fetchall()
    }
    columns = (
        ("ocr_page_count", "INTEGER NOT NULL DEFAULT 0 CHECK (ocr_page_count >= 0)"),
        (
            "ocr_completed_page_count",
            "INTEGER NOT NULL DEFAULT 0 CHECK (ocr_completed_page_count >= 0)",
        ),
        (
            "ocr_failed_page_count",
            "INTEGER NOT NULL DEFAULT 0 CHECK (ocr_failed_page_count >= 0)",
        ),
        (
            "ocr_retried_page_count",
            "INTEGER NOT NULL DEFAULT 0 CHECK (ocr_retried_page_count >= 0)",
        ),
    )
    for name, definition in columns:
        if name not in existing_columns:
            connection.execute(f"ALTER TABLE knowledge_document_versions ADD COLUMN {name} {definition}")


def _apply_knowledge_ocr_job_stage_v1(connection: sqlite3.Connection) -> None:
    """将既有索引任务的阶段枚举扩展为真实的 OCR 识别阶段。

    SQLite 不能直接变更 ``CHECK``，因此只重建这一张不含客户正文的任务事实表。所有稳定任务
    ID、generation 外键、性能计量和失败摘要都会原样复制；调用方不能把这条 migration 当成
    重新索引、重新 OCR 或恢复旧运行任务的入口。
    """

    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_index_jobs'"
    ).fetchone()
    definition_sql = (
        str(definition_row["sql"] or "")
        if isinstance(definition_row, sqlite3.Row)
        else str(definition_row[0] or "") if definition_row is not None else ""
    )
    if "ocr_recognizing" in definition_sql:
        return

    replacement = "knowledge_index_jobs_ocr_stage_new"
    connection.execute(
        f"""
        CREATE TABLE {replacement} (
            index_job_id TEXT PRIMARY KEY,
            knowledge_base_id TEXT NOT NULL,
            index_generation_id TEXT NOT NULL,
            target_generation_number INTEGER NOT NULL CHECK (target_generation_number >= 1),
            status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'partial_failure', 'failed', 'cancelled')),
            stage TEXT NOT NULL CHECK (stage IN ('queued', 'parsing', 'ocr_recognizing', 'chunking', 'keyword_indexing', 'vector_indexing', 'verifying', 'activating', 'completed', 'partial_failure', 'failed', 'cancelled')),
            total_document_count INTEGER NOT NULL CHECK (total_document_count >= 1),
            parsed_document_count INTEGER NOT NULL DEFAULT 0 CHECK (parsed_document_count >= 0),
            indexed_document_count INTEGER NOT NULL DEFAULT 0 CHECK (indexed_document_count >= 0),
            failed_document_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_document_count >= 0),
            reused_parsed_document_count INTEGER NOT NULL DEFAULT 0 CHECK (reused_parsed_document_count >= 0),
            parse_and_chunk_elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK (parse_and_chunk_elapsed_ms >= 0),
            vector_index_elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK (vector_index_elapsed_ms >= 0),
            keyword_index_elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK (keyword_index_elapsed_ms >= 0),
            total_elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK (total_elapsed_ms >= 0),
            vector_indexed_child_count INTEGER NOT NULL DEFAULT 0 CHECK (vector_indexed_child_count >= 0),
            reused_vector_child_count INTEGER NOT NULL DEFAULT 0 CHECK (reused_vector_child_count >= 0),
            embedded_child_count INTEGER NOT NULL DEFAULT 0 CHECK (embedded_child_count >= 0),
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            failure_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            UNIQUE (knowledge_base_id, target_generation_number),
            FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id),
            FOREIGN KEY (index_generation_id) REFERENCES knowledge_index_generations(index_generation_id)
        )
        """
    )
    columns = (
        "index_job_id, knowledge_base_id, index_generation_id, target_generation_number, status, stage, "
        "total_document_count, parsed_document_count, indexed_document_count, failed_document_count, "
        "reused_parsed_document_count, parse_and_chunk_elapsed_ms, vector_index_elapsed_ms, "
        "keyword_index_elapsed_ms, total_elapsed_ms, vector_indexed_child_count, "
        "reused_vector_child_count, embedded_child_count, cancel_requested, failure_summary, created_at, "
        "updated_at, started_at, completed_at"
    )
    connection.execute(
        f"INSERT INTO {replacement} ({columns}) SELECT {columns} FROM knowledge_index_jobs"
    )
    connection.execute("DROP TABLE knowledge_index_jobs")
    connection.execute(f"ALTER TABLE {replacement} RENAME TO knowledge_index_jobs")
    connection.execute(
        "CREATE INDEX idx_knowledge_index_jobs_base_status "
        "ON knowledge_index_jobs(knowledge_base_id, status, updated_at DESC)"
    )


def _apply_knowledge_import_state_repair_v1(connection: sqlite3.Connection) -> None:
    """修复旧版本把“仅导入”误标为 indexing 的资料库状态。

    只处理没有活动 generation 且不存在 queued/running Job 的记录，因此不会中断真实索引，
    也不会改动已有可用 generation。导入后的候选版本仍保留在原表中等待客户明确建立索引。
    """

    connection.execute(
        """
        UPDATE knowledge_bases
        SET status = 'empty'
        WHERE status = 'indexing'
          AND active_index_generation = 0
          AND NOT EXISTS (
              SELECT 1
              FROM knowledge_index_jobs AS job
              WHERE job.knowledge_base_id = knowledge_bases.knowledge_base_id
                AND job.status IN ('queued', 'running')
          )
        """
    )


def _create_knowledge_ocr_contract_tables(connection: sqlite3.Connection, *, suffix: str) -> None:
    """创建 K7 迁移期间的影子表；``suffix`` 仅由模块常量传入。"""

    connection.execute(
        f"""
        CREATE TABLE knowledge_documents{suffix} (
            document_id TEXT PRIMARY KEY,
            knowledge_base_id TEXT NOT NULL,
            display_name TEXT NOT NULL COLLATE NOCASE,
            document_type TEXT NOT NULL CHECK (document_type IN ('text', 'pdf', 'docx', 'image')),
            active_version_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (knowledge_base_id, display_name),
            FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE knowledge_document_versions{suffix} (
            document_version_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            knowledge_base_id TEXT NOT NULL,
            version_number INTEGER NOT NULL CHECK (version_number >= 1),
            storage_ref TEXT NOT NULL UNIQUE,
            storage_suffix TEXT NOT NULL DEFAULT '',
            source_sha256 TEXT NOT NULL,
            document_type TEXT NOT NULL CHECK (document_type IN ('text', 'pdf', 'docx', 'image')),
            parser_profile_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'queued', 'parsing', 'parsed', 'indexing', 'ready', 'partial_failure',
                    'failed', 'superseded', 'deleting', 'deleted'
                )
            ),
            extracted_char_count INTEGER NOT NULL DEFAULT 0 CHECK (extracted_char_count >= 0),
            parent_chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (parent_chunk_count >= 0),
            child_chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (child_chunk_count >= 0),
            failure_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (document_id, version_number),
            UNIQUE (document_id, source_sha256),
            FOREIGN KEY (document_id) REFERENCES knowledge_documents{suffix}(document_id)
                ON DELETE CASCADE,
            FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE knowledge_parent_chunks{suffix} (
            parent_chunk_id TEXT PRIMARY KEY,
            document_version_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            knowledge_base_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            heading_path_json TEXT NOT NULL DEFAULT '[]',
            source_kind TEXT NOT NULL CHECK (
                source_kind IN ('line', 'page', 'paragraph', 'table', 'region', 'mixed')
            ),
            source_locator TEXT NOT NULL,
            start_char INTEGER NOT NULL CHECK (start_char >= 0),
            end_char INTEGER NOT NULL CHECK (end_char > start_char),
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            splitter_profile_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (document_version_id, ordinal),
            FOREIGN KEY (document_version_id)
                REFERENCES knowledge_document_versions{suffix}(document_version_id) ON DELETE CASCADE,
            FOREIGN KEY (document_id) REFERENCES knowledge_documents{suffix}(document_id) ON DELETE CASCADE,
            FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE knowledge_child_chunks{suffix} (
            child_chunk_id TEXT PRIMARY KEY,
            parent_chunk_id TEXT NOT NULL,
            document_version_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            knowledge_base_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            previous_child_chunk_id TEXT NOT NULL DEFAULT '',
            next_child_chunk_id TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL CHECK (
                source_kind IN ('line', 'page', 'paragraph', 'table', 'region', 'mixed')
            ),
            source_locator TEXT NOT NULL,
            start_char INTEGER NOT NULL CHECK (start_char >= 0),
            end_char INTEGER NOT NULL CHECK (end_char > start_char),
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            splitter_profile_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (document_version_id, ordinal),
            FOREIGN KEY (parent_chunk_id) REFERENCES knowledge_parent_chunks{suffix}(parent_chunk_id)
                ON DELETE CASCADE,
            FOREIGN KEY (document_version_id)
                REFERENCES knowledge_document_versions{suffix}(document_version_id) ON DELETE CASCADE,
            FOREIGN KEY (document_id) REFERENCES knowledge_documents{suffix}(document_id) ON DELETE CASCADE,
            FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE knowledge_generation_documents{suffix} (
            index_generation_id TEXT NOT NULL,
            document_version_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            PRIMARY KEY (index_generation_id, document_version_id),
            UNIQUE (index_generation_id, ordinal),
            FOREIGN KEY (index_generation_id)
                REFERENCES knowledge_index_generations(index_generation_id) ON DELETE CASCADE,
            FOREIGN KEY (document_version_id)
                REFERENCES knowledge_document_versions{suffix}(document_version_id) ON DELETE CASCADE
        )
        """
    )


def _create_knowledge_ocr_contract_indexes(connection: sqlite3.Connection) -> None:
    """重建被替换表的查询索引；索引名保持不变，调用方无需知道 K7 迁移。"""

    connection.execute(
        "CREATE INDEX idx_knowledge_documents_base_updated "
        "ON knowledge_documents(knowledge_base_id, updated_at DESC, document_id DESC)"
    )
    connection.execute(
        "CREATE INDEX idx_knowledge_document_versions_document_version "
        "ON knowledge_document_versions(document_id, version_number DESC)"
    )
    connection.execute(
        "CREATE INDEX idx_knowledge_document_versions_base_status "
        "ON knowledge_document_versions(knowledge_base_id, status, updated_at DESC)"
    )
    connection.execute(
        "CREATE INDEX idx_knowledge_parent_chunks_version_ordinal "
        "ON knowledge_parent_chunks(document_version_id, ordinal)"
    )
    connection.execute(
        "CREATE INDEX idx_knowledge_child_chunks_version_ordinal "
        "ON knowledge_child_chunks(document_version_id, ordinal)"
    )
    connection.execute(
        "CREATE INDEX idx_knowledge_child_chunks_parent_ordinal "
        "ON knowledge_child_chunks(parent_chunk_id, ordinal)"
    )
    connection.execute(
        "CREATE INDEX idx_knowledge_generation_documents_version "
        "ON knowledge_generation_documents(document_version_id, index_generation_id)"
    )


def _apply_commander_conversation_memory_v1(connection: sqlite3.Connection) -> None:
    """建立 C6.2 自动会话层，只存受控摘要、近轮消息和材料引用。

    与 `long_term_memories` 不同，这里的内容不代表永久偏好：会话只服务有限连续对话。
    文件正文、表格原始行、绝对路径、凭据和模型隐藏推理都不属于这个表的合法内容；服务层
    会在写入前脱敏并截断，SQLite 只保存已经收束的文本。
    """

    connection.executescript(
        """
        CREATE TABLE commander_conversations (
            conversation_id TEXT PRIMARY KEY,
            project_scope TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            material_bindings_json TEXT NOT NULL DEFAULT '[]',
            last_task_id TEXT NOT NULL DEFAULT '',
            last_plan_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX idx_commander_conversations_scope_updated
        ON commander_conversations(project_scope, updated_at DESC, conversation_id DESC);

        CREATE TABLE commander_conversation_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            task_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (conversation_id)
                REFERENCES commander_conversations(conversation_id) ON DELETE CASCADE
        );

        CREATE INDEX idx_commander_conversation_messages_recent
        ON commander_conversation_messages(conversation_id, created_at ASC, message_id ASC);
        """
    )


def _apply_commander_conversation_archive_v1(connection: sqlite3.Connection) -> None:
    """把 C6.2 的有限近轮存储升级为完整归档，不改变模型上下文的窗口上限。

    旧版本已经删除的正文无法恢复。本迁移只给旧会话标记一个保守的摘要水位，避免下一轮
    保存时把仍在表里的近轮重复写进摘要；迁移后新增的消息不再因超过窗口而删除。
    """

    connection.execute(
        "ALTER TABLE commander_conversations "
        "ADD COLUMN summary_message_count INTEGER NOT NULL DEFAULT 0"
    )
    connection.execute(
        """
        UPDATE commander_conversations
        SET summary_message_count = (
            SELECT COUNT(*)
            FROM commander_conversation_messages AS message
            WHERE message.conversation_id = commander_conversations.conversation_id
        )
        WHERE summary <> '' AND summary_message_count = 0
        """
    )


_SCHEMA_MIGRATIONS: tuple[_SchemaMigration, ...] = (
    _SchemaMigration(
        migration_id="20260821_knowledge_foundation_v1",
        signature=(
            "knowledge_index_profiles:v1;knowledge_bases:v1;knowledge_audit_events:v1;"
            "sqlite_facts_only;no_documents_or_chunks"
        ),
        apply=_apply_knowledge_foundation_v1,
    ),
    _SchemaMigration(
        migration_id="20260821_knowledge_document_versions_v1",
        signature=(
            "knowledge_documents:v1;knowledge_document_versions:v1;"
            "controlled_copy_metadata_only;no_chunks_or_indexes"
        ),
        apply=_apply_knowledge_document_versions_v1,
    ),
    _SchemaMigration(
        migration_id="20260821_knowledge_chunks_v1",
        signature=(
            "knowledge_document_versions:storage_suffix_v1;knowledge_parent_chunks:v1;"
            "knowledge_child_chunks:v1;source_anchors_and_adjacency"
        ),
        apply=_apply_knowledge_chunks_v1,
    ),
    _SchemaMigration(
        migration_id="20260821_knowledge_keyword_jobs_v1",
        signature=(
            "knowledge_index_generations:v1;knowledge_generation_documents:v1;"
            "knowledge_index_jobs:v1;knowledge_child_chunks_fts:v1;keyword_generation_recovery"
        ),
        apply=_apply_knowledge_keyword_jobs_v1,
    ),
    _SchemaMigration(
        migration_id="20260825_knowledge_index_job_metrics_v1",
        signature=(
            "knowledge_index_jobs:v2;reused_parsed_document_count;"
            "parse_and_chunk_elapsed_ms;vector_index_elapsed_ms;"
            "keyword_index_elapsed_ms;total_elapsed_ms;no_raw_content"
        ),
        apply=_apply_knowledge_index_job_metrics_v1,
    ),
    _SchemaMigration(
        migration_id="20260825_knowledge_index_vector_reuse_metrics_v1",
        signature=(
            "knowledge_index_jobs:v3;vector_indexed_child_count;reused_vector_child_count;"
            "embedded_child_count;no_vectors_in_sqlite"
        ),
        apply=_apply_knowledge_index_vector_reuse_metrics_v1,
    ),
    _SchemaMigration(
        migration_id="20260825_knowledge_ocr_contract_v1",
        signature=(
            "knowledge_documents:v2;knowledge_document_versions:v2;"
            "knowledge_parent_chunks:v2;knowledge_child_chunks:v2;"
            "knowledge_generation_documents:v2;allow_image_document_and_region_anchor"
        ),
        apply=_apply_knowledge_ocr_contract_v1,
        requires_foreign_keys_disabled=True,
    ),
    _SchemaMigration(
        migration_id="20260825_knowledge_ocr_page_metrics_v1",
        signature=(
            "knowledge_document_versions:v3;ocr_page_count;ocr_completed_page_count;"
            "ocr_failed_page_count;ocr_retried_page_count;no_ocr_text_or_images"
        ),
        apply=_apply_knowledge_ocr_page_metrics_v1,
    ),
    _SchemaMigration(
        migration_id="20260825_knowledge_ocr_job_stage_v1",
        signature=(
            "knowledge_index_jobs:v4;allow_ocr_recognizing_stage;preserve_job_generation_metrics"
        ),
        apply=_apply_knowledge_ocr_job_stage_v1,
        requires_foreign_keys_disabled=True,
    ),
    _SchemaMigration(
        migration_id="20260826_knowledge_import_state_repair_v1",
        signature=(
            "knowledge_bases:stale_import_indexing_to_empty;"
            "only_without_active_generation_or_active_index_job"
        ),
        apply=_apply_knowledge_import_state_repair_v1,
    ),
    _SchemaMigration(
        migration_id="20260826_commander_conversation_memory_v1",
        signature=(
            "commander_conversations:v1;commander_conversation_messages:v1;"
            "bounded_session_summary_recent_turns_material_refs"
        ),
        apply=_apply_commander_conversation_memory_v1,
    ),
    _SchemaMigration(
        migration_id="20260826_commander_conversation_archive_v1",
        signature=(
            "commander_conversations:v2;summary_message_count;"
            "complete_sanitized_archive_bounded_prompt_context"
        ),
        apply=_apply_commander_conversation_archive_v1,
    ),
)
