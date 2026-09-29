"""Classify files on disk and render the rule table (``classify`` and ``rules`` commands)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from commitminer.classify import FALLBACK_RULE, Category, Rule, match
from commitminer.languages import Language, language_of
from commitminer.signals import MAX_CONTENT, detect


@dataclass(frozen=True, slots=True)
class Verdict:
    """How one path was classified, and whether its content was read."""

    path: str
    category: Category
    rule: Rule | None
    language: Language | None
    signals: tuple[str, ...]
    read: bool

    @property
    def rule_id(self) -> str:
        """The matched rule's id, or ``fallback``."""
        return self.rule.rule_id if self.rule is not None else FALLBACK_RULE


def read_head(path: Path, limit: int = MAX_CONTENT) -> bytes:
    """The first ``limit`` bytes of a file."""
    with path.open("rb") as handle:
        return handle.read(limit)


def classify_file(root: Path, path: str, rules: Sequence[Rule], content: bool = True) -> Verdict:
    """Classify a repository-relative ``path``, reading ``root/path`` for signals if it exists."""
    language = language_of(path)
    file = root / path
    read = content and file.is_file()
    signals = detect(language, read_head(file)) if read else ()
    rule = match(path, rules, signals)
    category = rule.category if rule is not None else Category.OTHER
    return Verdict(path, category, rule, language, signals, read)


def verdict_to_json(verdict: Verdict) -> dict[str, Any]:
    """The JSON object printed by ``classify --json``."""
    return {
        "path": verdict.path,
        "category": verdict.category.value,
        "rule": verdict.rule_id,
        "language": verdict.language.value if verdict.language else None,
        "signals": list(verdict.signals),
        "content_read": verdict.read,
    }


def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def render_verdicts(verdicts: Sequence[Verdict], content: bool) -> str:
    """A table of path, category and rule, then the rationale of every rule used."""
    width = max([len("rule"), *(len(v.rule_id) for v in verdicts)])
    rows = [f"{'category':<10} {'rule':<{width}}  path"]
    unread = False
    for verdict in verdicts:
        mark = ""
        if content and not verdict.read:
            mark, unread = " *", True
        signals = f"  [{', '.join(verdict.signals)}]" if verdict.signals else ""
        rows.append(
            f"{verdict.category.value:<10} {verdict.rule_id:<{width}}  "
            f"{_ascii(verdict.path)}{signals}{mark}"
        )
    if unread:
        rows.append("* not a file under the root: classified by path only")
    if not content:
        rows.append("content not read (--no-content): classified by path only")
    rows += ["", "rules used:"]
    used: dict[str, str] = {}
    for verdict in verdicts:
        used.setdefault(
            verdict.rule_id,
            verdict.rule.rationale if verdict.rule is not None else "no rule matched",
        )
    rows += [f"  {rule_id:<{width}}  {rationale}" for rule_id, rationale in used.items()]
    return "\n".join(rows)


def describe_match(rule: Rule) -> str:
    """The matcher of a rule as one line, e.g. ``name: *_test.go, unless: **/src/main/**``."""
    text = f"{rule.kind}: {', '.join(rule.patterns)}"
    if rule.case_sensitive:
        text += " (case-sensitive)"
    if rule.unless:
        text += f", unless: {', '.join(rule.unless)}"
    return text


def render_rules(rules: Sequence[Rule], custom: int = 0) -> str:
    """The effective table, one rule per block, in match order."""
    rows: list[str] = []
    for number, rule in enumerate(rules, start=1):
        origin = "  (commitminer.toml)" if number <= custom else ""
        rows.append(
            f"{number:>2}. {rule.rule_id} -> {rule.category.value} [{rule.languages_label}]{origin}"
        )
        rows.append(f"    {describe_match(rule)}")
        rows.append(f"    {rule.rationale}")
    rows.append(f"    no rule matched -> {Category.OTHER.value} ({FALLBACK_RULE})")
    return "\n".join(rows)


def render_rules_markdown(rules: Sequence[Rule]) -> str:
    """The table as Markdown, as committed in ``docs/rules.md``."""
    rows = [
        "| # | rule | category | languages | match | rationale |",
        "| ---: | --- | --- | --- | --- | --- |",
    ]
    for number, rule in enumerate(rules, start=1):
        patterns = ", ".join(f"`{pattern}`" for pattern in rule.patterns)
        sensitive = " (case-sensitive)" if rule.case_sensitive else ""
        if rule.unless:
            sensitive += "; unless: " + ", ".join(f"`{pattern}`" for pattern in rule.unless)
        rows.append(
            f"| {number} | `{rule.rule_id}` | {rule.category.value} | {rule.languages_label} "
            f"| {rule.kind}: {patterns}{sensitive} | {rule.rationale.replace('|', '\\|')} |"
        )
    rows.append(f"| - | `{FALLBACK_RULE}` | {Category.OTHER.value} | any | no rule matched | |")
    return "\n".join(rows)
