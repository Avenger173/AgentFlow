"""校验完整目录候选包的公开布局，不启动 GUI 或读取客户数据。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证 AgentFlow 目录发行的单入口布局。")
    parser.add_argument("--release-root", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    root = _parse_args().release_root.resolve()
    required_files = [
        root / "AgentFlow.exe",
        root / "backend" / "AgentFlowBackend.exe",
        root / "release-manifest.json",
        root / "release-sbom.json",
    ]
    missing = [path.relative_to(root).as_posix() for path in required_files if not path.is_file()]
    if missing:
        raise SystemExit(f"目录发行布局验证已停止：缺少 {', '.join(missing)}")
    if (root / "bin").exists():
        raise SystemExit("目录发行布局验证已停止：发现多余 bin/，客户入口必须位于发行根目录。")

    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("entrypoint") != "AgentFlow.exe" or manifest.get("backend") != "backend/AgentFlowBackend.exe":
        raise SystemExit("目录发行布局验证已停止：发行清单与实际客户入口不一致。")
    print("AgentFlow directory release layout verification passed: entrypoint=root backend=present")


if __name__ == "__main__":
    main()
