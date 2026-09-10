from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from app.database.sqlite import get_connection
from app.schemas.chat import WorkflowMaterialBinding
from app.schemas.conversation import (
    ConversationContext,
    ConversationMessageRecord,
    ConversationSessionList,
    ConversationSessionRecord,
    ConversationTranscriptPage,
)


MAX_RECENT_MESSAGES = 20
RECENT_MESSAGE_TOKEN_BUDGET = 18_000
LEGACY_RECENT_MESSAGE_FALLBACK = 8
CONVERSATION_SUMMARY_MAX_LENGTH = 1400
CONVERSATION_TITLE_MAX_LENGTH = 42

_SUMMARY_CLAUSE_SPLIT_PATTERN = re.compile(r"[。！？!?；;\n]+")
_CONSTRAINT_SIGNAL_PATTERN = re.compile(
    r"(?:必须|不得|不能|不要|只能|只需|固定|统一|保持|预算|格式|范围|来源|要求|约束)"
)
_PENDING_SIGNAL_PATTERN = re.compile(r"(?:继续|接下来|下一步|还要|尚未|待办|待处理|之后再|稍后)")


def create_conversation(*, project_scope: str) -> ConversationSessionRecord:
    """创建一段空会话，调用方随后才会写入首条成功的问答。"""

    now = _utc_now()
    record = ConversationSessionRecord(
        conversation_id=f"conv_{uuid4().hex[:16]}",
        project_scope=project_scope,
        created_at=now,
        updated_at=now,
    )
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO commander_conversations (
                conversation_id, project_scope, title, summary, material_bindings_json,
                last_task_id, last_plan_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.conversation_id,
                record.project_scope,
                record.title,
                record.summary,
                "[]",
                record.last_task_id,
                record.last_plan_id,
                record.created_at,
                record.updated_at,
            ),
        )
    return record


def get_conversation(conversation_id: str) -> ConversationSessionRecord | None:
    """按稳定 ID 读取会话元数据；不隐式新建，方便上层处理范围切换。"""

    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM commander_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    return _row_to_session(row) if row is not None else None


