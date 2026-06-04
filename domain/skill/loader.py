"""技能文件加载：从 agent_flow/skills/ 下的 .md 文件读出 Skill。

每个 skill 一个 markdown 文件，带极简 frontmatter（无需 PyYAML）：

    ---
    name: 静态网页部署
    description: 把生成好的静态站点真正部署上线并预览
    tags: deploy, web, frontend
    ---
    正文（技能内容/步骤/要点）...

frontmatter 缺省时退化为：id/name 取文件名，正文为整篇内容。
"""

from __future__ import annotations

import re
from pathlib import Path

from domain.skill.skill import Skill

# loader.py -> skill -> domain -> agent_flow
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw_meta, body = match.group(1), match.group(2)
    meta: dict[str, str] = {}
    for line in raw_meta.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip()
    return meta, body


def _parse_tags(value: str) -> list[str]:
    if not value:
        return []
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    parts = re.split(r"[,，]", value)
    return [p.strip().strip("'\"") for p in parts if p.strip()]


def load_skill_file(path: str | Path) -> Skill:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    return Skill(
        id=(meta.get("id") or p.stem).strip(),
        name=(meta.get("name") or p.stem).strip(),
        description=meta.get("description", "").strip(),
        content=body.strip(),
        tags=_parse_tags(meta.get("tags", "")),
        source=str(p),
    )


def load_skills_from_dir(path: str | Path | None = None) -> list[Skill]:
    directory = Path(path) if path else DEFAULT_SKILLS_DIR
    if not directory.is_dir():
        return []
    skills: list[Skill] = []
    for fp in sorted(directory.glob("*.md")):
        try:
            skills.append(load_skill_file(fp))
        except Exception as exc:  # noqa: BLE001
            print(f"[skill] 跳过无法解析的技能文件 {fp}: {exc}")
    return skills
