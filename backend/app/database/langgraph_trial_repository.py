"""LGM5.7 开发者试点准入记录仓储。

记录只服务于内部试点的可追溯与撤销；没有 FastAPI、Qt 或客户 Runtime 路由会读取它。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.database.sqlite import get_connection
from app.schemas.langgraph_trial import (
    LangGraphCompositionTrialAdmissionRecord,
    LangGraphCompositionTrialAuthorizationRecord,
)


class LangGraphTrialAdmissionConflictError(ValueError):
    """试点准入记录的计划身份或终态不允许被改写。"""


class LangGraphTrialAuthorizationConflictError(ValueError):
    """候选运行预授权的计划身份或终态不允许被改写。"""


def save_langgraph_composition_trial_admission(
    record: LangGraphCompositionTrialAdmissionRecord,
) -> LangGraphCompositionTrialAdmissionRecord:
    """创建或更新同一 Runtime 的准入记录，保留开始前撤销语义。"""

    with get_connection() as connection:
        row = connection.execute(
            "SELECT admission_json FROM langgraph_composition_trial_admissions WHERE runtime_task_id = ?",
            (record.runtime_task_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO langgraph_composition_trial_admissions (
                    runtime_task_id, plan_digest, status, admission_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.runtime_task_id,
                    record.plan_digest,
                    record.status,
                    record.model_dump_json(),
                    record.created_at,
                    record.updated_at,
                ),
            )
            return record

        current = LangGraphCompositionTrialAdmissionRecord.model_validate_json(row["admission_json"])
        if current.plan_digest != record.plan_digest:
            raise LangGraphTrialAdmissionConflictError("同一 Runtime 任务不能改用另一份试点计划摘要。")
        if current.status == "revoked":
            raise LangGraphTrialAdmissionConflictError("已撤销的开发者试点不能重新启用；请创建新的 Runtime 任务。")
        if current.status == "admitted":
            if record.status != "admitted" or record.evidence != current.evidence:
                raise LangGraphTrialAdmissionConflictError(
                    "已准入的试点不能改写证据；只能在开始前通过撤销入口关闭。"
                )
            return current
        record = record.model_copy(update={"created_at": current.created_at})
        connection.execute(
            """
            UPDATE langgraph_composition_trial_admissions
            SET status = ?, admission_json = ?, updated_at = ?
            WHERE runtime_task_id = ?
            """,
            (record.status, record.model_dump_json(), record.updated_at, record.runtime_task_id),
        )
    return record


def load_langgraph_composition_trial_admission(
    runtime_task_id: str,
) -> LangGraphCompositionTrialAdmissionRecord | None:
    """按 Runtime 任务读取单条准入记录，不扫描其它任务。"""

    with get_connection() as connection:
        row = connection.execute(
            "SELECT admission_json FROM langgraph_composition_trial_admissions WHERE runtime_task_id = ?",
            (runtime_task_id,),
        ).fetchone()
    if row is None:
        return None
    return LangGraphCompositionTrialAdmissionRecord.model_validate_json(row["admission_json"])


def revoke_langgraph_composition_trial_admission(
    runtime_task_id: str,
) -> LangGraphCompositionTrialAdmissionRecord:
    """在任何 Graph 派发前显式撤销试点；撤销是不可逆终态。"""

    current = load_langgraph_composition_trial_admission(runtime_task_id)
    if current is None:
        raise LangGraphTrialAdmissionConflictError("开发者试点准入记录不存在，无法撤销。")
    if current.status == "revoked":
        return current
    if current.status != "admitted":
        raise LangGraphTrialAdmissionConflictError("未准入的开发者试点不需要撤销。")
    revoked = LangGraphCompositionTrialAdmissionRecord.model_validate(
        {
            **current.model_dump(),
            "status": "revoked",
            "blockers": ("开发者已在任务启动前撤销 LangGraph 试点。",),
            "updated_at": _now(),
        }
    )
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE langgraph_composition_trial_admissions
            SET status = ?, admission_json = ?, updated_at = ?
            WHERE runtime_task_id = ?
            """,
            (revoked.status, revoked.model_dump_json(), revoked.updated_at, runtime_task_id),
        )
    return revoked


