"""Skill tools: discover, load, and (for agent-owned skills) create and improve."""

from __future__ import annotations

import json

from ..kernel import ToolSpec
from ..skills import SkillError
from ..tooling import ToolContext, ToolError

READ_ROLES = ("worker", "reviewer", "auditor", "librarian", "refiner", "manager", "planner", "evolver", "absorber")


def _store(ctx: ToolContext):
    return ctx.kernel.get("skills")


def skills_list(ctx: ToolContext, query: str = "") -> str:
    rows = _store(ctx).list(query)
    if not rows:
        return "no skills match"
    return "\n".join(f"- {r['name']} [{r['category']}, {r['origin']}{', pinned' if r['pinned'] else ''}, "
                     f"{r['state']}, used {r['use_count']}x]: {r['description'][:200]}" for r in rows)


def skill_view(ctx: ToolContext, name: str, file_path: str = "") -> str:
    try:
        text = _store(ctx).view(name, file_path or None)
    except SkillError as exc:
        raise ToolError(str(exc))
    ctx.viewed.add((name, file_path or "SKILL.md"))
    ctx.project.log.append("skill/viewed", actor=ctx.actor, skill=name, file=file_path or "SKILL.md")
    return text


def skill_manage(ctx: ToolContext, action: str, name: str, content: str = "", old: str = "", new: str = "",
                 file_path: str = "", category: str = "general", reason: str = "") -> str:
    store = _store(ctx)
    actor = f"agent:{ctx.actor}"
    try:
        if action == "create":
            res = store.create(name, content, category=category, actor=actor, reason=reason)
        elif action == "patch":
            target = file_path or "SKILL.md"
            if (name, target) not in ctx.viewed:
                raise ToolError(f"read-before-write: call skill_view(name={name!r}"
                                f"{', file_path=' + repr(target) if target != 'SKILL.md' else ''}) first")
            res = store.patch(name, old, new, actor=actor, file_path=target, reason=reason)
        elif action == "write_file":
            exists = store.find(name) and (store.find(name) / file_path).exists()
            if exists and (name, file_path) not in ctx.viewed:
                raise ToolError(f"read-before-write: call skill_view(name={name!r}, file_path={file_path!r}) first")
            res = store.write_file(name, file_path, content, actor=actor, reason=reason)
        elif action == "archive":
            res = store.archive(name, actor=actor, reason=reason)
        else:
            raise ToolError("action must be create | patch | write_file | archive")
    except SkillError as exc:
        raise ToolError(str(exc))
    ctx.state.setdefault("skill_actions", []).append(f"{action} {name}")
    ctx.project.log.append("skill/mutated", actor=actor, action=action, skill=name, file=file_path or "SKILL.md",
                           reason=reason, result=res)
    return json.dumps(res, ensure_ascii=False)


def setup(kernel) -> None:
    S = {"type": "object", "additionalProperties": False}
    kernel.register_tool(ToolSpec(
        "skills_list", "List skills in the shared library, optionally filtered by a keyword.",
        {**S, "properties": {"query": {"type": "string"}}, "required": []}, skills_list, roles=READ_ROLES))
    kernel.register_tool(ToolSpec(
        "skill_view", "Load a skill's SKILL.md (or one of its support files via file_path) before doing a task "
        "it covers.",
        {**S, "properties": {"name": {"type": "string"}, "file_path": {"type": "string"}}, "required": ["name"]},
        skill_view, roles=READ_ROLES))
    kernel.register_tool(ToolSpec(
        "skill_manage",
        "Create or improve a skill. Actions: create (content = full SKILL.md with frontmatter name+description), "
        "patch (exact old->new; skill_view it first), write_file (support file under references/, scripts/, "
        "templates/ or assets/), archive. Only agent-created, unpinned skills are writable.",
        {**S, "properties": {
            "action": {"type": "string", "enum": ["create", "patch", "write_file", "archive"]},
            "name": {"type": "string"}, "content": {"type": "string"}, "old": {"type": "string"},
            "new": {"type": "string"}, "file_path": {"type": "string"}, "category": {"type": "string"},
            "reason": {"type": "string"}},
         "required": ["action", "name"]},
        skill_manage, roles=("worker", "librarian")))
