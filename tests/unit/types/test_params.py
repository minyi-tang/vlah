"""Machine-readable blocks inside the charter's markdown files."""

import pytest

from vlah.core.types.params import Limits, Target, parse_limits, parse_target

CONSTRAINTS = """# Constraints

Do not change the public API.

```yaml limits
eval_machine_hours: 96
days: 2
runs: {bench_full: 96}
```
"""


def test_limits_are_read_from_the_fenced_block() -> None:
    assert parse_limits(CONSTRAINTS) == Limits(
        eval_machine_hours=96, days=2, runs={"bench_full": 96}
    )


def test_constraints_without_limits_are_an_error() -> None:
    """The budget meter must never run without a limit (D43)."""
    with pytest.raises(ValueError, match="limits"):
        parse_limits("# Constraints\n\nNo numbers here.\n")


def test_target_is_optional() -> None:
    assert parse_target("# Progress\n") is None
    assert parse_target(
        "```yaml target\nsource: bench_full\nscore: 70\ndirection: higher\n```\n"
    ) == Target(source="bench_full", score=70, direction="higher")
