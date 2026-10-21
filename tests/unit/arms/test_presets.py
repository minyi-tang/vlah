"""The arm presets must match the v2.2 D58 switch table cell by cell."""

from vlah.arms.config import PRESETS

# Copied from D58. B0 is a single agent, so switches about other workers are off.
SwitchValue = str | bool

D58_TABLE: dict[str, tuple[SwitchValue, SwitchValue, SwitchValue, SwitchValue]] = {
    # Columns: B0, B1, S1, S2.
    "repo_read_scope": (
        "all",
        "spec_approaches_own",
        "spec_approaches_own_candidates",
        "all",
    ),
    "context_composition": (
        "spec_notes",
        "spec_own_claims",
        "spec_own_claims_scores",
        "spec_digest_diff",
    ),
    "search": ("own_notes", "off", "candidates_only", "on"),
    "diff_since": ("none", "off", "results_only", "on"),
    "router_rule1": (True, True, True, True),
    "router_rules_2_5": (False, False, False, True),
    "router_rule6_baseline_push": (True, False, True, True),
    "needs_confirmation": (False, False, True, True),
    "hash_dedup": (False, False, True, True),
    "dead_end_presearch": (False, False, False, True),
    "failure_refresh": (False, False, False, True),
    "librarian": (False, False, False, True),
    "planner": (False, False, False, False),
    "fork": (False, False, False, False),
    "worker_compose_tasks": (False, False, False, True),
    "map": ("initial_own", "initial_only", "updates_no_scripts", "full"),
    "gates": (True, True, True, True),
    "egress": (True, True, True, True),
    "restart_path": (True, True, True, True),
    "critic_holdout": (True, True, True, True),
    "prescreen": (False, False, False, False),
}


def test_presets_match_d58() -> None:
    for field_name, expected_values in D58_TABLE.items():
        actual_values = tuple(
            PRESETS[arm].model_dump()[field_name] for arm in ("B0", "B1", "S1", "S2")
        )
        assert actual_values == expected_values, field_name


def test_table_covers_every_switch() -> None:
    assert set(D58_TABLE) == set(PRESETS["S2"].model_dump()) - {"arm"}


def test_s2p_differs_from_s2_only_by_the_planner() -> None:
    differences = {
        field_name
        for field_name, value in PRESETS["S2P"].model_dump().items()
        if value != PRESETS["S2"].model_dump()[field_name]
    }
    assert differences == {"arm", "planner"}
