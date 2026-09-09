"""构建 AgentFlowBackend 的目录式发行载荷。

该脚本只装配 Python 后端，不复制 Qt 主程序。调用方必须显式指定一个空的发布根目录，
从而避免把客户数据、开发产物或本地 .env 混入发行载荷。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
SPECIFICATION = BACKEND_ROOT / "packaging" / "agentflow_backend.spec"
NODE_RUNTIME_SOURCE = BACKEND_ROOT / "runtime" / "deepseek_harness_node"


class ReleaseBuildError(RuntimeError):
    """发行载荷不满足确定性装配条件时抛出。"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 AgentFlow Python 后端目录发行载荷。")
    parser.add_argument("--release-root", type=Path, required=True, help="目标 AgentFlow 目录，必须不存在或为空。")
    parser.add_argument(
        "--include-node-harness",
        action="store_true",
        help="显式随发行载荷复制已锁定的 Node Harness node_modules。",
    )
    parser.add_argument(
        "--node-runtime",
        type=Path,
        help="便携 Node 根目录；仅与 --include-node-harness 一起使用，目录内必须有 node.exe。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出将要装配的脱敏清单，不安装依赖、不调用 PyInstaller，也不写文件。",
    )
    return parser.parse_args()


def _release_manifest(*, node_harness_included: bool) -> dict[str, object]:
    """生成不含绝对路径、密钥或客户数据的发行清单。"""

    return {
        "format": "agentflow.release-manifest.v1",
        "layout": "directory",
        "entrypoint": "AgentFlow.exe",
        "backend": "backend/AgentFlowBackend.exe",
        "mutable_roots": ["%LOCALAPPDATA%/AgentFlow/data", "%LOCALAPPDATA%/AgentFlow/output"],
        "node_harness_included": node_harness_included,
        "node_harness_default_enabled": False,
        "excluded": [".env", "data", "output", "customer_plugins", "node_modules_without_explicit_opt_in"],
    }


def _validate_request(args: argparse.Namespace) -> None:
    if not SPECIFICATION.is_file():
        raise ReleaseBuildError("缺少 AgentFlowBackend PyInstaller 规格文件。")
    if args.node_runtime and not args.include_node_harness:
        raise ReleaseBuildError("--node-runtime 只能与 --include-node-harness 一起使用。")
    if args.include_node_harness:
        if not (NODE_RUNTIME_SOURCE / "node_modules" / ".bin").is_dir():
            raise ReleaseBuildError("项目内 Node Harness 依赖不完整，拒绝装配。")
        if args.node_runtime is None or not (args.node_runtime / "node.exe").is_file():
            raise ReleaseBuildError("启用 Node Harness 时必须提供包含 node.exe 的便携 Node 目录。")

    release_root = args.release_root.resolve()
    if release_root.exists() and any(release_root.iterdir()):
        raise ReleaseBuildError("目标发布目录非空；拒绝覆盖未知文件。")


def _require_pyinstaller() -> str:
    try:
        return version("pyinstaller")
    except PackageNotFoundError as error:
        raise ReleaseBuildError(
            "构建机尚未安装 PyInstaller；请在 backend 虚拟环境安装 requirements-dev.txt 后重试。"
        ) from error


def _copytree_without_runtime_secrets(source: Path, destination: Path) -> None:
    """复制已锁定的 Node runtime，但永远忽略探针状态和潜在本地配置。"""

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in {".probe-state", ".env", "logs"} or name.startswith(".env.")}
        return ignored

    shutil.copytree(source, destination, ignore=ignore)


def _build(args: argparse.Namespace) -> dict[str, object]:
    release_root = args.release_root.resolve()
    release_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentflow_backend_release_") as temporary:
        temporary_root = Path(temporary)
        dist_path = temporary_root / "dist"
        work_path = temporary_root / "work"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                str(SPECIFICATION),
                "--noconfirm",
                "--clean",
                "--distpath",
                str(dist_path),
                "--workpath",
                str(work_path),
            ],
            cwd=BACKEND_ROOT,
            check=True,
        )
        bundle_root = dist_path / "AgentFlowBackend"
        if not (bundle_root / "AgentFlowBackend.exe").is_file():
            raise ReleaseBuildError("PyInstaller 未生成 AgentFlowBackend.exe。")
        shutil.copytree(bundle_root, release_root / "backend")

        if args.include_node_harness:
            runtime_destination = release_root / "runtime" / "deepseek_harness_node"
            _copytree_without_runtime_secrets(NODE_RUNTIME_SOURCE, runtime_destination)
            shutil.copytree(args.node_runtime.resolve(), release_root / "runtime" / "node")

    manifest = _release_manifest(node_harness_included=args.include_node_harness)
    (release_root / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    args = _parse_args()
    _validate_request(args)
    manifest = _release_manifest(node_harness_included=args.include_node_harness)
    if args.dry_run:
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        return

    pyinstaller_version = _require_pyinstaller()
    _build(args)
    print(
        "AgentFlowBackend directory payload built: "
        f"layout=directory pyinstaller={pyinstaller_version} node_harness={args.include_node_harness}"
    )


if __name__ == "__main__":
    try:
        main()
    except ReleaseBuildError as error:
        raise SystemExit(f"发行装配已停止：{error}") from error
