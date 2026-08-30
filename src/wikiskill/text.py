from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import pathlib
import re

from .errors import WikiSkillError

SECRET_PATTERNS = (
    re.compile(r"(?i)((?:authorization|cookie|set-cookie):\s*)[^\r\n]+"),
    re.compile(
        r"""(?ix)
        (["']?(?:[a-z0-9]+_)*(?:api_?key|access_?key|secret(?:_access)?_key|token|secret|password|credentials?)(?:_[a-z0-9]+)*["']?\s*[=:]\s*)
        (?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}]+)
        """
    ),
    re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+(?=@)"),
    re.compile(
        r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[opusr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|glpat-[A-Za-z0-9_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"
    ),
    re.compile(
        r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "") + "<redacted>",
            text,
        )
    return text


def _sanitize(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    return value


def _session_digest(session: dict) -> str:
    encoded = json.dumps(session, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _bounded_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n\n... middle omitted ...\n\n"
    if limit <= len(marker):
        return marker[:limit]
    available = limit - len(marker)
    head = available // 3
    return text[:head] + marker + text[-(available - head) :]


def _trace_text(session: dict, limit: int = 15_000) -> str:
    lines = [
        f"session: {session.get('id', '')}",
        f"harness: {session.get('harness', '')}",
        f"project: {session.get('project', '')}",
        f"outcome: {'failure' if session.get('gave_up') else 'unlabeled'}",
    ]
    for message in session.get("messages", []):
        lines.append(f"\n[{message.get('role', 'unknown')}]\n{message.get('text', '')}")
    return _bounded_text(redact("\n".join(lines)), limit)


def _wiki_section(
    root: pathlib.Path, path: pathlib.Path, budget: int, *, tail: bool = False
) -> str:
    label = f"\n## {path.relative_to(root)}\n"
    available = max(0, budget - len(label))
    content = path.read_text(encoding="utf-8")
    if not available:
        body = ""
    elif tail and len(content) > available:
        body = content[-available:]
    else:
        body = _bounded_text(content, available)
    return (label + body)[:budget]


def _read_wiki(root: pathlib.Path, limit: int = 100_000) -> str:
    index_budget = limit * 15 // 100
    log_budget = limit * 10 // 100
    impact_budget = limit * 20 // 100
    pattern_budget = limit - index_budget - log_budget - impact_budget
    patterns = sorted((root / "wiki/patterns").glob("*.md"))
    per_pattern = pattern_budget // max(1, len(patterns))
    parts = [_wiki_section(root, root / "wiki/index.md", index_budget)]
    parts.extend(_wiki_section(root, path, per_pattern) for path in patterns)
    parts.append(_wiki_section(root, root / "wiki/log.md", log_budget, tail=True))
    parts.append(
        _wiki_section(root, root / "wiki/skill-impact.md", impact_budget, tail=True)
    )
    return "".join(parts)[:limit]


def _apply_edits(content: str, edits: list[dict]) -> str:
    for edit in edits:
        op = edit["op"]
        target = edit.get("target", "")
        addition = edit["content"]
        if op == "append":
            if _contains_block(content, addition):
                continue
            content = content.rstrip("\n") + "\n\n" + addition.strip("\n") + "\n"
        else:
            if not target:
                raise WikiSkillError(f"patch target not found for {op}")
            replacement = addition if op == "replace" else target + addition
            position, applied = _unapplied_target(content, target, replacement)
            if position >= 0:
                content = (
                    content[:position] + replacement + content[position + len(target) :]
                )
            elif applied or (op == "replace" and _contains_block(content, addition)):
                continue
            else:
                raise WikiSkillError(f"patch target not found for {op}")
    return content


def _unapplied_target(content: str, target: str, replacement: str) -> tuple[int, bool]:
    start = 0
    applied = False
    while (position := content.find(target, start)) >= 0:
        end = position + len(replacement)
        if content.startswith(replacement, position) and (
            end == len(content) or content[end] == "\n"
        ):
            applied = True
            start = end
        else:
            return position, applied
    return -1, applied


def _contains_block(content: str, value: str) -> bool:
    needle = value.strip("\n")
    if not needle:
        return False
    start = 0
    while (found := content.find(needle, start)) >= 0:
        end = found + len(needle)
        if (found == 0 or content[found - 1] == "\n") and (
            end == len(content) or content[end] == "\n"
        ):
            return True
        start = found + 1
    return False


def _skill_diff(old: pathlib.Path, new: pathlib.Path) -> str:
    names = sorted(
        {path.relative_to(old) for path in old.rglob("*.md")}
        | {path.relative_to(new) for path in new.rglob("*.md")}
    )
    output: list[str] = []
    for name in names:
        before = (
            (old / name).read_text(encoding="utf-8").splitlines(True)
            if (old / name).exists()
            else []
        )
        after = (
            (new / name).read_text(encoding="utf-8").splitlines(True)
            if (new / name).exists()
            else []
        )
        output.extend(
            difflib.unified_diff(
                before, after, fromfile=f"skills/{name}", tofile=f"candidate/{name}"
            )
        )
    return "".join(output)


def _impact_entry(proposal: dict, diff: str, outcome: dict) -> str:
    entry = f"\n## {dt.datetime.now(dt.UTC).isoformat()} — {outcome['outcome']}\n\n"
    entry += f"Reason: {proposal.get('reason', '').strip()}\n\n```diff\n{diff}```\n"
    if outcome.get("score") is not None:
        entry += (
            f"\nValidation score: {outcome['score']} (best: {outcome['best_score']})\n"
        )
    return entry
