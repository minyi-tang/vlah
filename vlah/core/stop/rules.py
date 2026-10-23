"""Stop rules: budget spent, spend over the escalation fraction, target reached (v2.2 D52, C5).

Wave 1 has the minimal set from section 14. Each rule fires at most once per run;
what fired is kept in the run's key-value namespace, so a restart does not fire it
again. Events are written as the actor ci: the actor table is a frozen contract
with no budget-meter entry, and ci has the eval service's write priority and no
other writer in Wave 1.

- budget spent: the meter reaches Limits.eval_machine_hours, or any source reaches
  its Limits.runs count -> stopped(reason="budget"). The runtime then runs the
  Integrator's closeout; this rule never stops anything itself.
- spend over the fraction in escalation.md (total_spend_over_fraction, 0.8) ->
  one escalation event with the options and the default when unanswered.
- target reached: the deliverable's top-level score meets Target -> one escalation.
  A human decides whether to continue; nothing stops.
An escalation event has no payload model, so its options are in reason and the
evidence lists what the human should look at.
"""

import json
import logging
from dataclasses import dataclass

from vlah.core.blackboard.api import Blackboard
from vlah.core.budget.meter import KeyValueBudgetMeter
from vlah.core.integrator.api import IntegratorService
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.events import Event, event_to_json
from vlah.core.types.ids import ActorId, format_event_local_id
from vlah.core.types.params import Limits, Target
from vlah.ports.store import KeyValueStore

logger = logging.getLogger(__name__)

STOP_ACTOR = ActorId("ci")
FIRED_PREFIX = "stop:fired:"
EVENT_COUNT_KEY = "stop:event_count"
DEFAULT_ESCALATION_FRACTION = 0.8
DEFAULT_TIMEOUT_HOURS = 8.0
DEFAULT_ACTION = "freeze_affected_approach"
RULE_BUDGET = "budget_spent"
RULE_SPEND = "spend_over_fraction"
RULE_TARGET = "target_reached"


@dataclass(frozen=True, kw_only=True)
class EscalationPolicy:
    """escalation.md: the fraction that escalates, and the default when unanswered."""

    total_spend_over_fraction: float = DEFAULT_ESCALATION_FRACTION
    timeout_hours: float = DEFAULT_TIMEOUT_HOURS
    action: str = DEFAULT_ACTION


class TopLevelScores:
    """The top-level score of a baseline's candidate, from candidates/<hash>/result.json."""

    def __init__(self, *, blackboard: Blackboard, source: str) -> None:
        self.blackboard = blackboard
        self.source = source

    def score_of(self, baseline: BaselineRecord) -> float | None:
        if baseline.added is None:
            return None
        else:
            path = f"candidates/{baseline.added}/result.json"
            if path not in self.blackboard.list_paths(path, None):
                return None
            else:
                data: object = json.loads(self.blackboard.read(path, None))
                levels = data.get("levels") if isinstance(data, dict) else None
                for level in levels if isinstance(levels, list) else []:
                    if isinstance(level, dict) and level.get("level") == self.source:
                        score = level.get("score")
                        return float(score) if isinstance(score, (int, float)) else None
                    else:
                        continue
                return None


def meets(target: Target, score: float) -> bool:
    if target.direction == "higher":
        return score >= target.score
    else:
        return score <= target.score


