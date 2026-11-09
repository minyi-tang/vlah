# Role prompts and capabilities

How every LLM role in VLAH gets its prompt and its tools. This is the design for the alpha; it extends the worker prompt in `vlah/prompts/worker/` (W1-08, revised in #145) to the other roles without rewriting it.

> **Status:** implemented for the worker, 2026-11-02 (`vlah/prompts/`). Nothing here changes a contract. The worker is the only role that runs in the alpha; the other profiles are described so that adding one later is a profile, not a redesign.

## Four rules

1. **The prompt says the mission and the taste. Code enforces everything else** (D35). The Router, the tool server, the egress policy and the allowed-tool list are the rules. The prompt only tells the agent what will block it, so nothing surprises it.
2. **Every role is assembled the same way from the same parts.** Roles differ in three things only: the mission file, the capabilities granted, and what the store lets them see. No role has a hand-written system prompt of its own.
3. **A capability is a bundle, not a prompt edit.** Shell, the herd tools, a search MCP, computer use: each is one package holding its tool allowlist, its MCP server config, its egress hosts and one short card. Granting it to a role adds the card to the prompt and the tools to the harness together. Adding a capability never touches a mission file.
4. **The prompt is frozen for a comparison and changes one item at a time.** Each prompt part is a file in the repo. A run records the hash of the assembled prompt. A change to any part gets a `docs/DECISIONS.md` row and lands between comparisons, never during one (D42, P11's "add one rule at a time, rerun after each").

This follows the split DeepSeek's `dsh-superagent-prototype` makes in its prompt docs: facts enforced by tools and the host rank above prompt rules, prompt rules above skills and team sections, and message content is data. The Router there routes only and the Executor judges and implements; VLAH already has this split in code (D13, D15, D16).

## Assembly order

Every session's prompt is these sections, in this order, each from its own markdown file. The order is fixed so that a change to a later part leaves the earlier text, and its cache prefix, unchanged.

| # | Section | Source | Varies by | Budget |
|---|---|---|---|---|
| 1 | Mission | `prompts/<role>/mission.md` | role | 150 words |
| 2 | Workspace | `prompts/<role>/workspace.md` | role | 80 words |
| 3 | Boundaries | `prompts/boundaries.md`, the invariants, plus one line per capability | capabilities | 250 words |
| 4 | Tool cards | `prompts/cards/<name>.md`, one per granted capability | capabilities | 250 words each |
| 5 | Suggestions | `prompts/<role>/suggestions.md` | role | 120 words |
| 6 | Your task | the task's goal and refs, from the queue | task | as the queue wrote it |

`vlah/prompts/assemble.py` renders them; `render_worker_prompt` is now a one-line wrapper over the worker profile, so both harnesses kept their call.

The material (spec, charter, own claims, digests, scores) stays in CONTEXT.md, composed per arm by `core/worker/start_pack.py` (D58, D77), and so do the notices the herd appends during a session (an interrupt, a restart note). The prompt never quotes it and never tells the agent what to read (D1).

Budgets are tested, not hand-counted: `tests/unit/harness/test_prompts.py` fails when a file exceeds its budget. The whole worker prompt without CONTEXT.md is about 850 words.

## Capabilities

A capability is one value in `vlah/prompts/capabilities.py` plus its card in `vlah/prompts/cards/<name>.md`. The alpha has two, `herd` and `shell`.

```python
@dataclass(frozen=True, kw_only=True)
class Capability:
    name: str
    tools: dict[HarnessKind, tuple[str, ...]]   # tool patterns per harness; Copilot allows every tool, so it lists none
    mcp_servers: tuple[McpServer, ...]          # static servers; the herd's own is configured per session
    allowed_urls: tuple[str, ...]               # hosts the sandbox may reach for it (D75)
    boundary: str                               # one line for the boundaries section
    card: str                                   # cards/<name>.md
```

The harness takes its allowed-tool list, its MCP config and the egress hosts from the role's capability set. `ClaudeCodeConfig.allowed_tools` remains as an override for one run. `--strict-mcp-config` stays: only the servers the capabilities name load. A search server later is one more `Capability` with an `McpServer`, its host and a card; computer use is the same with that harness's tool names.

A card says what the tools are for and the one or two things the agent must know to use them, in the agent's terms. It never repeats the tool schema, which the MCP server already sends. The herd card carries the candidate and claim formats from #145, moved out of the boundaries.

What a capability may not do: grant a tool that reaches eval definitions, hidden data or another agent's sandbox (D10, D63), or give a worker push credentials (D68). The boundary check in `testing/boundary.py` runs against every capability set, so a new bundle cannot widen the sandbox by accident.

## Role profiles

A profile names a mission and a capability set. It is a plain mapping in `vlah/prompts/roles/<role>/profile.py`; the alpha registers only the first two.

| Role | Capabilities | Sees | Status |
|---|---|---|---|
| worker | herd, shell | its arm's CONTEXT.md, its blackboard view | built; prompt from #145 |
| composer | herd, shell | the two candidates and the baseline, no arm context (D74) | built; same profile as worker with a compose task brief |
| critic | herd, shell, eval_code (private store, read only) | everything, including the audit rationale area (D11, D63) | holdout half in code; LLM audit is Wave 3 |
| librarian | herd | all claims of its approaches | deferred (roadmap open question 2) |
| planner | herd (decisions only) | all digests and eval statistics | Wave 3 |
| pi | herd, human (the inbox) | the charter and the public audit conclusions | Wave 3 |

The composer is not a role of its own: the Integrator starts an agent with the worker profile and a compose task, and the agent runs outside every arm's pool with its output going back to the Integrator only (D74). The profile system does not need to know about it.

The herd.yaml of a run can set the worker's capability set by name, which is how a worker gets a search server or computer use for one problem once that capability exists:

```yaml
harness:
  kind: claude_code
  settings:
    capabilities: [herd, shell, web_search]
```

It sits under `harness.settings` because `HerdConfig` is a frozen contract and a top-level `roles:` key would need a contract PR. Workers never see herd.yaml (D63); the assembly reads it, the harness gets the result. An unknown name fails the run before any session starts.

## The mission files

The mission is the only prose that differs by role, so it is where the design effort goes. Each one is a direction, not a procedure. Drafts, to be revised in their own PRs:

**worker** (as in #145): one of several workers on a hard optimization problem; improve the shared solution and leave findings later work can build on; choose your own methods within the problem's constraints; the judge is slow and scarce, so use judgment about what is worth measuring; distinguish what you observed from what you suspect.

**critic**: the herd's auditor. When a candidate jumps or enters the top, explain why it scores, check it on the holdout, compare it with recorded dead ends, and look for anything that games the judge rather than the problem. Write a public conclusion and a private rationale; the rationale never reaches the blackboard, because "why it scores" is a hacking manual (D11). For a composite candidate, say whether its parts add, overlap or conflict (D64).

**librarian**: an editor, not a gatekeeper (D14). Each round, read the new claims of your approaches, tag them, and rewrite the digest in its fixed structure: current best, active hypotheses, corroborated insights, proposed insights, dead ends, open questions, under 2,000 tokens. Distill insights from high-scoring diffs. Never decide what anyone works on.

**planner**: read every digest and the eval statistics; write decisions only, each with a rationale: budget share per approach, freeze or divest, queue order, a call on the Librarian or the Critic. The allocation rule is deterministic; override it only with a stated reason, and write anything on the escalation list as a request to the PI (D15).

**pi**: the one agent that talks to the human. Negotiate the charter; once it is signed, write spec.md and the approaches and start the first wave. At runtime, be the console: answer the human, raise what needs their attention, and nothing else (§10).

No mission tells the agent how to investigate. Method advice goes in `suggestions.md`, which opens with "These are suggestions, not rules" and may be ignored (P11).

## What this changed in code

1. **Assembly** (`vlah/prompts/`): `assemble(profile, agent, task)` renders the sections; `render_worker_prompt` wraps it for the worker profile. The worker's files are `prompts/worker/{mission,workspace,suggestions}.md`; the invariants are `prompts/boundaries.md`; the cards are `prompts/cards/{herd,shell}.md`. A `RoleProfile` (`prompts/profile.py`) is a role name, its prompt package and its capabilities.
2. **Harness** (`adapters/harness/claude_code`, `copilot`): both configs carry a `profile`; the tool allowlist, the MCP config and the egress hosts come from it. The runtime builds the profile from `harness.settings.capabilities`. The ports did not change: `AgentSpec` already carries `command`, `files` and `egress`.

Not in scope: new roles, scheduling, the Critic audit, any contract change. A profile for a role that does not run is a mission file and a table row, nothing more.

## How we keep it from drifting

- A run's status file should record the hash of each role's assembled prompt, so two runs are comparable only when the hash matches, and the H1 preregistration names it. The status schema does not have the field yet.
- One prompt change per PR, with a DECISIONS row and the scorecard item it is meant to move. If no item names it, it waits.
- The prompt's effectiveness is not claimed from reading it. It is claimed from a real-run comparison, as #145 says.
- A rule moves from a suggestion to a boundary only after a scorecard item loses to the isolated arm, and then one rule at a time (P11).

## Open questions

1. Should the budgets be words or tokens? Words, counted by a test, since the tokenizer differs by model and the point is discipline, not an exact window.
2. Does the composer get its own `suggestions.md` (how to resolve a conflict), or does the compose task brief carry that? The brief, since the Integrator writes it and it is per conflict; nothing is built for it yet.
3. Where does the prompt hash go: the status file, or the session's event? Open until the status schema changes.
