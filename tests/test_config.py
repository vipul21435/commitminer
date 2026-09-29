from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from commitminer.classify import RULES, Category, classify
from commitminer.config import (
    CONFIG_NAME,
    Config,
    ConfigError,
    find_config,
    load_config,
    parse_config,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "classify" / CONFIG_NAME


def rule(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": "fixtures-dir",
        "category": "test",
        "dirs": ["fixtures"],
        "rationale": "test data lives in fixtures/",
    }
    raw.update(overrides)
    return {key: value for key, value in raw.items() if value is not None}


def test_defaults_are_the_builtin_table() -> None:
    config = Config()
    assert config.rules == RULES
    assert parse_config({}).rules == RULES


def test_custom_rules_come_first_and_disabled_rules_are_dropped() -> None:
    config = parse_config({"classify": {"rules": [rule()], "disable": ["tooling-dir"]}})
    assert config.rules[0].rule_id == "fixtures-dir"
    assert "tooling-dir" not in {r.rule_id for r in config.rules}
    assert len(config.rules) == len(RULES)
    assert classify("fixtures/a.json", config.rules).category is Category.TEST
    assert classify("tools/cli/main.go", config.rules).rule_id == "go-source"


def test_every_matcher_and_option_is_accepted() -> None:
    config = parse_config(
        {
            "classify": {
                "rules": [
                    rule(id="a", dirs=None, names=["*.proto"], category="config"),
                    rule(id="b", dirs=None, paths=["gen/**"], category="generated"),
                    rule(
                        id="c",
                        dirs=None,
                        signals=["minified"],
                        languages=["javascript"],
                        category="generated",
                    ),
                    rule(id="d", dirs=None, names=["*Spec.java"], case_sensitive=True),
                ]
            }
        }
    )
    a, b, c, d = config.custom_rules
    assert (a.kind, b.kind, c.kind, d.kind) == ("name", "path", "signal", "name")
    assert c.languages_label == "javascript"
    assert d.case_sensitive
    assert classify("api/x.proto", config.rules).rule_id == "a"
    assert classify("ParserSpec.java", config.rules).rule_id == "d"
    assert classify("parserspec.java", config.rules).rule_id == "java-source"


def test_describe() -> None:
    config = parse_config({"classify": {"rules": [rule()], "disable": ["lockfile"]}})
    assert config.describe() == "config: commitminer.toml: 1 custom rule, 1 built-in rule disabled"
    two = parse_config(
        {"classify": {"rules": [rule(), rule(id="other")], "disable": []}}, Path("x.toml")
    )
    assert two.describe() == "config: x.toml: 2 custom rules, 0 built-in rules disabled"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"clasify": {}}, "unknown key 'clasify'"),
        ({"classify": []}, r"\[classify\] must be a table"),
        ({"classify": {"rule": []}}, "unknown key 'rule'"),
        ({"classify": {"disable": "lockfile"}}, "expected a list of strings"),
        ({"classify": {"disable": ["nope"]}}, "no built-in rule 'nope'"),
        ({"classify": {"rules": {}}}, "must be an array of tables"),
        ({"classify": {"rules": ["x"]}}, r"rules\[0\]: expected a table"),
        ({"classify": {"rules": [rule(id=None)]}}, "'id' is required"),
        ({"classify": {"rules": [rule(rationale=3)]}}, "'rationale' is required"),
        ({"classify": {"rules": [rule(id="test-dir")]}}, "is a built-in rule"),
        ({"classify": {"rules": [rule(id="fallback")]}}, "is a built-in rule"),
        ({"classify": {"rules": [rule(id="Bad Id")]}}, "use lower-case"),
        ({"classify": {"rules": [rule(category="tests")]}}, "unknown category 'tests'"),
        ({"classify": {"rules": [rule(dirs=None)]}}, "exactly one of dirs"),
        ({"classify": {"rules": [rule(names=["x"])]}}, "exactly one of dirs"),
        ({"classify": {"rules": [rule(dirs=[1])]}}, "expected a list of strings"),
        ({"classify": {"rules": [rule(dirs=[])]}}, "at least one non-empty pattern"),
        ({"classify": {"rules": [rule(dirs=None, signals=["x"])]}}, "unknown signal"),
        ({"classify": {"rules": [rule(languages=["kotlin"])]}}, "unknown language 'kotlin'"),
        ({"classify": {"rules": [rule(case_sensitive="yes")]}}, "must be true or false"),
        ({"classify": {"rules": [rule(glob=["x"])]}}, "unknown key 'glob'"),
        ({"classify": {"rules": [rule(), rule()]}}, "duplicate rule id 'fixtures-dir'"),
    ],
)
def test_invalid_configs_are_rejected(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_load_the_example_config() -> None:
    config = load_config(EXAMPLE)
    assert config.path == EXAMPLE
    assert [r.rule_id for r in config.custom_rules] == ["fixtures-dir"]
    assert config.disabled == ("tooling-dir",)


def test_load_errors_name_the_file(tmp_path: Path) -> None:
    missing = tmp_path / CONFIG_NAME
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(missing)
    missing.write_text("[classify\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(missing)
    missing.write_text("[classify]\ndisable = ['nope']\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=f"{CONFIG_NAME}: classify.disable"):
        load_config(missing)


def test_find_config(tmp_path: Path) -> None:
    assert find_config(tmp_path) is None
    (tmp_path / CONFIG_NAME).mkdir()
    assert find_config(tmp_path) is None
    other = tmp_path / "repo"
    other.mkdir()
    (other / CONFIG_NAME).write_text("", encoding="utf-8")
    assert find_config(other) == other / CONFIG_NAME
