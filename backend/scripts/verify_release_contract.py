"""LGM7 目录发行契约的离线验证，不构建安装包或读取客户配置。"""

from __future__ import annotations

import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent


def _probe_release_paths(release_root: Path, user_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "AGENTFLOW_RELEASE_MODE": "directory",
            "AGENTFLOW_PROJECT_ROOT": str(release_root),
            "AGENTFLOW_DATA_DIR": str(user_root / "data"),
            "AGENTFLOW_OUTPUT_DIR": str(user_root / "output"),
            "AGENTFLOW_USER_AGENTS_DIR": str(user_root / "agents"),
            "AGENTFLOW_NODE_HARNESS_NODE_PROGRAM": str(release_root / "runtime" / "node" / "node.exe"),
            "PYTHONPATH": str(BACKEND_ROOT),
        }
    )
    code = """
import json
from app.core.config import settings
print(json.dumps({
    'project_root': str(settings.project_root),
    'data_dir': str(settings.data_dir),
    'output_dir': str(settings.output_dir),
    'data_chart_output_dir': str(settings.data_chart_output_dir),
    'document_presentation_output_dir': str(settings.document_presentation_output_dir),
    'knowledge_report_output_dir': str(settings.knowledge_report_output_dir),
    'node_program': settings.node_harness_node_program,
}, ensure_ascii=False))
"""
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=BACKEND_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def main() -> None:
    verify_root = Path(tempfile.mkdtemp(prefix="agentflow_release_contract_"))
    try:
        release_root = verify_root / "AgentFlow"
        user_root = verify_root / "user-data"
        values = _probe_release_paths(release_root, user_root)

        assert Path(values["project_root"]) == release_root.resolve()
        assert Path(values["data_dir"]) == (user_root / "data").resolve()
        assert Path(values["output_dir"]) == (user_root / "output").resolve()
        assert Path(values["data_chart_output_dir"]).is_relative_to((user_root / "output").resolve())
        assert Path(values["document_presentation_output_dir"]).is_relative_to((user_root / "output").resolve())
        assert Path(values["knowledge_report_output_dir"]).is_relative_to((user_root / "output").resolve())
        # Windows 在不存在的未来发布目录上可能把 ``Administrator`` 折叠为 8.3 路径；
        # 不比较完整字符串，只验证目录发行的稳定尾部结构。
        assert Path(values["node_program"]).parts[-3:] == ("runtime", "node", "node.exe")

        entrypoint = BACKEND_ROOT / "packaging" / "agentflow_backend_entry.py"
        specification = BACKEND_ROOT / "packaging" / "agentflow_backend.spec"
        py_compile.compile(entrypoint, doraise=True)
        py_compile.compile(specification, doraise=True)
        spec_text = specification.read_text(encoding="utf-8")
        assert "AgentFlowBackend" in spec_text
        assert "agentflow_profile" in spec_text
        assert '(str(RUNTIME_ROOT / "node_modules")' not in spec_text
        assert 'BACKEND_ROOT / ".env"' not in spec_text
        for optional_ocr_package in ("paddle", "paddleocr", "paddlex"):
            assert f'"{optional_ocr_package}"' in spec_text

        build_script = BACKEND_ROOT / "scripts" / "build_directory_backend.py"
        dry_run = subprocess.run(
            [sys.executable, "-X", "utf8", str(build_script), "--release-root", str(verify_root / "release"), "--dry-run"],
            cwd=BACKEND_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        manifest = json.loads(dry_run.stdout)
        assert manifest["layout"] == "directory"
        assert manifest["backend"] == "backend/AgentFlowBackend.exe"
        assert manifest["node_harness_included"] is False
        assert ".env" in manifest["excluded"]

        sbom_script = BACKEND_ROOT / "scripts" / "generate_release_sbom.py"
        sbom_path = verify_root / "release-sbom.json"
        subprocess.run(
            [sys.executable, "-X", "utf8", str(sbom_script), "--output", str(sbom_path)],
            cwd=BACKEND_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        assert sbom["format"] == "agentflow.release-sbom.v1"
        assert sbom["scope"] == "python-backend-runtime-direct-dependencies"
        assert any(item["name"].casefold() == "fastapi" for item in sbom["packages"])
        assert all(len(item["license"]) <= 200 for item in sbom["packages"])

        print(
            "LGM7 release contract verification passed: "
            "directory paths=user-data, bundled-node=explicit, packaging=no-secrets/no-node_modules."
        )
    finally:
        shutil.rmtree(verify_root, ignore_errors=True)


if __name__ == "__main__":
    main()
