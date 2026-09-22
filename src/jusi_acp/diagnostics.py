"""Print recent ACP diagnostics without exporting conversation contents."""
from __future__ import annotations

import json
import os
import sys
from importlib.metadata import version
from pathlib import Path


def main() -> None:
    print(f"Python: {sys.executable}")
    print(f"jusi-acp source: {Path(__file__).resolve().parent}")
    for package in ("jusi-acp", "agent-client-protocol", "jusi"):
        print(f"{package}: {version(package)}")
    if os.environ.get("JUSI_STATE_HOME"):
        root = Path(os.environ["JUSI_STATE_HOME"]).expanduser()
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))).expanduser() / "jusi"
    rows = []
    for path in (root / "plugins/acp").glob("*/*/sessions/*/events.jsonl"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("title") in {"ACP diagnostic", "ACP background task failed", "ACP runtime failed"}:
                    rows.append(row)
        except OSError:
            continue
    for row in sorted(rows, key=lambda row: row.get("time", ""))[-10:]:
        print(f"\n{row.get('time', '')} {row['title']}\n{row.get('text', '')}")
    if not rows:
        print("No saved ACP diagnostics found.")


if __name__ == "__main__":
    main()