def get_conversation_context(conversation_id: str) -> ConversationContext:
    """读取结构化摘要与受 token 预算约束的近轮原文。"""

    with get_connection() as connection:
        session_row = connection.execute(
            "SELECT * FROM commander_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if session_row is None:
            raise LookupError("未找到指定会话。")
        total = int(
            connection.execute(
                "SELECT COUNT(*) FROM commander_conversation_messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
        )
        summarized_count = min(_summary_message_count(session_row), total)
        unsummarized_count = max(0, total - summarized_count)
        # 迁移前的有限窗口可能被整体标成“已摘要”。这种旧会话仍回退显示最后 8 条；
        # 新写入会话始终至少保留一条未摘要原文，因此不会走这个兼容分支。
        row_limit = min(MAX_RECENT_MESSAGES, unsummarized_count)
        if row_limit == 0 and total > 0:
            row_limit = min(LEGACY_RECENT_MESSAGE_FALLBACK, total)
        rows = connection.execute(
            """
            SELECT * FROM (
                SELECT * FROM commander_conversation_messages
                WHERE conversation_id = ?
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
            )
            ORDER BY created_at ASC, message_id ASC
            """,
            (conversation_id, row_limit),
        ).fetchall()
    recent_messages = _fit_recent_messages([_row_to_message(row) for row in rows])
    session = _row_to_session(session_row)
    estimated_memory_tokens = estimate_conversation_tokens(session.summary) + sum(
        estimate_conversation_tokens(item.content) + 6 for item in recent_messages
    )
    return ConversationContext(
        session=session,
        recent_messages=recent_messages,
        summarized_message_count=summarized_count,
        estimated_memory_tokens=estimated_memory_tokens,
    )


def list_conversations(*, project_scope: str, limit: int = 40) -> ConversationSessionList:
    """列出同一 project scope 下最近更新的会话元数据，不传输客户聊天正文。"""

    bounded_limit = max(1, min(limit, 80))
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT conversation.*,
                   (SELECT COUNT(*)
                    FROM commander_conversation_messages AS message
                    WHERE message.conversation_id = conversation.conversation_id) AS archived_message_count
            FROM commander_conversations AS conversation
            WHERE project_scope = ?
            ORDER BY updated_at DESC, conversation_id DESC
            LIMIT ?
            """,
            (project_scope, bounded_limit),
        ).fetchall()
    return ConversationSessionList(
        project_scope=project_scope,
        conversations=[_row_to_session(row) for row in rows],
    )


def get_conversation_transcript(
    *,
    conversation_id: str,
    project_scope: str,
    offset: int = 0,
    limit: int = 40,
) -> ConversationTranscriptPage:
    """按页返回完整脱敏会话归档，并拒绝跨 project scope 的读取。"""

    bounded_offset = max(0, offset)
    bounded_limit = max(1, min(limit, 100))
    with get_connection() as connection:
        session_row = connection.execute(
            """
            SELECT conversation.*,
                   (SELECT COUNT(*)
                    FROM commander_conversation_messages AS message
                    WHERE message.conversation_id = conversation.conversation_id) AS archived_message_count
            FROM commander_conversations AS conversation
            WHERE conversation_id = ? AND project_scope = ?
            """,
            (conversation_id, project_scope),
        ).fetchone()
        if session_row is None:
            raise LookupError("未找到指定会话。")
        total = connection.execute(
            "SELECT COUNT(*) FROM commander_conversation_messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        rows = connection.execute(
            """
            SELECT * FROM commander_conversation_messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC, message_id ASC
            LIMIT ? OFFSET ?
            """,
            (conversation_id, bounded_limit, bounded_offset),
        ).fetchall()
    return ConversationTranscriptPage(
        session=_row_to_session(session_row),
        offset=bounded_offset,
        limit=bounded_limit,
        total=int(total),
        messages=[_row_to_message(row) for row in rows],
    )


def save_conversation_turn(
    *,
    conversation_id: str,
    user_message: str,
    assistant_message: str,
    material_bindings: list[WorkflowMaterialBinding],
    task_id: str,
    plan_id: str,
) -> ConversationContext:
    """原子保存一轮成功对话，并把超出模型窗口的旧消息压成短摘要。

    只在 `/api/chat` 已拿到有效回复后调用。这样模型请求失败、网络中断或输出契约失败不会
    把一条“看似已经答复”的用户输入写进连续会话，也不会污染下一次指代解析。
    """

    now = _utc_now()
    # 同一轮的两条消息共享时间戳，故再用同一 turn_id 的有序后缀固定“用户 -> 助手”顺序。
    # 不能依赖两个随机 UUID 的字典序，否则极端情况下 Prompt 与重启后的对话窗会把角色颠倒。
    turn_id = uuid4().hex[:16]
    with get_connection() as connection:
        session_row = connection.execute(
            "SELECT * FROM commander_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if session_row is None:
            raise LookupError("未找到指定会话。")
        session = _row_to_session(session_row)
        connection.executemany(
            """
            INSERT INTO commander_conversation_messages (
                message_id, conversation_id, role, content, task_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (f"turn_{turn_id}_1", conversation_id, "user", user_message, task_id, now),
                (f"turn_{turn_id}_2", conversation_id, "assistant", assistant_message, task_id, now),
            ),
        )

        summary, summary_message_count = _compact_conversation_window(
            connection=connection,
            conversation_id=conversation_id,
            session_row=session_row,
        )

        # 完整归档保留在 SQLite；模型上下文由 get_conversation_context() 单独取最后有限消息。
        # 不能再 DELETE 早期消息，否则客户切换会话后无法像正常聊天产品一样回看历史。
        bindings_json = (
            json.dumps([item.model_dump(mode="json") for item in material_bindings], ensure_ascii=False)
            if material_bindings
            else json.dumps([item.model_dump(mode="json") for item in session.material_bindings], ensure_ascii=False)
        )
        title = session.title or _title_from_message(user_message)
        connection.execute(
            """
            UPDATE commander_conversations
            SET title = ?, summary = ?, summary_message_count = ?, material_bindings_json = ?,
                last_task_id = ?, last_plan_id = ?, updated_at = ?
            WHERE conversation_id = ?
            """,
            (
                title,
                summary,
                summary_message_count,
                bindings_json,
                task_id,
                plan_id,
                now,
                conversation_id,
            ),
        )

    return get_conversation_context(conversation_id)


def append_conversation_assistant_delivery(
    *,
    conversation_id: str,
    task_id: str,
    assistant_message: str,
) -> ConversationContext:
    """追加异步任务的最终客户交付，并以子任务 ID 保证恢复/重试时不重复写入。

    普通聊天在同一 HTTP 请求内保存“用户 -> 助手”成对消息；知识库问答等安全 Runtime 则在
    后台完成。此处只接收已经由服务层脱敏且受长度限制的最终正文，不让任务日志、Prompt 或
    任意客户端文本混入会话档案。
    """

    now = _utc_now()
    message_id = f"delivery_{task_id}"
    with get_connection() as connection:
        session_row = connection.execute(
            "SELECT * FROM commander_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if session_row is None:
            raise LookupError("未找到指定会话。")

        # 同一个 Runtime 可能在进程恢复后再次走到完成收束；稳定 message_id 使最终回答只出现一次。
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO commander_conversation_messages (
                message_id, conversation_id, role, content, task_id, created_at
            ) VALUES (?, ?, 'assistant', ?, ?, ?)
            """,
            (message_id, conversation_id, assistant_message, task_id, now),
        ).rowcount
        if inserted:
            summary, summary_message_count = _compact_conversation_window(
                connection=connection,
                conversation_id=conversation_id,
                session_row=session_row,
            )
            connection.execute(
                """
                UPDATE commander_conversations
                SET summary = ?, summary_message_count = ?, last_task_id = ?, updated_at = ?
                WHERE conversation_id = ?
                """,
                (summary, summary_message_count, task_id, now, conversation_id),
            )

    return get_conversation_context(conversation_id)


def estimate_conversation_tokens(value: str) -> int:
    """给多供应商会话窗口使用的保守 token 估算，不冒充 Provider 账单。"""

    if not value:
        return 0
    wide_characters = sum(1 for character in value if ord(character) > 127)
    ascii_characters = len(value) - wide_characters
    return wide_characters + (ascii_characters + 2) // 3


def _fit_recent_messages(messages: list[ConversationMessageRecord]) -> list[ConversationMessageRecord]:
    """从最新消息向前装箱，同时受消息数量和近轮 token 预算双重约束。"""

    selected: list[ConversationMessageRecord] = []
    used_tokens = 0
    for item in reversed(messages[-MAX_RECENT_MESSAGES:]):
        item_tokens = estimate_conversation_tokens(item.content) + 6
        if selected and used_tokens + item_tokens > RECENT_MESSAGE_TOKEN_BUDGET:
            break
        selected.append(item)
        used_tokens += item_tokens
    selected.reverse()
    return selected


def _compact_conversation_window(*, connection, conversation_id: str, session_row) -> tuple[str, int]:
    """把近轮 token 窗口之外的消息合并进摘要，并保持摘要水位单调前进。"""

    total = int(
        connection.execute(
            "SELECT COUNT(*) FROM commander_conversation_messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
    )
    existing_count = min(_summary_message_count(session_row), total)
    recent_rows = connection.execute(
        """
        SELECT * FROM (
            SELECT * FROM commander_conversation_messages
            WHERE conversation_id = ?
            ORDER BY created_at DESC, message_id DESC
            LIMIT ?
        )
        ORDER BY created_at ASC, message_id ASC
        """,
        (conversation_id, MAX_RECENT_MESSAGES),
    ).fetchall()
    recent_messages = _fit_recent_messages([_row_to_message(row) for row in recent_rows])
    summary_boundary = max(existing_count, total - len(recent_messages))
    summary = str(session_row["summary"] or "")
    if summary_boundary > existing_count:
        rows = connection.execute(
            """
            SELECT * FROM commander_conversation_messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC, message_id ASC
            LIMIT ? OFFSET ?
            """,
            (conversation_id, summary_boundary - existing_count, existing_count),
        ).fetchall()
        summary = _merge_summary(summary, [_row_to_message(row) for row in rows])
    return summary, summary_boundary


def _row_to_session(row) -> ConversationSessionRecord:
    try:
        material_payload = json.loads(str(row["material_bindings_json"] or "[]"))
    except json.JSONDecodeError:
        material_payload = []
    if not isinstance(material_payload, list):
        material_payload = []
    bindings: list[WorkflowMaterialBinding] = []
    for item in material_payload[:8]:
        if not isinstance(item, dict):
            continue
        try:
            bindings.append(WorkflowMaterialBinding.model_validate(item))
        except ValueError:
            continue
    return ConversationSessionRecord(
        conversation_id=str(row["conversation_id"]),
        project_scope=str(row["project_scope"]),
        title=str(row["title"] or ""),
        summary=str(row["summary"] or ""),
        material_bindings=bindings,
        last_task_id=str(row["last_task_id"] or ""),
        last_plan_id=str(row["last_plan_id"] or ""),
        archived_message_count=int(row["archived_message_count"] or 0)
        if "archived_message_count" in row.keys()
        else 0,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_message(row) -> ConversationMessageRecord:
    return ConversationMessageRecord(
        message_id=str(row["message_id"]),
        conversation_id=str(row["conversation_id"]),
        role=str(row["role"]),
        content=str(row["content"]),
        task_id=str(row["task_id"] or ""),
        created_at=str(row["created_at"]),
    )


def _summary_message_count(row) -> int:
    """兼容迁移前返回的 Row；负值视为损坏值并安全回退到零。"""

    if "summary_message_count" not in row.keys():
        return 0
    return max(0, int(row["summary_message_count"] or 0))


def _merge_summary(existing_summary: str, expired: list[ConversationMessageRecord]) -> str:
    """生成目标/约束/待办/结果摘要，避免旧实现只保留截断流水账。"""

    grouped: dict[str, list[str]] = {key: [] for key in ("目标", "约束", "待办", "结果", "历史")}
    for line in existing_summary.splitlines():
        match = re.match(r"^\[(目标|约束|待办|结果|历史)\]\s*(.+)$", line.strip())
        if match:
            _append_summary_value(grouped[match.group(1)], match.group(2))
        elif line.strip():
            _append_summary_value(grouped["历史"], line.strip()[:180])

    for item in expired:
        compact = " ".join(item.content.split())
        if not compact:
            continue
        task_hint = (item.task_id or "无任务ID")[:48]
        if item.role == "user":
            _append_summary_value(grouped["目标"], f"({task_hint}) {compact[:180]}")
            for clause in _SUMMARY_CLAUSE_SPLIT_PATTERN.split(compact):
                normalized_clause = clause.strip()
                if not normalized_clause:
                    continue
                if _CONSTRAINT_SIGNAL_PATTERN.search(normalized_clause):
                    _append_summary_value(grouped["约束"], f"({task_hint}) {normalized_clause[:120]}")
                if _PENDING_SIGNAL_PATTERN.search(normalized_clause):
                    _append_summary_value(grouped["待办"], f"({task_hint}) {normalized_clause[:120]}")
        else:
            _append_summary_value(grouped["结果"], f"({task_hint}) {compact[:180]}")

    limits = {"目标": 1, "约束": 3, "待办": 1, "结果": 1, "历史": 1}
    rendered: list[str] = []
    for label in ("目标", "约束", "待办", "结果", "历史"):
        for value in reversed(grouped[label][-limits[label] :]):
            line = f"[{label}] {value}"
            candidate = "\n".join([*rendered, line])
            if len(candidate) <= CONVERSATION_SUMMARY_MAX_LENGTH:
                rendered.append(line)
    return "\n".join(rendered)


def _append_summary_value(values: list[str], value: str) -> None:
    normalized = " ".join(value.split())
    if normalized and normalized not in values:
        values.append(normalized)


def _title_from_message(message: str) -> str:
    """从首条客户输入生成稳定、无需额外模型调用的会话标题。

    会话标题只是会话切换列表的导航文案，不能为了它额外发起一次模型请求，也不能把
    ``@知识库`` 这类路由提示、Markdown 标记或换行直接暴露成技术化标题。原始输入仍按
    原样保存在 ``commander_conversation_messages``，此处仅生成一条短、可读的副本。
    """

    # @Agent 仅代表本轮路由偏好，不是客户任务本身；兼容中英文 Agent 名称与连字符/下划线。
    title = re.sub(r"(?<!\S)@[\w\-\u4e00-\u9fff]+", " ", message)
    title = " ".join(title.split())
    # 首条提问常见的礼貌前缀没有辨识价值，删除后列表更容易扫描。仅移除句首，不能改动
    # 正文中的需求语义，也不尝试把客户输入改写成模型生成的标题。
    title = re.sub(r"^(?:请|帮我|麻烦|劳烦)\s*", "", title)
    title = title.strip(" `#>*-_\u3000")
    if not title:
        return "未命名会话"
    if len(title) <= CONVERSATION_TITLE_MAX_LENGTH:
        return title
    return f"{title[:CONVERSATION_TITLE_MAX_LENGTH - 1].rstrip()}…"


def _utc_now() -> str:
    # 微秒级时间让连续轮次在 SQLite 的文本排序中保持真实先后；同一轮仍由 message_id 后缀
    # 保证用户消息先于助手消息。
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
