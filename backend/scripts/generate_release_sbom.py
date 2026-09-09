"""生成目录发行候选的最小 SBOM。

SBOM 只描述随 Python 后端发布的声明依赖及其构建环境版本/许可证元数据；不扫描客户
目录、不记录绝对路径，也不读取 .env、任务、材料或运行期数据库。
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, metadata, version
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 AgentFlow 目录发行 Python SBOM。")
    parser.add_argument("--output", type=Path, required=True, help="候选发行目录中的 release-sbom.json。")
    parser.add_argument(
        "--requirements",
        type=Path,
        default=BACKEND_ROOT / "requirements.txt",
        help="正式运行时依赖清单；默认 backend/requirements.txt。",
    )
    return parser.parse_args()


def _declared_distributions(requirements_path: Path) -> list[str]:
    """解析 requirements 的直接声明，不把构建工具或全局环境误计为客户载荷。"""

    discovered: list[str] = []
    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = _REQUIREMENT_NAME.match(line)
        if match:
            discovered.append(match.group(1))
    return sorted(set(discovered), key=str.casefold)


def _distribution_record(name: str) -> dict[str, str]:
    """只保留公开包元数据；缺失安装项必须显式可见，不能静默伪造版本。"""

    try:
        package_metadata = metadata(name)
        package_version = version(name)
    except PackageNotFoundError:
        return {"name": name, "version": "missing", "license": "unknown"}

    raw_license = (
        package_metadata.get("License-Expression")
        or package_metadata.get("License")
        or "unknown"
    ).strip()
    # 部分 wheel 把完整许可证正文写在 ``License`` 元数据中。SBOM 保留可定位的摘要，
    # 正文仍应由随包的第三方许可证文件或供应商发行物提供，不能把候选目录膨胀成文档副本。
    license_name = next((line.strip() for line in raw_license.splitlines() if line.strip()), "unknown")[:200]
    return {
        "name": package_metadata.get("Name", name),
        "version": package_version,
        "license": license_name or "unknown",
    }


def main() -> None:
    args = _parse_args()
    requirements_path = args.requirements.resolve()
    if not requirements_path.is_file():
        raise SystemExit("SBOM 生成已停止：正式运行时 requirements.txt 不存在。")

    packages = [_distribution_record(name) for name in _declared_distributions(requirements_path)]
    if any(item["version"] == "missing" for item in packages):
        missing = ", ".join(item["name"] for item in packages if item["version"] == "missing")
        raise SystemExit(f"SBOM 生成已停止：构建环境缺少正式依赖：{missing}")

    document = {
        "format": "agentflow.release-sbom.v1",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "scope": "python-backend-runtime-direct-dependencies",
        "project": "AgentFlow",
        "packages": packages,
        "excluded": [
            "api_keys",
            "customer_data",
            "absolute_paths",
            "development_build_tools",
            "optional_node_runtime",
        ],
    }
    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"AgentFlow release SBOM written: packages={len(packages)}")


if __name__ == "__main__":
    main()
