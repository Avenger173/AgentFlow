"""SQLite-backed metadata repository for the controlled media workspace.

The workspace keeps source images, revisions, and exports in its private file
tree. This module persists only the versioned metadata that references those
files. `manifest_json` deliberately follows the existing task-repository
snapshot pattern: a project change is committed as one SQLite transaction and
the service layer remains the single validator of the manifest contract.
"""

from __future__ import annotations

import json
from typing import Any

from app.database.sqlite import get_connection


class MediaWorkspaceProjectNotFoundError(LookupError):
    """No canonical metadata exists for the requested controlled project."""


def load_media_workspace_manifest(project_id: str) -> dict[str, Any]:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT manifest_json FROM media_workspace_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    if row is None:
        raise MediaWorkspaceProjectNotFoundError(project_id)
    try:
        manifest = json.loads(str(row["manifest_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("图片工程 SQLite 元数据无法读取。") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("图片工程 SQLite 元数据结构无效。")
    return manifest


def list_media_workspace_manifests() -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT manifest_json FROM media_workspace_projects "
            "ORDER BY updated_at DESC, created_at DESC, project_id DESC"
        ).fetchall()
    manifests: list[dict[str, Any]] = []
    for row in rows:
        try:
            manifest = json.loads(str(row["manifest_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("图片工程 SQLite 元数据无法读取。") from exc
        if not isinstance(manifest, dict):
            raise RuntimeError("图片工程 SQLite 元数据结构无效。")
        manifests.append(manifest)
    return manifests


def save_media_workspace_manifest(manifest: dict[str, Any]) -> None:
    project_id = str(manifest.get("project_id", ""))
    title = str(manifest.get("title", ""))
    created_at = str(manifest.get("created_at", ""))
    updated_at = str(manifest.get("updated_at", ""))
    schema_version = manifest.get("schema_version")
    if not project_id or not title or not created_at or not updated_at or not isinstance(schema_version, int):
        raise ValueError("图片工程元数据缺少持久化所需字段。")
    serialized = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO media_workspace_projects (
                project_id, schema_version, title, created_at, updated_at, manifest_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                schema_version = excluded.schema_version,
                title = excluded.title,
                updated_at = excluded.updated_at,
                manifest_json = excluded.manifest_json
            """,
            (project_id, schema_version, title, created_at, updated_at, serialized),
        )


def delete_media_workspace_manifest(project_id: str) -> None:
    """Remove metadata only; retained for controlled migration regression fixtures."""

    with get_connection() as connection:
        connection.execute("DELETE FROM media_workspace_projects WHERE project_id = ?", (project_id,))
