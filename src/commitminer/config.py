"""Per-repository settings from ``commitminer.toml``.

The classifier::

    [classify]
    disable = ["tooling-dir"]          # built-in rule ids to drop

    [[classify.rules]]                 # checked before the built-in table, in order
    id = "fixtures-dir"
    category = "test"
    dirs = ["fixtures"]                # exactly one of: dirs, names, paths, signals
    languages = ["python"]             # optional; default: every file
    case_sensitive = false             # optional
    rationale = "this project keeps test data in fixtures/"

Filter limits, feature caps, weights and difficulty bands (every key is
optional; see :class:`~commitminer.settings.Settings` for the defaults)::

    [filter]
    max_lines = 400                    # source+test lines
    max_source_files = 10

    [score]
    test_lines_cap = 40
    assertions_cap = 5

    [score.weights]                    # small_diff, test_lines_added, added_assertions,
    linked_reference = 3.0             # linked_reference, fix_keyword, focused_source

    [difficulty]
    files_cap = 10
    hunks_cap = 10
    lines_cap = 100
    cross_file_cap = 4
    medium_at = 2.0                    # band thresholds on the 0-10 difficulty
    hard_at = 4.5

    [difficulty.weights]               # files, hunks, lines, cross_file, public_api
    public_api = 2.0

Unknown tables and keys are errors, so a typo cannot silently do nothing.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Final

from commitminer.classify import FALLBACK_RULE, RULES, Category, Rule, RuleKind
from commitminer.languages import Language
from commitminer.settings import DifficultyWeights, Settings, Weights

CONFIG_NAME: Final = "commitminer.toml"
"""File name looked up at the root of a repository."""

_MATCHERS: Final[dict[str, RuleKind]] = {
    "dirs": "dir",
    "names": "name",
    "paths": "path",
    "signals": "signal",
}
_RULE_KEYS: Final = frozenset({"id", "category", "rationale", "languages", "case_sensitive"})
_BUILTIN_IDS: Final = frozenset(rule.rule_id for rule in RULES)
_TABLES: Final = frozenset({"classify", "filter", "score", "difficulty"})
_NUMBERS: Final[dict[str, dict[str, type]]] = {
    "filter": {"max_lines": int, "max_source_files": int},
    "score": {"test_lines_cap": int, "assertions_cap": int},
    "difficulty": {
        "files_cap": int,
        "hunks_cap": int,
        "lines_cap": int,
        "cross_file_cap": int,
        "medium_at": float,
        "hard_at": float,
    },
}
"""Settings fields each table may set, with their TOML type."""
_WEIGHTS: Final[dict[str, type[Weights] | type[DifficultyWeights]]] = {
    "score": Weights,
    "difficulty": DifficultyWeights,
}


class ConfigError(ValueError):
    """``commitminer.toml`` is unreadable or does not follow the schema."""


@dataclass(frozen=True, slots=True)
class Config:
    """Classifier overrides and scoring settings for one repository."""

    path: Path | None = None
    custom_rules: tuple[Rule, ...] = ()
    disabled: tuple[str, ...] = ()
    tuning: Settings = field(default_factory=Settings)
    """Limits, caps, weights and bands from the file (defaults for keys it does not set)."""
    tuned: tuple[str, ...] = ()
    """The ``table.key`` names the file set, for :meth:`describe`."""

    @property
    def rules(self) -> tuple[Rule, ...]:
        """Custom rules first, then the built-in table without the disabled rules."""
        kept = tuple(rule for rule in RULES if rule.rule_id not in self.disabled)
        return self.custom_rules + kept

    def settings(
        self,
        *,
        max_lines: int | None = None,
        max_source_files: int | None = None,
        test_lines_cap: int | None = None,
    ) -> Settings:
        """The file's settings with this config's rules; command-line values win if given."""
        base = self.tuning
        return replace(
            base,
            rules=self.rules,
            max_lines=base.max_lines if max_lines is None else max_lines,
            max_source_files=base.max_source_files
            if max_source_files is None
            else max_source_files,
            test_lines_cap=base.test_lines_cap if test_lines_cap is None else test_lines_cap,
        )

    def describe(self) -> str:
        """One line for the terminal: where the config came from and what it changes."""
        custom, disabled = len(self.custom_rules), len(self.disabled)
        where = self.path or CONFIG_NAME
        text = (
            f"config: {where}: {custom} custom rule{'' if custom == 1 else 's'}, "
            f"{disabled} built-in rule{'' if disabled == 1 else 's'} disabled"
        )
        if self.tuned:
            text += f", {len(self.tuned)} scoring setting{'' if len(self.tuned) == 1 else 's'}"
        return text


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}: expected a list of strings")
    return tuple(value)


def _check_keys(table: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key {unknown[0]!r} (allowed: {', '.join(sorted(allowed))})"
        )


def _rule(raw: Any, where: str) -> Rule:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    _check_keys(raw, _RULE_KEYS | _MATCHERS.keys(), where)
    for key in ("id", "category", "rationale"):
        if not isinstance(raw.get(key), str):
            raise ConfigError(f"{where}: {key!r} is required and must be a string")
    rule_id = raw["id"]
    if rule_id in _BUILTIN_IDS or rule_id == FALLBACK_RULE:
        raise ConfigError(
            f"{where}: id {rule_id!r} is a built-in rule; pick a new id "
            "(and list the built-in one under classify.disable to replace it)"
        )
    try:
        category = Category(raw["category"])
    except ValueError:
        choices = ", ".join(c.value for c in Category)
        raise ConfigError(f"{where}: unknown category {raw['category']!r} ({choices})") from None
    matchers = [key for key in _MATCHERS if key in raw]
    if len(matchers) != 1:
        raise ConfigError(f"{where}: give exactly one of {', '.join(_MATCHERS)}")
    patterns = _strings(raw[matchers[0]], f"{where}.{matchers[0]}")
    languages: list[Language] = []
    for name in _strings(raw.get("languages", []), f"{where}.languages"):
        try:
            languages.append(Language(name))
        except ValueError:
            choices = ", ".join(language.value for language in Language)
            raise ConfigError(f"{where}: unknown language {name!r} ({choices})") from None
    case_sensitive = raw.get("case_sensitive", False)
    if not isinstance(case_sensitive, bool):
        raise ConfigError(f"{where}: case_sensitive must be true or false")
    try:
        return Rule(
            rule_id,
            category,
            _MATCHERS[matchers[0]],
            patterns,
            raw["rationale"],
            languages=tuple(languages),
            case_sensitive=case_sensitive,
        )
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from None


def _number(value: Any, kind: type, where: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    if kind is int and not isinstance(value, int):
        raise ConfigError(f"{where}: expected an integer, got {value!r}")
    if not math.isfinite(value):
        raise ConfigError(f"{where}: expected a finite number, got {value!r}")
    return value


def _tuning(data: Mapping[str, Any], where: str) -> tuple[Settings, tuple[str, ...]]:
    """Read ``[filter]``, ``[score]`` and ``[difficulty]`` into a :class:`Settings`."""
    values: dict[str, Any] = {}
    tuned: list[str] = []
    for table, numbers in _NUMBERS.items():
        section = data.get(table, {})
        if not isinstance(section, dict):
            raise ConfigError(f"{where}: [{table}] must be a table")
        allowed = set(numbers) | ({"weights"} if table in _WEIGHTS else set())
        _check_keys(section, frozenset(allowed), f"{where}: [{table}]")
        for key, kind in numbers.items():
            if key in section:
                values[key] = _number(section[key], kind, f"{where}: {table}.{key}")
                tuned.append(f"{table}.{key}")
        if table not in _WEIGHTS:
            continue
        weights_class = _WEIGHTS[table]
        raw = section.get("weights", {})
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: [{table}.weights] must be a table")
        names = frozenset(item.name for item in fields(weights_class))
        _check_keys(raw, names, f"{where}: [{table}.weights]")
        chosen = {
            key: float(_number(value, float, f"{where}: {table}.weights.{key}"))
            for key, value in raw.items()
        }
        tuned += [f"{table}.weights.{key}" for key in chosen]
        target = "weights" if table == "score" else "difficulty_weights"
        try:
            values[target] = weights_class(**chosen)
        except ValueError as exc:
            raise ConfigError(f"{where}: [{table}.weights]: {exc}") from None
    try:
        return Settings(**values), tuple(tuned)
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from None


def parse_config(data: Mapping[str, Any], path: Path | None = None) -> Config:
    """Validate a parsed TOML document and build a :class:`Config`."""
    where = str(path) if path is not None else CONFIG_NAME
    _check_keys(data, _TABLES, where)
    tuning, tuned = _tuning(data, where)
    section = data.get("classify", {})
    if not isinstance(section, dict):
        raise ConfigError(f"{where}: [classify] must be a table")
    _check_keys(section, frozenset({"rules", "disable"}), f"{where}: [classify]")
    disabled = _strings(section.get("disable", []), f"{where}: classify.disable")
    for rule_id in disabled:
        if rule_id not in _BUILTIN_IDS:
            raise ConfigError(f"{where}: classify.disable: no built-in rule {rule_id!r}")
    raw_rules = section.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigError(f"{where}: classify.rules must be an array of tables")
    rules = tuple(
        _rule(raw, f"{where}: classify.rules[{index}]") for index, raw in enumerate(raw_rules)
    )
    seen: set[str] = set()
    for rule in rules:
        if rule.rule_id in seen:
            raise ConfigError(f"{where}: duplicate rule id {rule.rule_id!r}")
        seen.add(rule.rule_id)
    return Config(path=path, custom_rules=rules, disabled=disabled, tuning=tuning, tuned=tuned)


def load_config(path: Path) -> Config:
    """Read and validate a ``commitminer.toml`` file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    return parse_config(data, path)


def find_config(root: Path) -> Path | None:
    """``root/commitminer.toml`` if it exists."""
    candidate = root / CONFIG_NAME
    return candidate if candidate.is_file() else None
