"""The arm switch table (v2.2 D58): what each arm of the comparison turns on and off.

It is signed with the preregistration, so "sharing" has exactly one meaning. The
Integrator settings are not here: they live in Params, which every arm shares.
"""

from typing import Literal

from vlah.core.types.base import FrozenModel

ArmName = Literal["B0", "B1", "S1", "S2", "S2P"]


class ArmConfig(FrozenModel):
    """One arm's switches; each component reads only the fields that concern it."""

    arm: ArmName
    repo_read_scope: Literal[
        "all", "spec_approaches_own", "spec_approaches_own_candidates"
    ]
    context_composition: Literal[
        "spec_notes", "spec_own_claims", "spec_own_claims_scores", "spec_digest_diff"
    ]
    search: Literal["own_notes", "off", "candidates_only", "on"]
    diff_since: Literal["none", "off", "results_only", "on"]
    router_rule1: bool
    router_rules_2_5: bool
    router_rule6_baseline_push: bool
    needs_confirmation: bool
    hash_dedup: bool
    dead_end_presearch: bool
    failure_refresh: bool
    librarian: bool
    planner: bool
    fork: bool
    worker_compose_tasks: bool
    map: Literal["initial_own", "initial_only", "updates_no_scripts", "full"]
    # The same in every arm (D58); kept as fields so the table is complete and auditable.
    gates: bool = True
    egress: bool = True
    restart_path: bool = True
    critic_holdout: bool = True
    prescreen: bool = False


# B0 is a single agent: switches about other workers do not apply and are off.
B0 = ArmConfig(
    arm="B0",
    repo_read_scope="all",
    context_composition="spec_notes",
    search="own_notes",
    diff_since="none",
    router_rule1=True,
    router_rules_2_5=False,
    router_rule6_baseline_push=True,
    needs_confirmation=False,
    hash_dedup=False,
    dead_end_presearch=False,
    failure_refresh=False,
    librarian=False,
    planner=False,
    fork=False,
    worker_compose_tasks=False,
    map="initial_own",
)

B1 = ArmConfig(
    arm="B1",
    repo_read_scope="spec_approaches_own",
    context_composition="spec_own_claims",
    search="off",
    diff_since="off",
    router_rule1=True,
    router_rules_2_5=False,
    router_rule6_baseline_push=False,
    needs_confirmation=False,
    hash_dedup=False,
    dead_end_presearch=False,
    failure_refresh=False,
    librarian=False,
    planner=False,
    fork=False,
    worker_compose_tasks=False,
    map="initial_only",
)

S1 = ArmConfig(
    arm="S1",
    repo_read_scope="spec_approaches_own_candidates",
    context_composition="spec_own_claims_scores",
    search="candidates_only",
    diff_since="results_only",
    router_rule1=True,
    router_rules_2_5=False,
    router_rule6_baseline_push=True,
    needs_confirmation=True,
    hash_dedup=True,
    dead_end_presearch=False,
    failure_refresh=False,
    librarian=False,
    planner=False,
    fork=False,
    worker_compose_tasks=False,
    map="updates_no_scripts",
)

S2 = ArmConfig(
    arm="S2",
    repo_read_scope="all",
    context_composition="spec_digest_diff",
    search="on",
    diff_since="on",
    router_rule1=True,
    router_rules_2_5=True,
    router_rule6_baseline_push=True,
    needs_confirmation=True,
    hash_dedup=True,
    dead_end_presearch=True,
    failure_refresh=True,
    librarian=True,
    planner=False,
    fork=False,
    worker_compose_tasks=True,
    map="full",
)

S2P = S2.model_copy(update={"arm": "S2P", "planner": True})

PRESETS: dict[ArmName, ArmConfig] = {"B0": B0, "B1": B1, "S1": S1, "S2": S2, "S2P": S2P}