class BudgetStopRules:
    def __init__(
        self,
        *,
        store: KeyValueStore,
        blackboard: Blackboard,
        meter: KeyValueBudgetMeter,
        integrator: IntegratorService,
        limits: Limits,
        target: Target | None,
        policy: EscalationPolicy,
    ) -> None:
        self.store = store
        self.blackboard = blackboard
        self.meter = meter
        self.integrator = integrator
        self.limits = limits
        self.target = target
        self.policy = policy
        self.scores = (
            TopLevelScores(blackboard=blackboard, source=target.source)
            if target is not None
            else None
        )
        # A baseline's score never changes once known; evaluate runs every tick.
        self.score_by_baseline: dict[int, float] = {}

    def claim_rule(self, rule: str) -> bool:
        """True for the one caller that fires this rule in this run."""
        return self.store.compare_and_set(f"{FIRED_PREFIX}{rule}", None, b"1", None)

    def fire(
        self, rule: str, event_type: str, now_seconds: float, **fields: object
    ) -> Event:
        """Write the rule's event; if the write raises, the rule is due again next tick.

        The supervisor swallows exceptions from evaluate, so a claim left in place
        after a failed write would silence the rule, and a run that should stop
        would keep spending until its time limit.
        """
        try:
            return self.write(event_type, now_seconds, **fields)
        except Exception:
            self.store.delete(f"{FIRED_PREFIX}{rule}")
            raise

    def write(self, event_type: str, now_seconds: float, **fields: object) -> Event:
        number = self.store.increment(EVENT_COUNT_KEY, 1)
        local_id = format_event_local_id(STOP_ACTOR, number)
        event = Event.model_validate(
            {
                "local_id": local_id,
                "ts": now_seconds,
                "actor": STOP_ACTOR,
                "role": "ci",
                "type": event_type,
                **fields,
            }
        )
        receipt = self.blackboard.write(
            STOP_ACTOR,
            {f"events/{local_id}.json": event_to_json(event).encode("utf-8")},
            f"{event_type} by the budget meter",
        )
        if receipt.status == "rejected":
            logger.error(f"the blackboard rejected {event_type}: {receipt.violations}")
        else:
            pass
        return event

    def exhausted_source(self) -> str | None:
        counts = self.meter.counts().runs_by_source
        for source, cap in sorted(self.limits.runs.items()):
            if counts.get(source, 0) >= cap:
                return source
            else:
                continue
        return None

    def evaluate(self, now_seconds: float) -> Event | None:
        """The first rule that fires now; each fires once per run, stopped last."""
        view = self.meter.spent()
        source = self.exhausted_source()
        if (
            view.fraction_of_limit >= self.policy.total_spend_over_fraction
            and self.claim_rule(RULE_SPEND)
        ):
            return self.fire(
                RULE_SPEND,
                "escalation",
                now_seconds,
                target=RULE_SPEND,
                reason=(
                    f"{view.total_machine_hours:.2f} of {self.limits.eval_machine_hours:.2f} "
                    f"eval machine-hours spent ({view.fraction_of_limit:.0%}). Options: "
                    "continue, stop, revise the charter. Default when unanswered for "
                    f"{self.policy.timeout_hours:g} h: {self.policy.action}."
                ),
                evidence=["charter/constraints.md", "charter/escalation.md"],
            )
        elif self.target_met() and self.claim_rule(RULE_TARGET):
            assert self.target is not None
            return self.fire(
                RULE_TARGET,
                "escalation",
                now_seconds,
                target=RULE_TARGET,
                reason=(
                    f"The deliverable reached the target {self.target.score:g} on "
                    f"{self.target.source}. Options: continue, stop. Nothing stops "
                    "on its own; in testbed runs the runner's time limit governs."
                ),
                evidence=[
                    "charter/progress.md",
                    f"baseline/{self.integrator.current().k}.json",
                ],
            )
        elif (view.fraction_of_limit >= 1.0 or source is not None) and self.claim_rule(
            RULE_BUDGET
        ):
            what = (
                f"{source} reached its {self.limits.runs[source]} runs"
                if source is not None
                else f"{view.total_machine_hours:.2f} eval machine-hours reached the limit"
            )
            return self.fire(
                RULE_BUDGET,
                "stopped",
                now_seconds,
                reason=what,
                evidence=["charter/constraints.md"],
                data={"reason": "budget"},
            )
        else:
            return None

    def target_met(self) -> bool:
        if self.target is None or self.scores is None:
            return False
        else:
            baseline = self.integrator.current()
            score = self.score_by_baseline.get(baseline.k)
            if score is None:
                score = self.scores.score_of(baseline)
                if score is not None:
                    self.score_by_baseline[baseline.k] = score
                else:
                    pass
            else:
                pass
            return score is not None and meets(self.target, score)
