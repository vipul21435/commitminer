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
from commitminer.settings import DifficultyWeights, Settings, Weights

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
        # Globs are checked when the file loads, not when a path is first matched.
        ({"classify": {"rules": [rule(dirs=["[z-a]*"])]}}, "empty character range z-a"),
        ({"classify": {"rules": [rule(dirs=["src/fixtures"])]}}, "cannot contain '/'"),
        (
            {"classify": {"rules": [rule(dirs=None, names=["data/*.json"])]}},
            "cannot contain '/'",
        ),
        ({"classify": {"rules": [rule(dirs=None, paths=["/gen/**"])]}}, "leading or trailing"),
        ({"classify": {"rules": [rule(dirs=["{" + "a," * 300 + "b}"])]}}, "alternatives"),
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


# --- scoring settings ---------------------------------------------------------------


def test_scoring_settings_are_read_and_command_line_values_win() -> None:
    config = parse_config(
        {
            "filter": {"max_lines": 250, "max_source_files": 3},
            "score": {"assertions_cap": 2, "weights": {"linked_reference": 3, "fix_keyword": 0}},
            "difficulty": {"hunks_cap": 6, "medium_at": 1.5, "hard_at": 4, "weights": {}},
        },
        Path("c.toml"),
    )
    settings = config.settings()
    assert (settings.max_lines, settings.max_source_files, settings.assertions_cap) == (250, 3, 2)
    assert (settings.hunks_cap, settings.medium_at, settings.hard_at) == (6, 1.5, 4)
    assert settings.weights == Weights(linked_reference=3.0, fix_keyword=0.0)
    assert settings.difficulty_weights == DifficultyWeights()
    assert settings.test_lines_cap == Settings().test_lines_cap
    assert config.describe().endswith(", 8 scoring settings")
    assert config.tuned[:2] == ("filter.max_lines", "filter.max_source_files")
    overridden = config.settings(max_lines=90, max_source_files=None, test_lines_cap=7)
    assert (overridden.max_lines, overridden.max_source_files) == (90, 3)
    assert overridden.test_lines_cap == 7


def test_settings_carry_the_config_rules() -> None:
    config = parse_config({"classify": {"rules": [rule()]}, "difficulty": {"lines_cap": 50}})
    assert config.settings().rules[0].rule_id == "fixtures-dir"
    assert config.describe().endswith(", 1 scoring setting")
    assert Config().settings() == Settings()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"filter": []}, r"\[filter\] must be a table"),
        ({"filter": {"max_line": 3}}, r"\[filter\]: unknown key 'max_line'"),
        ({"filter": {"weights": {}}}, "unknown key 'weights'"),
        ({"filter": {"max_lines": 2.5}}, "filter.max_lines: expected an integer"),
        ({"filter": {"max_lines": True}}, "filter.max_lines: expected a number"),
        ({"filter": {"max_lines": 0}}, "max_lines must be at least 1"),
        ({"score": {"weights": []}}, r"\[score.weights\] must be a table"),
        ({"score": {"weights": {"small": 1}}}, r"\[score.weights\]: unknown key 'small'"),
        ({"score": {"weights": {"small_diff": "3"}}}, "small_diff: expected a number"),
        ({"score": {"weights": {"small_diff": -1}}}, "weight small_diff must be"),
        ({"difficulty": {"medium_at": 5, "hard_at": 3}}, "0 <= medium_at <= hard_at"),
        ({"difficulty": {"hard_at": float("inf")}}, "hard_at: expected a finite number"),
        ({"difficulty": {"weights": {"api": 1}}}, "unknown key 'api'"),
    ],
)
def test_invalid_scoring_settings_are_rejected(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(data)