def save_langgraph_composition_trial_authorization(
    record: LangGraphCompositionTrialAuthorizationRecord,
) -> LangGraphCompositionTrialAuthorizationRecord:
    """创建一次候选运行预授权判断；授权后仅允许开始前撤销。"""

    with get_connection() as connection:
        row = connection.execute(
            "SELECT authorization_json FROM langgraph_composition_trial_authorizations WHERE runtime_task_id = ?",
            (record.runtime_task_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO langgraph_composition_trial_authorizations (
                    runtime_task_id, plan_digest, status, authorization_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.runtime_task_id,
                    record.plan_digest,
                    record.status,
                    record.model_dump_json(),
                    record.created_at,
                    record.updated_at,
                ),
            )
            return record

        current = LangGraphCompositionTrialAuthorizationRecord.model_validate_json(
            row["authorization_json"]
        )
        if current.plan_digest != record.plan_digest:
            raise LangGraphTrialAuthorizationConflictError(
                "同一 Runtime 任务不能改用另一份试点计划摘要。"
            )
        if current.status == "revoked":
            raise LangGraphTrialAuthorizationConflictError(
                "已撤销的开发者预授权不能重新启用；请创建新的 Runtime 任务。"
            )
        if current.status == "authorized":
            if record.status != "authorized" or record.authorization != current.authorization:
                raise LangGraphTrialAuthorizationConflictError(
                    "已授权的候选运行不能改写授权摘要；只能在开始前通过撤销入口关闭。"
                )
            return current
        record = record.model_copy(update={"created_at": current.created_at})
        connection.execute(
            """
            UPDATE langgraph_composition_trial_authorizations
            SET status = ?, authorization_json = ?, updated_at = ?
            WHERE runtime_task_id = ?
            """,
            (record.status, record.model_dump_json(), record.updated_at, record.runtime_task_id),
        )
    return record


def load_langgraph_composition_trial_authorization(
    runtime_task_id: str,
) -> LangGraphCompositionTrialAuthorizationRecord | None:
    """按 Runtime 读取候选运行预授权，不扫描其它任务。"""

    with get_connection() as connection:
        row = connection.execute(
            "SELECT authorization_json FROM langgraph_composition_trial_authorizations WHERE runtime_task_id = ?",
            (runtime_task_id,),
        ).fetchone()
    if row is None:
        return None
    return LangGraphCompositionTrialAuthorizationRecord.model_validate_json(
        row["authorization_json"]
    )


def revoke_langgraph_composition_trial_authorization(
    runtime_task_id: str,
) -> LangGraphCompositionTrialAuthorizationRecord:
    """在候选 Graph 创建前撤销预授权；撤销不可逆。"""

    current = load_langgraph_composition_trial_authorization(runtime_task_id)
    if current is None:
        raise LangGraphTrialAuthorizationConflictError("开发者预授权记录不存在，无法撤销。")
    if current.status == "revoked":
        return current
    if current.status != "authorized":
        raise LangGraphTrialAuthorizationConflictError("未授权的候选运行不需要撤销。")
    revoked = LangGraphCompositionTrialAuthorizationRecord.model_validate(
        {
            **current.model_dump(),
            "status": "revoked",
            "blockers": ("开发者已在候选 Graph 创建前撤销本次试点授权。",),
            "updated_at": _now(),
        }
    )
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE langgraph_composition_trial_authorizations
            SET status = ?, authorization_json = ?, updated_at = ?
            WHERE runtime_task_id = ?
            """,
            (revoked.status, revoked.model_dump_json(), revoked.updated_at, runtime_task_id),
        )
    return revoked


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
