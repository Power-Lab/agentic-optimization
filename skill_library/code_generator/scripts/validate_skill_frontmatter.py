from __future__ import annotations

import argparse
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML is required to validate YAML frontmatter.") from exc


def parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("frontmatter must start on the first line")

    for index in range(1, len(lines)):
        if lines[index] == "---":
            block = "\n".join(lines[1:index])
            data = yaml.safe_load(block)
            if not isinstance(data, dict):
                raise ValueError("frontmatter must be a YAML mapping")
            return data

    raise ValueError("missing closing frontmatter delimiter")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Codex skill frontmatter.")
    parser.add_argument("skill_md", type=Path)
    parser.add_argument("--expected-name", required=True)
    args = parser.parse_args()

    data = parse_frontmatter(args.skill_md)
    if data.get("name") != args.expected_name:
        raise ValueError(f"expected name {args.expected_name!r}, got {data.get('name')!r}")
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        raise ValueError("description must be a non-empty string")

    print(f"OK {args.skill_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

