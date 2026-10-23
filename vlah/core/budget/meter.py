"""The budget meter: eval machine-hours by role, approach and source (v2.2 D43, D42 rule 2).

Every judge run is charged its source's declared cost_machine_hours, cheap sources
included: the extra cheap evals S2 uses are not free. Totals live in the run's
key-value namespace as whole micro machine-hours, so concurrent writers add up
exactly (each increment is atomic) and the breakdowns always sum to the total.
Tokens are recorded too, and nothing ever caps them (P10).
"""

from dataclasses import dataclass

from vlah.core.budget.api import BudgetView
from vlah.core.types.ids import Role
from vlah.core.types.params import Limits
from vlah.ports.store import KeyValueStore

MICRO = 1_000_000
PREFIX = "budget:"
TOTAL_KEY = f"{PREFIX}total"
ROLE_PREFIX = f"{PREFIX}role:"
APPROACH_PREFIX = f"{PREFIX}approach:"
SOURCE_PREFIX = f"{PREFIX}source:"
RUNS_PREFIX = f"{PREFIX}runs:"
TOKENS_PREFIX = f"{PREFIX}tokens:"
NO_APPROACH = "(none)"


@dataclass(frozen=True, kw_only=True)
class RunCounts:
    runs_by_source: dict[str, int]
    tokens_by_role: dict[str, int]


def hours(micro: int) -> float:
    return micro / MICRO


class KeyValueBudgetMeter:
    def __init__(self, *, store: KeyValueStore, limits: Limits) -> None:
        self.store = store
        self.limits = limits

    def record(
        self, role: Role, approach: str | None, source: str, machine_hours: float
    ) -> None:
        """One judge run of source, charged its declared cost."""
        amount = round(machine_hours * MICRO)
        self.store.increment(TOTAL_KEY, amount)
        self.store.increment(f"{ROLE_PREFIX}{role}", amount)
        self.store.increment(f"{APPROACH_PREFIX}{approach or NO_APPROACH}", amount)
        self.store.increment(f"{SOURCE_PREFIX}{source}", amount)
        self.store.increment(f"{RUNS_PREFIX}{source}", 1)

    def record_tokens(self, role: Role, token_count: int) -> None:
        """Tokens are measured, never capped (P10)."""
        self.store.increment(f"{TOKENS_PREFIX}{role}", token_count)

    def totals(self, prefix: str) -> dict[str, int]:
        return {
            key.removeprefix(prefix): int(value)
            for key in self.store.scan(prefix)
            if (value := self.store.get(key)) is not None
        }

    def spent(self) -> BudgetView:
        total = self.totals(TOTAL_KEY).get("", 0)
        return BudgetView(
            total_machine_hours=hours(total),
            machine_hours_by_role={
                key: hours(value) for key, value in self.totals(ROLE_PREFIX).items()
            },
            machine_hours_by_approach={
                key: hours(value) for key, value in self.totals(APPROACH_PREFIX).items()
            },
            machine_hours_by_source={
                key: hours(value) for key, value in self.totals(SOURCE_PREFIX).items()
            },
            fraction_of_limit=hours(total) / self.limits.eval_machine_hours,
        )

    def counts(self) -> RunCounts:
        return RunCounts(
            runs_by_source=self.totals(RUNS_PREFIX),
            tokens_by_role=self.totals(TOKENS_PREFIX),
        )
