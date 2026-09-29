import re

import commitminer


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", commitminer.__version__)
