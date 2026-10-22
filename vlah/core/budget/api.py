"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

The budget meter: eval machine-hours by role, approach and source; tokens are
never capped (v2.2 D43, D42 rule 2, P10). Implemented by W1-10 (#20).
"""

from dataclasses import dataclass
from typing import Protocol

from vlah.core.types.ids import Role


@dataclass(frozen=True, kw_only=True)
class BudgetView:
    total_machine_hours: float
    machine_hours_by_role: dict[str, float]
    machine_hours_by_approach: dict[str, float]
    machine_hours_by_source: dict[str, float]
    fraction_of_limit: float


class BudgetMeter(Protocol):
    def record(
        self, role: Role, approach: str | None, source: str, machine_hours: float
    ) -> None: ...

    def spent(self) -> BudgetView: ...
