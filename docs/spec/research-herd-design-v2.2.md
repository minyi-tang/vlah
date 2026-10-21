# Research Herd Design (v2.2)

A large-scale agent collaboration system for research · Oct 17, 2026 · @Minyi

*English translation of the Chinese original (`zh/research-herd-design-v2.2.md`). IDs, numbers and section numbers are unchanged.*

## 0. How to read this document, and status tags

The goal of this document is not to get everything right in one step. It is to let the first PoC round get built quickly and start running, and, when it breaks, to make clear what to change. To that end, every design decision carries a status tag that says whether it can change and how. The decision register in §15 lists every decision by number. It is the single checklist for implementation and for retrospectives.

| Tag | Meaning | What to do when the PoC fails |
| --- | --- | --- |
| \[Constraint\] | Imposed by the environment or the problem; changing it means changing the problem | Don't change the design; change the problem definition or the environment |
| \[Derived P#\] | Derived from one of the principles in §2. If the principle holds, it holds | Question the principle first; if the principle stands, the derived decision stays |
| \[Hypothesis H#\] | Our guess. Registered in §3, with a validation experiment in §11 and a fallback in §15 | Switch to the fallback in the register; don't redesign |
| \[Default\] | An initial parameter value, with the reason for choosing it; tune freely | Tune it |

Sentences without a tag are explanation, not decisions. There are two kinds of readers. An agent that will implement this should first read the discussion trail in §0.1 to align on the thinking, then the minimum viable version in §14 and the register in §15, and then go back to other sections as needed. A person who wants to judge whether it is right should first read §1 to §3; all the premises are there.

The v1 of this document went through one independent review, recorded in Appendix A. v2 is a rewrite on top of v1, and every problem the review raised has been folded into the main text. Appendix A is kept so people can see where the first draft went wrong. Appendix B is an event-by-event walkthrough of a hypothetical case. v2.2 (2026-10-18): a coding agent self-reviewed v2 and produced a v3. After an independent review, we decided not to adopt its change of direction (splitting tasks by lever, a control group with static division of labor). We folded into this document only the improvements unrelated to that change of direction: D72 to D77, the restart path in the hook table in §7, §8 rule 6, the testbed changes in §14.1, and the verdict criteria for H1 and H3. The review is recorded in *Research Herd v3 Review* (not in the repo). Round 29 then removed every mechanism that existed to save tokens (P10; D47 withdrawn). Round 30, following Opus's review (file: *v2.2 Fixes Review*, `research-herd-v2.2-fixes.md`), fixed two statistical errors (the spot-check rule and the interaction threshold), made the merger the same across arms, deferred D78, and wired D72 to D78 into the schema, the component table, the arm-switch table, the L0 tests and Appendix C.

## 0.1 Discussion trail: how this design came about

The point of this section is not to learn the history. It is to know which question each design answers. When you want to change a decision, first find the row it corresponds to, and confirm that the question still has an answer after your change. The design came out of two days of probing questions, round after round. Each round was a specific pain point or a tightened constraint, and each overturned half of the previous round's answer.

| # | What this round asked | Turn or insight | Where it landed in the document |
| --- | --- | --- | --- |
| 1 | 100 machines and 50 agents solving one hard problem: is there an off-the-shelf orchestration framework? How would you get 10,000 agents to solve a Millennium Prize problem? Not simple divide & conquer; there must be a lot of communication | Off-the-shelf frameworks solve "how fast does each agent run", but the essence of this problem is shared cognitive state: a blackboard, verifiers as the trust layer, division of labor by function rather than by subproblem, attention as the real bottleneck, diversity managed explicitly | P3, P7; role table (§4) |
| 2 | Most of the problems I want to solve have an external eval | It shifts from "research institute" to "search": the archive from the AlphaEvolve line of work can be borrowed; reward hacking is a certainty, not a risk, and sharing amplifies it; best-of-N is a control that must be run | Anti-hacking (§9), B1 baseline (§11) |
| 3 | Evals are expensive and slow, and can't support high concurrency | The scarce resource is neither agents nor tokens; it is eval slots. The agent's job shifts from generating to predicting; tiered fidelity, peer review before submission, submissions that carry predictions | C1, D37, D40; ladder (§9) |
| 4 | Information sharing is the core, but when I did this before, handoffs always lost information: A sees a lead, hands it off, and it is gone, and nobody else can recover it | The root cause is that compression happens before the value is known. Agents have one capability humans lack: context can be copied. Fork instead of handoff, ask back, summaries only as an index, push turned into pull, leads made into executable artifacts | P2; D4 to D8 |
| 5 | A blackboard works, but how often does each agent read it? | Read by event and state, not by time: a fixed frequency has no correct value. Hard interrupt, soft injection, on-demand search; read more when stuck | P5; D17 to D19 |
| 6 | Then does the blackboard need a very strong agent to manage it? | Half right: it needs strong judgment, but that judgment can't sit on the read path, or it becomes a new lossy compressor and a single source of bias. Judging what matters and controlling who can see what must be two separate things | P4; D13 to D16 |
| 7 | The blackboard could actually just be a git repo | Yes, and the concepts map almost one to one. What git lacks is notification, semantic search, structured queries and large files; add a thin layer for each. Append-only is a mandatory convention | D30 to D33 |
| 8 | I've used kanban-style dispatchers. A problem that a single strong agent can solve gets worse when it is split up: one agent reads email, one reads Slack, and after merging, the information is all lost; the click never happens | This is the most fundamental objection to multi-agent systems, and it is right. The click only happens inside a single context. What runs in parallel should be attempts, not materials; the join is done by one mind; a task is a goal, not an instruction | P1; D1 to D3 |
| 9 | Then how do large numbers of agents avoid this on research problems? | Spend scale on the number of attempts, not on slicing up the problem; no up-front decomposition; layers split by abstraction, not by material; nobody owns the cross-approach click, so it has to be put on the schedule | P8; D27, Scout |
| 10 | I still haven't mapped these principles onto engineering practice that can actually be built | Written up as a system for the first time: repo layout, five services, the worker loop and hooks, baseline, metrics, build order | Skeleton of §5 to §8, §11, §14 |
| 11 | I don't want the herd to do only single-objective optimization. I want a place where I can discuss things with it in detail and get aligned before it starts | Humans are the densest information source, and also the easiest to lose through compression. PI agent, charter, teach-back, full discussion text stored in the repo, first wave forked from the PI, escalation; what persists across problems is the PI, not the herd | P6; D20 to D23; §10 |
| 12 | Does this need the strongest model to refine each agent's goal? | Half right: the strong model refines the problem, not each agent's instructions; otherwise it is the dispatcher trap again. Workers can't be weak either, because the click happens in the worker | Model tiers (§12), D47 |
| 13 | Write the design doc, self-review it, walk through a hypothetical case | v1 was written. The biggest hole the independent review found was a broken loop: when eval results came back, nobody picked them up. The easiest mistake to make when deriving downward from principles: not walking through one worker's day from start to finish | P9, D28; Appendices A, B |
| 14 | Not every problem has a fixed eval: optimizing a harness codebase has one; abstract problems only have a topline, and it isn't fixed | Three forms; in form B, changing the ruler is part of the work; only the topline can move budget, a hard anti-Goodhart rule | D12; metric claim; the three forms in §9 |
| 15 | The eval might be an external tool, a local dataset, Kusto, or flight signals; there has to be flexibility | The eval is not one component; it is a plugin interface for a set of signal sources. Sources declare their properties, and the ladder, calibration and budget are derived from the declarations; exposure attribution | C6, D39 |
| 16 | One final revision: the logic has to be coherent; every design must say clearly whether it is a hard constraint, a guess, or derived from a principle, so that after a PoC failure another agent doesn't have to guess; don't aim to get it all right in one step; land it quickly and keep it improvable | v2 rewrite: status tags, hard-constraint table, principle table with falsification conditions, hypothesis register, decision register, minimum viable version | §0, §1 to §3, §14, §15 |
| 17 | Too many machines were given to eval; eval is usually external. What is unclear is how to keep exploration both broad and deep, and how to add machines and agents dynamically when a good direction is found | Once eval is external, the cluster is essentially attention capacity. The unit of allocation is the slot; quick reaction is kept separate from making it permanent (surge); depth comes from fork, not from headcount; breadth relies on a hard floor; the first version's allocation is a deterministic rule | §9.1; D49 to D51 |
| 18 | Is this thinking reasonable? What similar things haven't I thought of yet? | Besides the success case, the failure case also needs an answer: when to stop, how to allocate human attention, hard constraints as gates, not as scores; the rest are listed as open questions | §9.2; D52 to D54 |
| 19 | What other problems are there? Is it enough to hand to an agent to start the PoC? | A review from the implementer's point of view: the task queue, the event schema, the semantics of eval\_request, and the arms of H1 were all left open. What you give the agent is not "go implement the design"; it is "go validate H1", and the first deliverable is an implementation plan, not code | D55 to D58; Appendix C |
| 20 | I have an existing k8s / Terraform cluster repo — but this should be layered: what the agents are launched with and how each agent is implemented should not be its concern; implement the effective parts together, without gluing them to a particular cluster repo or agent implementation | What we need to build is a standalone runtime + orchestration: four layers, six ports; herd tools are exposed as an MCP server so the harness is replaceable; one real adapter plus one in-memory adapter per port; the in-memory version is where iteration speed comes from | §4.1; D59 to D61; H12 |
| 21 | External review: H1's scores aren't trustworthy; workers can read hidden material; hooks miss built-in tools; fork doesn't solve a full context; D51 contradicts D12 | All five are facts about git and the harness, not design preferences: the verdict now re-scores self-selected candidates on a holdout; private store; three layers of hooks; a full context calls for compaction, not fork; allocation consumes only the top level | D62, D63; D60, D5, D51 rewritten |
| 22 | Optimization isn't just finding one optimal path. Different paths each improve different places, and your own example is a combination. Why does the design say best-of-N? | The name leaked from the control arm into the verdict. The output is a combination, not a single point: candidates with multiple parents, composition tasks, interaction claims; the control arm also merges after it finishes, and H1 tests the increment from sharing during the run; approaches are a portfolio, not bandit arms. Folded in during the same round from the external review: two holdouts, physical isolation of arms, push credentials, candidate format | D64 to D69; D62, D58, D51 rewritten; H13 |
| 23 | I'm about to start implementing the backbone, but it can't be just unit tests. I need a few good problems and a simple case that doesn't take much time, to confirm it works; that beats hooking it straight into a big project and running for two days before finding problems | A layer is missing between unit tests and H1. Three-level acceptance: L0 plumbing assertions; L1 a purpose-built synthetic problem, Knobs (each group of knobs targets one mechanism and serves as the positive control for the backbone), plus 10 auto-judged problems; L2 real problems. Don't move to the next level until the previous one is all green | D70; §14.1 |
| 24 | The test problems can't be so easy that one agent can finish them; the external eval harness needs disclosure; build these test guardrails first — this is a design in its own right, no smaller than a separate one | Knobs was changed so the material exceeds a single context, with B0 as the control arm. Eval trustworthiness was split out into its own design, *Ledger*: the threat model as the spine, twelve invariants, trust boundaries, the ledger, Vault, circuit breakers, a verdict engine, attack scripts as acceptance; Critic moved into the trust zone; Ledger before Knobs before the herd | Written at the time as *Ledger: Eval Trust Layer Design* (superseded, not in the repo); overturned in round 25 and rewritten as *Herd Testbed* (`herd-testbed.md`) |
| 25 | This isn't an eval trust layer; it is a guardrail we design ourselves, simulating a problem cheaply to benchmark our own behavior. The document isn't written in plain language. Is there simple open source we can reuse? | Ledger cut down to a testbed: five parts (problem catalog, fast tier, slow tier, recorder, scorecard + control groups); only three anti-cheating rules kept. The document restructured with the plain-language version first and the part for implementers after. Open source: SimPy, FastAPI + Redis, Inspect AI optional; Gas Town borrowed for concepts but not used wholesale | *Herd Testbed* rewritten in full; in this document, §6, §14, §14.1, Appendix C and D63 updated to cite it |
| 26 | The earlier use cases only had a large context; an agent could also handle them with bash or sampling. 4 works; it is really that there are many strategies and a single agent can't explore many directions at once. 5 works too. Use just these two for now | The selection criterion becomes: number of directions to iterate on × wait time per round > wall-clock time, and the improvements are composable, there are traps, and there is quota pressure; lots of material doesn't count. Knobs and the two other candidates were eliminated. Settled on A (a simulator scheduling policy, with 5 ingredients, 1 trap and 1 conflict planted) and B (a harness with 5 planted bugs), A first, then B. What the herd has to win on is not breadth; it is combining, not re-hitting, and quota tilt | *Herd Testbed* §1, §3, §4; in this document, §14.1 L1 rewritten, D71 |
| 27 | Had a coding agent self-review v2, and it produced v3; please verify it. I don't actually understand: do research across multiple aspects and deep optimization in each aspect conflict? I can't follow your mix of Chinese and English | v3 is a change of direction, not a revision: tasks split by lever, a Mapper added, the control group changed to static division of labor; its own simulations don't support these conclusions. Breadth and depth don't conflict. What v3 really bets on is "assign people by lever from the start", and what it loses is multiple paths attacking the same direction. v3 brings a few good things that are independent of the change of direction: a verdict protocol, paired measurement, a ratchet in the merge process, a restart path that doesn't depend on checkpoints, an egress allowlist; its three criticisms of the testbed hold | *Research Herd v3 Review*; this section |
| 28 | I think v2 is better; OK (fold in the improvements, produce v2.2) | v2 is the trunk; the task form is unchanged (each worker takes one approach and attacks the whole problem, D1, D24); no assigning work by lever, no Mapper, no B2 arm. Only the improvements unrelated to the change of direction are folded in, with all text written out clearly in Chinese | D72–D77; the hook table in §7; §8 rule 6; §14.1; the H1 and H3 rows; three changes in *Herd Testbed* |
| 29 | Tokens are unlimited; my point is that models and tokens should not hurt development efficiency or add engineering complexity | What the design has really been saving all along is eval slots, wall-clock time, context windows and human attention, not tokens. But a few things were added to save tokens or money, and all of them are removed: model tiers and H6 (all roles use the same strongest model), the Librarian's batching parameters, the token budget in the Budget meter and in constraints.md, the cost item in the scorecard, the testbed's token cap. Conversely, anything that costs only model calls and no eval can be done redundantly. The only thing left to check is the API's concurrency and rate limits | P10; D47 withdrawn; D78; §6, §12; *Herd Testbed* §6, §7, §11 |
| 30 | Opus reviewed v2.2 and produced a revision list; check it yourself and fix whatever is valid. Decisions on four items that needed a human decision: the merger is the same across arms; pre-screening is deferred; S1 receives baseline advances and the map; the weekly plan does not roll over and is only illustrative | Every item on the list holds, and three simulations were rerun with consistent results. Two statistical errors: the spot-check rule compared against the measurement that had been selected (winner's curse), giving 58% to 99% false alarms even when nobody cheats; changed to taking two new measurements of the same segment and comparing the difference. The 70% rule for interactions gave false alarms more than half the time; changed to the difference exceeding 2σ√2. One confound: the merger had been given only to S2; changed so that all four arms run it during the run with the same configuration, and "sharing" refers only to whether workers can see baseline advances. One leak: pre-screening's veto reasons would leak to workers why something scores, so pre-screening is deferred. One implementation gap: D72 to D78 had not been wired into the schema, the component table, the arm-switch table, L0 and Appendix C; now filled in. The number of runs is no longer hard-coded at 3; the closeout order is fixed; the map's first batch of scripts is written by humans. Knock-on effect: the merger moves up to step 1 of the testbed | D62, D65, D69, D72–D79; §5, §6, §8, §14; Appendix C; *Herd Testbed* and *One-page Alignment* (`one-page-alignment.md`) synced; review file *v2.2 Fixes Review* (`research-herd-v2.2-fixes.md`) |

**Three threads that run through the whole process**

1. The definition of the problem moved from "scheduling" to "information and attention". Round 1 changed it from a scheduling problem into a shared-cognitive-state problem. Round 4 traced the obstacle to sharing to the timing of compression. Round 8 traced the error in division of labor to slicing by material. Round 17 identified the essence of the cluster as attention capacity. Each step reduced a problem that looked like engineering back to a cognitive problem.
2. Each time a constraint tightened, it reversed the design's center of gravity. Eval exists → search; eval is scarce → prediction; eval is external → allocation; eval isn't fixed → changing the ruler is also work. When readers get a new constraint, they should expect the same kind of reversal, rather than patching the existing design.
3. From principles to engineering, to abstraction, to the minimum version: every external review found a loop that the previous step had not closed. This is a conclusion about how the design itself works: correct principles don't guarantee a correct system. You have to walk through one worker's day, and one implementer's first day, from start to finish. So the completion criterion for this document is not perfection. It is that it can be handed to an agent to start work, and that when it fails, you know what to change.

**Two beliefs that never changed**, and that are the premise of everything else: the value of multiple agents lies in the number of independent attempts, not in splitting one mind's work across several minds; and any collaboration mechanism that cannot beat best-of-N under the same budget is not worth keeping. If you want to change these two, what you are changing is not this design; it is the problem.

## 1. Problem and hard constraints

**Problem**: Given a research or engineering problem with an external eval signal, a cluster of 100 machines, and a few dozen LLM agents each with a full harness, design a system that lets them collaborate so that, under the same eval budget, the final combination they deliver consistently beats best-of-N (each running on its own, merged by the same merger, with no sharing). Note that the deliverable is usually a combination of improvements from multiple approaches, not a single point on one path (D64). At the same time, give humans a persistent entry point for alignment: discuss things fully before launch, and step in at any time during the run.

The eight items below are imposed by the environment; they are not design choices. After each one is the design direction it forces. Every decision in the main text can be traced back either to here or to §2.

| # | Constraint | The design it forces |
| --- | --- | --- |
| C1 \[Constraint\] | Evals are expensive and slow, have limited concurrency, and are usually an external system: submit, wait, collect results; the cluster does not run them. Typical values: hours to days per eval, a few dozen per day | Eval is the scarce resource. The system is designed around "where each eval is spent", and the agent's job is to predict, not only to generate (§9) |
| C2 \[Constraint\] | A single agent's context is limited | Agents can't each see all output; there must be filtering, layering and on-demand search (§8) |
| C3 \[Constraint\] | LLM output has variance, and LLMs exploit holes in the eval | Every score carries n and an interval; the eval definition is unreadable to workers; sharing amplifies hacking, and the defense lives on the eval side (§9) |
| C4 \[Constraint\] | Agents die, contexts fill up, tasks get interrupted | State can't live only inside agents; workers are stateless, and state lives in the repo and in checkpoints (§5, §7) |
| C5 \[Constraint\] | Human time is the scarcest and densest signal | Human interventions are queued, budgeted and kept losslessly; the herd doesn't read humans, it reads the charter (§10) |
| C6 \[Constraint\] | Different problems have different eval forms: external tools, local datasets, Kusto queries, flight signals, humans | Eval is a plugin interface; the ladder and quotas are derived from declarations (§9) |
| C7 \[Constraint\] | The 100 machines are mainly for workers and their sandboxes (local proxies run here); workers are API-bound, and the instantaneous token rate is limited by API quota | Machine allocation and backoff strategy (§12) |
| C8 \[Constraint\] | The first version runs 50 agents; the design target is a few hundred, not a few thousand | A single git repo as the blackboard is the simplest choice within the feasible range (§5) |

**Non-goals**

- Not a general workflow engine. No kanban-style task-DAG dispatch.
- No model training. Differences come only from the harness, the roles and the context.
- Problems with no signal at all are not a design target. Form C in §9 (human judgment only) can run, but its value guarantee is the weakest.
- No UI in the first version. The human–machine entry point is a long-running session.
- The first version does not aim for more than 1,000 agents.

## 2. Principles

Ten principles. Each states its source (why we believe it), the decisions it derives (the D numbers in §15), and what you would see if it were wrong. Principles are not dogma: the last column is the evidence that would overturn each one.

| # | Principle | Source | Decisions derived | What you would see if it is wrong |
| --- | --- | --- | --- | --- |
| P1 | Attempts run in parallel, not materials. The click happens inside a single context; slicing by source or subtask splits information that needs to be joined across two minds | Practical experience: one agent reads email, one reads Slack, and after merging, the information is all lost; kanban-style dispatchers work poorly on research problems | D1 a single task form; D2 no up-front decomposition; D3 the join is done by a single agent | A control group sliced by material does no worse than the full-material group |
| P2 | Compress only after the value is known. A lead's value shows up late; summarizing early is a blind projection | Practical experience: leads get lost in handoffs and can't be recovered | D4 traces kept in full; D5 fork instead of handoff; D6 summaries only as an index; D7 search is pulled by the receiver; D8 surprises recorded separately | In the handoff experiment, there is no gap between the summary group and the fork group |
| P3 | Verifiers are the trust layer; eval is the scarce resource | DeepMind's 100-agent experiment: one agent found an exploit, and within minutes other agents were reusing it; hallucination amplifies with the number of agents | D9 a state machine per claim type; D10 eval definition unreadable to workers; D11 Critic audits; D12 only the highest-trust signal can move budget | A control group that doesn't isolate eval shows no spread of hacking |
| P4 | The agent that judges what matters and the mechanism that controls who can see what are two separate things | Reasoning: a strong agent managing the blackboard becomes a new lossy compressor and a single source of bias | D13 layer 0 is pure code; D14 the Librarian is an editor, not a gatekeeper; D15 the Planner only writes decisions; D16 the Scout is independent | A control group in which a single strong agent manages the blackboard does less wasted work |
| P5 | Reading the blackboard is triggered by events and state, not by time | Reasoning: timed polling either pollutes the context or misses key information, and there is no correct frequency | D17 three channels; D18 cursor diff truncation; D19 read more when stuck | A fixed-frequency control group is no worse on wasted work or on interruption cost |
| P6 | Humans are the densest information source; alignment comes before launch | Practical experience: compressing a two-hour discussion into a one-page problem.md is the single biggest handoff loss in the whole system | D20 charter negotiation and signing; D21 full discussion text stored in the repo and searchable; D22 first-wave workers forked from the PI's context; D23 human insights written as claims | First-wave workers that read the spec do no worse than forked ones (H10) |
| P7 | Diversity has to be managed explicitly | Literature: part of the diminishing returns of swarm scale comes from duplication; practice: agents converge on the same idea | D24 approach portfolio and leases; D25 dead\_end is a first-class citizen; D26 mandatory search before submitting an eval | Candidate diversity doesn't drop, and dead\_ends are not re-walked |
| P8 | Layers split by abstraction, not by material | Reasoning: layers that cut across material leave nobody owning the cross-approach click | D16 Scout; D27 one agent per layer holds the full picture of that layer | The reuse rate of the cross-approach insights the Scout produces is zero (H9) |
| P9 | Eval results must have a receiver | A hole found in the v1 review: when an async eval comes back, the submitter has already released the task, and the loop is broken | D28 result\_ready is bound to the submitter's checkpoint and generates a follow-up task | In a control group without the binding, eval results are still digested promptly |
| P10 | Models and tokens are not constraints. Add no mechanism to save tokens, use no weak model to save money, set no budget on tokens. There are only four things to save: eval slots, wall-clock time, each agent's context window, and human attention. Conversely, anything that costs only model calls and no eval can be done redundantly | Round 29: tokens are unlimited; don't let models and tokens hurt development efficiency or add engineering complexity | D47 model tiers and H6 withdrawn; the Budget meter does not count tokens; the Librarian does not batch; the scorecard's cost counts only eval; D78 trades tokens for eval | The API's concurrency or rate limit becomes the bottleneck (even then, what needs saving is concurrency, not tokens) |

## 3. Hypothesis register

These are our guesses. Each states why we believe it, which experiment validates it (§11), and where we fall back if it fails (the fallback of the corresponding decision in §15). The purpose of the PoC is to turn this table, row by row, into validated or refuted.

| # | Hypothesis | Why we believe it | Validation experiment | Fallback on failure |
| --- | --- | --- | --- | --- |
| H1 | Under the same eval budget, a herd that shares insights and dead\_ends during the run delivers a final combination that consistently beats best-of-N, which runs in isolation and is merged by the same Integrator, but whose workers cannot see baseline advances | The experience of human research groups; sharing dead\_ends directly saves duplicated work | S2 (Planner off) vs B1 @ 5 to 10 workers, with the number of runs computed from the D72 pilot and a preregistered effect size; S1 as an intermediate arm to locate the value of what is communicated | The system degrades to best-of-N + dedup + tiered eval, which still saves eval compared with bare best-of-N |
| H2 | Handoff fidelity with fork is far higher than with summaries | LLM context can be copied; human brains can't | Handoff fidelity experiment | Fall back to trace pointers + ask back; give up fork |
| H3 | Polling at tool call boundaries is enough to replace push-style hard interrupts | Tasks last 20 minutes to 2 hours, the event rate is low, and the interval between tool calls is far shorter than a task. This assumes the harness has tool hooks; otherwise the granularity is only herd tool calls (D60) | Interrupt latency and wasted-work diagnostics | Add NATS push |
| H4 | A single git repo holds up to a few hundred agents | 300 commits/hour is far below git's limit | Merge queue backlog metric | Split into repos by approach, or switch the source of truth to a database |
| H5 | Local proxies and T1 can be calibrated against the top-level eval to a usable correlation | Work like FunSearch and AlphaEvolve relies on cheap proxies to drive search | Correlation records | Fall back to only T0 plus the top-level eval; reduce the number of workers, and have workers do more reasoning and fewer experiments |
| H7 | A single Planner is acceptable up to a few hundred agents | Decisions are low-frequency, and it reads digests, not traces | Curve of the Planner's single-run duration against the number of approaches | Shard by approach |
| H8 | Prediction records can calibrate agents' judgment, and are worth weighting by | The calibration literature | Correlation between prediction error and the success rate of later candidates | Drop the weighting; only record |
| H9 | The Scout can produce cross-approach insights with reuse value | The walkthrough in Appendix B; group meetings in human labs | Reuse rate of cross\_approach insights | Remove the Scout; cross-approach connections rely on humans |
| H10 | First-wave workers forked from the PI agent's context do better than ones that only read the spec | The conversation record carries over implicit taste and boundaries | A control with half of the first wave forked and half reading the spec | All read the spec; the PI's conversation serves only as searchable material |
| H11 | Cross-machine fork, including sandbox state, is feasible in engineering terms at an acceptable cost | The conversation is text; the sandbox can be snapshotted | Week 0 spike: migration time and snapshot size | Replay the conversation + command log, accepting reduced fidelity |
| H12 | The boundaries of the six ports are right: swapping the Executor and Harness adapters does not change the S2-vs-B1 conclusion | In the design, L2 depends only on L1; herd tool hooks live on the server side, and harness-specific capabilities are explicit fields on the port | Swap in the k8s Executor in week 4 and a second Harness in week 5; rerun S2 vs B1 @ 5 | If the conclusion changes, there is a leak; promote the leaking capability to an explicit field on the port; don't fall back to glue |
| H13 | On composable problems, allocating by "marginal gain over the current best combination" beats allocating by each approach's own best score | When approaches are complementary, an approach with a high score of its own may overlap with the combination | From week 5, A/B the two kinds of improvement\_rate | Keep each approach's own score |

## 4. System overview

Five layers. The existence of each layer traces back to a principle or a constraint. The judgment layer exists because of P4 (separating judgment from visibility). The shared-state layer exists because of C4 and P2 (state outside the agents, lossless). The mechanism layer exists because of the other half of P4 (visibility controlled by pure code) and C2 (filtering is required). The execution layer is the workers. The human–machine layer is P6. Workers deal only with the repo and the mechanism layer, and never send messages directly to other agents. The judgment layer only reads digests and writes decisions; it is not on the workers' read path.

&#91;embedded content: System architecture · five layers; workers interact only through the mechanism layer and the repo\]

How to read it: the downward arrows are the three channels through which information flows to workers (diff, search, hard interrupt). The upward arrows are workers' contributions entering the repo through the merge queue. Eval results are written back to the repo and then turned into events by hooks. The Router in the diagram is, in the first version, a check inside the harness, not a standalone service (D29).

| Role | Count | Model | Trigger | Reads | Writes | Source |
| --- | --- | --- | --- | --- | --- | --- |
| PI agent | 1, persistent across problems | Strongest | Human messages; escalation requests from the Planner | charter, digest, decisions, any trace | charter, claims with author=human, first-wave forks | P6 |
| Planner | 1 | Strongest | When the top-level eval results for each quota window are all in | all digests, eval statistics | decisions only | P4, D15 |
| Scout | 1, from week 5 | Strongest | Every K hours | all digests, surprises, high-score diffs | cross-approach insights; forks workers from its own context | P8, H9 |
| Critic | 1 to 3 | Strongest | A candidate enters the top-k or its score jumps; rate-limited | candidates, eval details, eval code, holdout | audits, split into public conclusions and non-public reasons | P3 |
| Librarian | 1 in the first version | Strongest | Runs continuously | new claims, high-score diffs, traces | digests, insights, tags | P4 |
| Worker | 50 | As strong as possible | Picks up a task | spec, charter, digest, diff, search | claims, candidates, traces, checkpoints | P1 |
| Indexer, Router check, Merge queue, Eval service, Budget meter, Integrator | 1 each | None | Repo events, queues, tool call boundaries | repo, KV | indexes, interrupts, main, results, metering | P4 layer 0 |

## 4.1 Architecture layers and ports

**D59 \[Derived; the flip side of constraint C8\] The herd is a standalone runtime + orchestration. It connects to the outside world through ports and is not bound to any cluster management system or agent implementation.** Rationale: the effective parts worked out in this document (blackboard, state machine, eval ladder, allocation, communication rules, alignment protocol) are orthogonal to "what the agents are launched with, and how each agent is implemented in detail". If the two are bound together, swapping a harness or a cluster means a rewrite, and the PoC's conclusions can't be carried over either.

```
L3  Problem instance  charter/, spec.md, eval/sources/, approaches/                  one per problem
L2  herd runtime      blackboard conventions, state machine, events, Indexer, task queue, Eval service,
                      allocation, routing rules, stop rules, Budget meter, Integrator,
                      role programs (PI, Planner, Critic, Librarian, Scout)           what we build
L1  Ports             Executor, Harness, SignalSource, Store, Human, Model            interfaces; L2 depends only on this layer
L0  Adapters          k8s / local process / Ray; Claude Code / Agent SDK / OpenHands;
                      git / Redis / S3 / sqlite-vec; CLI / Slack                      replaceable; the cluster repo lives here
```

**The six ports**

| Port | What L2 needs it to do | First-version adapter | Later |
| --- | --- | --- | --- |
| Executor | `start(agent_spec, task, ctx) → handle`; `checkpoint(handle) → blob`; `restore(blob) → handle`; `kill`; heartbeat | Local subprocess | k8s pod (connects to the cluster repo), Ray |
| Harness | build(task, ctx, tool\_endpoint, hooks) → agent\_spec, which is handed to the Executor to start; the process calls tools through the herd's tool service and leaves a claim when it finishes; export\_state(handle) → blob / import\_state(blob) (only with supports\_checkpoint). Capability declarations: supports\_tool\_hooks and `supports_checkpoint` | Claude Code headless | Agent SDK, OpenHands, a hand-written loop |
| SignalSource | The declaration from §9; `submit(candidate)` / `poll` / push callback | In the PoC phase, implemented by the testbed's slow-tier service (a separate document, *Herd Testbed* (`herd-testbed.md`)); the herd side has only an HTTP client | Kusto, flight, humans |
| Store | VersionedStore (git semantics: append, merge sequence numbers, hooks), KV, ObjectStore, VectorIndex | In-memory version (for tests) + real git / Redis / S3 / sqlite-vec | Swapping implementations does not change L2 |
| Human | The message channel between the PI agent and humans | CLI long-running session | Slack, Web |
| Model | `complete(role, messages, tools)`; embed(texts) → vectors (needed by the Indexer, dedup and diff ranking; the in-memory adapter uses bag-of-words or hashing) | A per-role model routing table | Swap models per role, A/B |

**D60 \[Derived P4\] Hooks come in three layers, each placed according to what it cares about.** Layer one: the hooks for herd tools live on the MCP tool server: the prefix check for repo\_write, the search and gate before eval\_request, immediate push, writing surprises to disk. These concern only herd tools, so the server side is enough, and any harness that speaks MCP can serve as a worker without changing a line. Layer two: the interrupt check needs to see all of the harness's tool calls, and Claude Code's built-in Bash, Read and Edit don't go through the MCP server, so this check uses the harness's own tool hook (Claude Code's PreToolUse). For this, the Harness port has an optional capability, `supports_tool_hooks`; a harness without it checks only on herd tool calls, at a coarser granularity. Layer three: the Executor as the backstop. The heartbeat is sent on a timer by the Executor's sidecar and does not depend on any tool call. If a Router hit is not acknowledged within 5 minutes \[Default\], the Executor sends SIGTERM to trigger a checkpoint and restarts the worker with the interrupt content. If the process exits without a claim, it is restarted once with a reminder. The v2 first draft said "hooks are all on the server side". That was wrong: it missed the harness's built-in tools.

**Where the Harness port leaks, stated openly rather than hidden.** Fork fidelity depends on whether the harness can export and import conversation state. On the port, this is an optional capability: a harness with `supports_checkpoint` set to true supports real fork; one with false degrades to "restart from a trace pointer", and the gain from H2 is discounted. The Store port's VersionedStore also leaks a little: L2 depends on the semantics "merge sequence numbers are globally monotonic", and adapters must guarantee it.

**Role programs are not ports.** PI, Planner, Critic, Librarian and Scout are the herd's own code. They call models through the Model port and read and write through the Store port. Swapping models is configuration; changing role logic is changing the herd itself.

**D61 \[Hypothesis H12\] How to validate the abstraction, and restraint.** Whether the ports are right is tested by swapping adapters: with the same problem and the same charter, switch the Executor from a local process to k8s and the Harness from Claude Code to Agent SDK; the S2-vs-B1 conclusion should not change. Three rules of restraint. First, in the first version each port gets only one real adapter plus one in-memory adapter. Second, the second real adapter comes after H1, and the interface is distilled from the differences the second one reveals; don't design for generality while there is only one implementation. Third, the in-memory adapter is not optional: it lets the whole herd run a tiny problem in one process in a few seconds. It is where iteration speed comes from, and it is also the regression test for every change.

**Repo layout**

```
herd/
  core/        L2: blackboard, queue, eval, allocation, rules, stop, budget
  roles/       L2: pi, planner, critic, librarian, scout (prompt + program)
  tools/       L2: MCP tool service; the hooks for herd tools live here (D60 layer one)
  ports/       L1: executor, harness, signal_source, store, human, model
  adapters/    L0: executor/{local,k8s}, harness/{claude_code,sdk},
                   store/{memory,git,redis,s3,sqlite_vec}, human/{cli}, model/{router}
  problems/    L3: one directory per problem
  tests/       end-to-end tests run with the memory adapters
```

The cluster repo is not merged in; it is a dependency of `adapters/executor/k8s`.

## 5. Data model

**D30 \[Derived P2, P4; Hypothesis H4\] One git repository is the single source of truth.** All derived state (the SQLite index, embeddings, current state) can be rebuilt from it. Git was not chosen to save effort. Commit history is an append-only log (P2 requires losslessness). A branch is a fork. A merge conflict is free conflict detection. Blame is credit assignment. CI is the verifier gate (P3). A PR is pre-submission review. Bisect can find which change made the score drop. Harnesses like Claude Code are also far more fluent with git than with any custom API. The scale limit is H4.

**Directory layout**

```
charter/                     §10. If status is not aligned, the herd does not start
  params.yaml                Machine-readable parameters: truncation cutoff, surge k, floor, attention minutes, budget caps
spec.md                      Problem statement + eval interface; generated by the PI agent after the charter is signed (hand-written by a human in the H1 phase)
approaches/<id>.md           Approaches. frontmatter: status, parent. No shares stored here; shares live in allocation/
allocation/<local_id>.json   Current allocation (takes effect in merged seq order, same as events): target_slots and eval_share per approach; written by the allocation rule or a Planner override
claims/<agent>/<id>.md       One contribution, in per-author directories
candidates/<hash>/
  candidate.*                The candidate itself, written by the originator; CI decides write rights by originator
  prediction.json            Written by the originator
  local.json                 The originator's local proxy eval record
  result.json                Only the Eval service can write it
  lineage.json               parents: […], can have multiple parents (combined candidates, D64); written by the Eval service after it checks the parent hashes
digests/<approach>.md        Maintained by the Librarian, hard cap 2,000 tokens [Default]
decisions/<ts>.md            Written by the Planner, can be appealed
events/<agent>/<local_id>.json  State-change events, in per-initiator directories; original files are never modified; the writer does not know the seq
events/_seq/<seq>             Only the merge queue can write it: points to the merged event file; seq is assigned only at merge time (D56)
traces/<agent>/<task>.ptr    Pointer into object storage + summary + surprise flag
checkpoints/<agent>/<ts>.ptr Checkpoint pointer for the conversation + sandbox state
eval/sources/<id>.yaml       Public metadata of the source; the definition itself lives in the private store (D10, D63)
```

**D76 \[Derived P2, P4; from the v3 review\] v2.2 adds three directories.** `map/` is the opportunity map: measurement scripts that break the gap down by cause, and their output on every baseline version (map/gap.md, map/measurements/\<id>/). The Integrator reruns them after every baseline\_advanced. Humans and agents can all read, cite and rerun them. A human hand-writes the first batch of measurement scripts when setting up the problem. They go on the before-the-gate checklist alongside spec.md and the first approaches, and the first approaches are written against map/gap.md. For the two testbed problems, the problem packs ship them. S2 workers can add new scripts to map/measurements/, through repo\_write and CI's schema check. S1 receives the updated map but cannot add scripts. The map is shared information, not a basis for assigning work: the task shape does not change (D1), and the PI and Planner still manage approaches as an approach portfolio (D24). The map tells everyone where the gap is and which part nobody can explain yet, and the PI can open an exploration approach based on it. What is shared is rerunnable measurements, not summaries; this is exactly P2. `baseline/<k>.json`: which candidates each baseline version contains, the paired results, and the gate results (D74). `report/`: the attribution report and merge log generated at the end of a run. Event types are filled out to match: baseline\_advanced, integration\_failed, interaction\_found, map\_updated, audit\_checked, baseline\_reverted, dedup\_hit. Claim status adds quarantined.

**Three things that live in KV, not in git**: leases (D32), the task queue, and eval queue state. They change two orders of magnitude more often than commits.

**D63 \[Constraint: git has no per-path read permissions\] Things that live in the private store, not in the blackboard repo**: eval definitions and data, holdouts, the rationale section of audits, and the Critic's notes. Workers can clone the whole blackboard repo (P1 requires workers to see all the material), so "a directory in the repo that workers cannot read" is impossible in git. Anything a worker should not see cannot go into the blackboard. It can only go into a separate store that worker pods have no credentials for (another repo or an object storage bucket). The blackboard holds only the public metadata of sources and the public conclusions of audits. In the PoC phase, hidden data and scoring code exist only on the machine that runs the testbed's slow-tier service, not on worker machines. Only the Eval service, the Critic and humans have credentials for the private store. The PI agent does not: the first wave of workers is forked from its context (D22), and whatever it has read is inherited, so it can see only the public conclusions of audits.

**D31 \[Derived C4, P2\] Append-only.** Every contribution is a new file, and no agent modifies another agent's files. State changes can be expressed only through events. A worker can write only paths under its own prefix, and CI checks this. These three rules let 50 agents push concurrently with almost no conflicts, and they make history tamper-proof. Directories are split by author so that CI's path check can be enforced (v1 review).

**D32 \[Constraint: heartbeat frequency\] Leases do not go into git.** A lease is derived state and lives in KV (Redis or NATS KV): `{agent, approach, target_claims, depends_on, task_id, heartbeat, files_touched}`. The Executor's sidecar sends a heartbeat every 30 seconds \[Default\], independent of tool calls (D60). On each heartbeat it also runs git diff --name-only against the sandbox and writes files\_touched into the lease (§8 rule 6 and D74 use it). The TTL is 2 hours \[Default\]. On timeout, the Indexer's cleanup job reclaims the lease, after first asking the Executor whether that handle is still alive. Reason: heartbeat frequency and commit frequency differ by two orders of magnitude, and putting leases in git would flood the repo.

**D33 \[Derived P2\] Traces and checkpoints live in object storage; they are archived, never deleted.** The repo holds only pointers. Evidence pointers must always resolve, and CI verifies this at merge time. After 30 days they are compressed into cold storage \[Default\].

**Claim frontmatter**

```yaml
id: c-2026-10-17-0412
type: result | insight | dead_end | metric | question | audit
status: proposed            # Initial status; the current status is computed from events
approach: a-03
author: worker-17 | librarian-1 | critic-1 | scout | eval_service | human
tags: [low_signal, new_approach, cross_approach]   # Optional; written by the Librarian or the author
blamed_constraint: constraints.md#…                # Optional; filled in when a dead_end attributes its cause to a constraint (D52)
depends_on: [c-…]           # May depend on proposed claims, but must note their current status
evidence:                    # At least one, otherwise CI rejects
  - trace: traces/worker-17/t-0391.ptr#L120-L188
  - candidate: candidates/9f3a…/result.json
surprise: true | false       # The author flags "not what I expected" (D8)
reuse_count: 0               # Maintained by the Indexer, not written by the author
```

**D9 \[Derived P3\] Each claim type has its own state machine instead of sharing a single verified.** v1 had only one verified. CI could verify only results, so insights stayed at proposed forever, and P3 forbids building on unverified things. Together this sealed off sharing. It is now split by type:

| claim type | Status | Who can advance it |
| --- | --- | --- |
| result | proposed → verified level by level up the source ladder, and any step can → refuted; verified → quarantined → verified \| refuted (the culprit found by bisecting after a spot check, D72) | Eval service, level by level; the Critic can set refuted; the Integrator can set quarantined, and only a human can lift it; a human can make any change |
| insight | proposed → corroborated (cited by ≥ 2 results verified at the top level, or confirmed by the Critic); can → refuted | Indexer, automatically by reuse; Critic; human |
| dead\_end | proposed → confirmed (confirmed by the Critic, or reproduced by a second independent worker); can → reinstated | Critic; worker reproduction; human |
| metric | proposed → calibrated (correlation with the higher-trust source one level up ≥ threshold, with samples ≥ N) → deprecated | Eval service, automatically; Critic; human |
| question | open → answered | Any role |
| audit | Terminal state | Written by the Critic, split into a public conclusion and a rationale that workers cannot read |
| approach | active → frozen → dead\_end; can → reinstated | Planner (an approach with more than 30% of the budget share must be escalated to a human); human |

**D34 \[Derived P3; v1 review\] Three companion rules for events.** Priority is human > Critic > Eval service / CI / Integrator > Planner > Indexer > Scout / Librarian / worker, and on conflict the higher one overrides. A single-source refuted does not cascade. Only a refuted issued by a human or the Critic, or confirmed by a second source, triggers the §8 hard interrupt, and a `reinstated` event revokes it. Event order uses the merge sequence number, not cross-machine clocks.

**D67 \[Constraint: neither git nor the shell can restrict reads; external review N7\] Arms and runs are isolated by physical separation, not by rules.** D58 says B1 workers read only their own prefix, but workers have a full clone and a shell, so a rule cannot stop them. Therefore: each run gets a `run_id`, with its own repo, KV namespace and eval quota, and the 12 runs cannot see each other. Each B1 worker uses its own separate repo, and at the end the Eval service collects them and does the merge. S1's "sees only candidates and scores" is a filtered mirror repo published by the Indexer; workers clone the mirror, not the original repo.

**D68 \[Derived D31; external review N8\] The worker's clone is read-only, and only the MCP tool service holds push credentials.** Otherwise a worker could use git push to bypass repo\_write's prefix check and the bookkeeping of immediate push. repo\_write commits and pushes on the server side. The worker's local clone is for reading only and fetches periodically. CI's path check is kept as a second line of defense.

**D69 \[External review C8; hash changed in round 30\] Candidate format.** `candidates/<hash>/manifest.yaml`: `{kind: patch | config | artifact, target: <baseline identifier; a target-repo sha is no longer allowed>, parents: […], approach, author, created_at}`, plus a payload: for patch, a unified diff against target; for config, one file; for artifact, a directory. hash = hash(baseline identifier + normalized payload) (D73). The same diff applied to different baselines is a different candidate. Otherwise the Integrator's remeasure would hit dedup\_hit and silently not run, when that remeasure is exactly what should catch an interaction. Dedup uses this hash; similarity search (needs\_confirmation) still considers only the payload. A combined candidate's payload is the full merged patch, not references to its parents.

**D55 \[Derived C4\] Definition of the task queue.** Both v1 and the v2 first draft referred to `claim_task()` without defining the queue. This is where implementation would get stuck on day one. The queue is a set in KV, sorted by priority:

```yaml
task:
  id: t-0391
  approach: a-03
  goal: "Advance the whole problem with method Y" | "Decide the next step based on the result of candidate c-xxx" | "Find a counterexample to dead_end c-yyy" | "Combine candidates c-a and c-b" (D64)
      | "Rebase candidate c-x onto baseline@k and resolve conflicts" (compose, D74; run by a new agent started outside the pool) | "Follow-up after integration_failed" (D74)
  seed_context: checkpoints/worker-17/…ptr | null   # Non-empty = fork-style resume
  priority: 0 | 1 | 2                                # 0 is highest
  for_agent: worker-17 | null                        # Non-empty = offered to this agent first; when it holds no lease, the task enters the public queue
  created_by: indexer | human | scout | planner | integrator
```

Who generates tasks: the Indexer maintains regular tasks according to each approach's target\_slots (the goal uses the approach template), and tops them up when they run low. When result\_ready arrives, the Indexer unconditionally generates a follow-up task, with priority 0 and for\_agent set to the submitter. Humans, the Scout and the Planner can inject tasks. When the queue is empty, workers take exploration-template tasks (new approaches, long-tail approaches, finding a counterexample to a dead\_end). The H1 phase has no checkpoints yet (they are wired in only in week 4), so the seed\_context of a result\_ready follow-up task is empty, and ctx carries the result claim pointer and that worker's own claims. The submitter\_checkpoint field can be empty. Implementation of `claim_task()`: first take tasks whose for\_agent is the caller itself, then go by priority, then by the approach with the largest `target_slots - current_slots`.

**D56 \[Derived P3\] Event schema and merge sequence numbers.** All state is computed by replaying events, so events must have a uniform structure:

```yaml
event:
  seq: 10482            # Assigned by the merge queue when it merges into main; globally monotonic; empty when the writer submits
  local_id: critic-1/00017   # The writer's own sequence number, which is the file name
  ts: 2026-10-18T07:02:11Z
  actor: critic-1
  role: human | critic | eval_service | ci | indexer | planner | scout | librarian | integrator | worker
  type: claim_status | approach_status | result_ready | surge_start | surge_end
      | plateau | escalation | lease_stale | allocation | reinstated
      | baseline_advanced | baseline_reverted | integration_failed | interaction_found
      | map_updated | audit_checked | dedup_hit                      # Added by D74, D76
  target: c-0211 | a-03 | t-0391
  from: proposed        # For status events
  to: refuted
  source: bench_full    # For result events
  reason: "eval parser bug"
  evidence: [claims/critic-1/audit-0211.md]

# Extra fields for result_ready (D28, D57, D73)
  candidate: 9f3a…
  claim_id: c-…            # The result claim generated automatically at eval_request time
  submitter: worker-07
  submitter_checkpoint: checkpoints/worker-07/…ptr
  baseline: 6              # The baseline version it was paired against
  paired: {mean: 1.8, se: 0.6, n_items: 120, n_reps: 1}   # n_reps counts only remeasures the Eval service ran itself; cache hits do not count
  per_item: eval/results/…/per_item.json                   # Pointer; proportion metrics also carry a bootstrap interval
  score: 63.4              # Absolute score, for diagnostics only

# Fields of events emitted by the Integrator (D74)
# baseline_advanced:   baseline: 7, parent_baseline: 6, candidate: <hash>, files_changed: [prompt_builder.py],
#                      paired: {mean, se, n_items, n_reps}, guard_pending: true | false
# baseline_reverted:   from_baseline: 9, new_baseline: 10, removed: [<hash>], reason: audit | guard:<name>   # Records are append-only
# integration_failed:  candidate, baseline, paired, reason: below_1se | gate:<name> | conflict_unresolved
# interaction_found:   candidate, baseline, earlier: {mean, se}, remeasured: {mean, se}, overlaps_with: [<hash>]
# audit_checked:       segment: [j, k], daily: {mean, se}, audit: {mean, se}, threshold, flagged
# map_updated:         baseline, measurements: [<id>], output: map/measurements/<id>/<baseline>/
# dedup_hit:           candidate, original_claim
```

Assigning seq: the merge queue is the only process that writes main of the blackboard repo (the baseline branch of the repo of the system being optimized is written by the Integrator, D74; two repos, two writers). Each time it merges a commit that carries an event, it writes an \`events/\_seq/\<seq>\` that points to that event file, and writes the seq into the merge commit's trailer. This is what the Store port's \`merge(branch) → seq\` means. Priority conflicts are resolved by role (D34). The Indexer replays in seq order, so rebuilds are always deterministic.

## 6. Components

Eleven components, six of them without a model. **D13 \[Derived P4\]**: models appear only where judgment is needed, and the judging agents never sit on the read path. Everything that controls who can see what is pure code, because pure code does not summarize, has no bias, and can be audited.

| Component | Model | Responsibilities | Trigger | On failure | Source |
| --- | --- | --- | --- | --- | --- |
| Indexer | None | Parses frontmatter into SQLite; embeds new claims, digests and trace slices; recomputes current state from events; maintains reuse\_count; provides `diff_since(cursor, working_set)` and `search(query, k)`; cleans up leases past their TTL; generates follow-up tasks for result\_ready (D28) | Repo post-receive hook | Can be fully rebuilt from the repo; during a rebuild, workers read only the spec and digests | D13 |
| Router check | None | A worker uses its cursor to ask the Indexer "has any event hit my lease?"; a hit is handled by the §8 rules. When it checks: every tool call if the harness has a tool hook, otherwise every herd tool call; the Executor backstops per D60. The rules are data: `router/rules.yaml` | Tool call boundary | Skipped; the change is seen through the diff at the next sync point | D29 \[Hypothesis H3\]: no NATS push in the first version |
| Merge queue | None | Workers push to their own branch, and the queue rebases onto main in order. CI has two levels: path, schema and evidence-pointer checks run synchronously in seconds; verifiers run asynchronously, and their results are written back as events | push | A backlog is the H4 signal | D31 |
| Eval service | None | A thin client for the external eval: turns eval\_request into submit, polls status, writes result.json and result\_ready. In the PoC phase it connects to the testbed's slow-tier service (a separate document: *Herd Testbed*, `herd-testbed.md`), whose submit / status / history interface matches the real external eval. Quotas, hidden data and multiple seeds all live on that side, not here | eval\_request; arrival from a push-type source | The queue is persisted in KV | §9 |
| Budget meter | None | Meters eval machine-hours by role, approach and source (tokens are only recorded, with no cap, P10), checks them against charter/constraints.md, and triggers escalation on overrun | Every API call and every eval completion | Metering lags but does not block | v1 review: without it, the budget is just a sentence |
| Librarian | Strongest, same as other roles (P10) | Runs continuously. Each round processes the new commits since the last round, with no batching parameter (P10): tags new claims (including low\_signal); rewrites digests (fixed structure: current best, active hypotheses, corroborated insights, proposed insights, dead ends, open questions; ≤ 2,000 tokens); distills insights from the diffs of high-scoring candidates against their parents. One instance in the first version, not split by approach | Runs whenever there is a new commit | Digests go stale; workers can bypass them and search directly | D14: an editor, not a gatekeeper |
| Critic | Strongest | Explains why a high-scoring candidate scores well; checks it against the holdout; checks its similarity to dead\_ends; checks whether it exploits an eval loophole. Writes audits: a public conclusion + a rationale that workers cannot read. The only agent that can read eval code | A candidate enters the top-k or jumps by more than 2 historical standard deviations \[Default\]; rate-limited to 6 per hour \[Default\] | The backlog is queued by jump size | D11 \[Derived P3\]; the rationale is not public because "why it scores" is a hacking manual |
| Planner | Strongest \[Hypothesis H7\] | Reads all digests and eval statistics, and writes only decisions: each approach's budget share, freezing or divesting, reordering the queue, calling on the Librarian or the Critic. Every decision carries a rationale. Decisions on the escalation list can be written only as a request to the PI agent | After all top-level eval results for each quota window are in; backstop every N hours \[Default: half the top-level eval duration\] | The previous decisions stay in effect; workers claiming tasks on their own does not depend on it | D15 \[Derived P4\]: no global view, no forking, no tuning of communication parameters |
| Scout | Strongest \[Hypothesis H9\] | Every K hours \[Default: 6\], reads all digests, spot checks surprises and high-scoring diffs, and looks specifically for cross-approach connections. Writes cross\_approach insights. For anything worth pursuing, it forks a worker directly from its own context. Does not write decisions and does not touch the budget | Scheduled | Cross-approach connections rely on humans | D16 \[Derived P4, P8\]: the mind that holds the whole picture and the mind that allocates the budget are kept separate |
| PI agent | Strongest | Talks only to the human. Negotiates the charter; after it is signed, generates spec.md and approaches/ and forks the first wave of workers. Acts as the console at runtime. Persists across problems | Messages from the human; escalation requests from the Planner | The human edits the charter directly | P6, §10 |
| Integrator | None | The only process that can write the baseline branch of the repo of the system being optimized. Verified candidates go first-in, first-out: rebase; if there is a conflict, issue a compose task (run by a new agent outside the pool); if the baseline has moved, do one paired remeasure; merge according to the acceptance rule and emit baseline\_advanced and related events. Every 3 merges, run the expensive gates as a batch; if a problem is found, bisect and write a new baseline that excludes the culprit. Every 2 merges trigger a spot check (two new measurements). After baseline\_advanced, rerun the measurement scripts in map/. At the end of the run, execute the closeout order and attribution. Same configuration in all four arms | A result claim reaches verified; the budget runs out | The queue is in KV; recovers from baseline/\<k>.json | D74 \[Derived P1, D64\]; round 30 |

## 7. Worker harness

**D35 \[Derived C4\] Workers are stateless.** All of a worker's state is either in the repo or in a checkpoint in object storage. If a worker dies, it starts over from the checkpoint or the task queue. Principles are enforced by hooks, not by reminders in the prompt: prompt-driven behavior drifts over long tasks, while hooks run every time and can be audited from logs. The prompt is only responsible for making the goal and the taste clear.

**D1 \[Derived P1\] There is only one task shape**: `{approach, goal, seed_context?}`. A goal is "advance the whole problem with method Y" or "decide the next step based on the eval result of candidate c-xxx", never "read X" or "look at directory Y". Every worker gets all the material. A non-empty seed\_context means: resume the conversation from that checkpoint, then append the goal. Forks, eval-result follow-ups and the PI's first wave all take this path.

```
loop:
  task = claim_task()
  ctx = spec.md + charter summary + digests/<approach> + diff_since(cursor, working_set)
  acquire_lease(approach, goal, target_claims)   # KV, not in git (D32)
    # target_claims = the claim ids parsed from goal; after that, every time repo_write writes a claim,
    #   the MCP server adds the new claim to lease.target_claims and merges its depends_on into lease.depends_on
  run_agent(ctx, tools, hooks)
    # Each claim / surprise / eval_request is committed + pushed as soon as it is produced (D36)
  release_lease(); checkpoint(); push(branch)
```

**Tools**

| Tool | Permissions | Source |
| --- | --- | --- |
| repo\_read | The whole repo, including other agents' claims, decisions, and the full text of the charter discussion | P1: see all the material |
| repo\_write | Add only, only under its own prefix; CI checks again | D31 |
| search(query, k) | Semantic search over claims, digests, trace slices and the charter discussion | D7: pull instead of push |
| eval\_request(candidate, parents, predictions{source: score}, rationale, confirm\_token?, baseline?) → {request\_id, claim\_id, baseline, status: queued \| needs\_confirmation{similar: \[…\]} \| dedup\_hit \| gate\_failed \| invalid\_request, degraded\_sources: \[…\]}. Two phases: if the first call's search finds similar candidates or dead\_ends, it returns needs\_confirmation and a confirm\_token, and the request is queued only when the worker calls again with the token (D26). baseline defaults to the current baseline. The Integrator's remeasures go through the internal interface remeasure(candidate, baseline), which skips needs\_confirmation and also skips dedup (D74). On enqueue, the service automatically creates a claim with type=result (its evidence points to the candidate) and returns claim\_id; the result at each later level is a status event on this claim. The service runs the levels automatically as declared, and the worker does not choose the level. parents is used to check parent hashes for lineage.json; a combined candidate has several (D57, D64) | Can only queue; cannot run evals directly; cannot read eval definitions | D10 |
| register\_exposure(candidate, tag, window) | Used with push-type sources; signals are attributed to the candidate only after registration | §9 |
| fork(reason, goal) | Saves a checkpoint and starts a sibling worker | D5 \[Hypothesis H2, H11\] |
| ask\_checkpoint(agent, ts, question) | Restores a read-only copy of a checkpoint to answer a question, including questions to the PI agent's alignment conversation | D5 |
| surprise(note, pointer) | Writes a claim with type=question and surprise=true; no conclusion required | D8 |
| sandbox | This task's isolated compute environment; local proxy evals run here | C1 |

**Hooks**

| When | Action | Source |
| --- | --- | --- |
| Before every tool call | Check whether any event hits this lease (§8 rules). This needs the harness's tool hook; a harness without one checks only on herd tool calls, and the Executor backstops per D60 | D17, D32 |
| During long sandbox calls | The Executor sidecar sends heartbeats on a timer (every 30 seconds \[Default\]), independent of tool calls, so long sandbox calls are not mistaken for stale | D32 |
| When each claim / surprise / eval\_request is produced | Commit + push immediately | D36 \[Derived P5\]: sharing latency must not equal task duration |
| Before eval\_request | Automatically search for similar candidates and confirmed dead\_ends. On a hit, return needs\_confirmation; the request is queued only when the worker calls again with the confirm\_token | D26 \[Derived P7\] |
| At eval\_request | The request must carry the predicted score for each level (predictions) and a rationale, and is rejected if either is missing. The gate (D54) runs here first. The local proxy result is written to local.json | D37 \[Hypothesis H8\] |
| When an eval result arrives | The Indexer unconditionally generates a follow-up task, with for\_agent pointing to the submitter. If the submitter holds a lease, the content of the rule 1 hard interrupt is "go claim it"; otherwise the task goes to the front of the public queue (D55) | D28 \[Derived P9\] |
| N consecutive failures \[Default: 3\] | Force a refresh of the digest and one search. Failure = one local proxy or cheap-level result that does not beat that worker's current best, or one eval\_request rejected by the gate / dedup | D19 \[Derived P5\] |
| On finding an anomaly | Produce an executable minimal reproduction (a failing test, a concrete example, a notebook) as evidence | D8 \[Derived P2\]: artifacts cannot be compressed; prose gets lost |
| Context near its limit | Not a fork: a fork copies the whole conversation, so the child starts at the limit. Instead, write a structured contribution claim (including unfinished leads and its own surprises), checkpoint, and then start a continuation task with a compressed context (spec + digest + its own claims + trace pointers). The continuation can ask\_checkpoint its own past. Under P2 this is lossy, so checkpoint as early as possible. v2.2 makes this path concrete (from the v3 review). The trigger is 70% of the window \[Default\]. The Harness adapter turns off auto-compaction and computes usage from the usage field in the session record. The new session's start pack = the original start pack + its own claims from this task + the interrupt content (if any) + the working-directory diff + the last 20 tool calls \[Default\]. If the process is killed, the sidecar writes a stub claim on its behalf that points to the trace. This path does not depend on checkpoints, can be used before the gate, and is the same for all arms; fork is still for after the gate (D5) | C4 |
| Before finishing | Must write ≥ 1 claim with evidence. A dead\_end that says "nothing found this time" is allowed; an empty result is not. Upload the trace; save a checkpoint | D4, D25 |

**D5 \[Derived P2; Hypothesis H2, H11\] Fork instead of handoff.** Checkpoint = the full conversation + sandbox state (a working-directory snapshot or a replayable command log) + tool state pointers. A checkpoint that stores the conversation but not the sandbox is only half a checkpoint: the forked worker inherits the ideas but loses the working directory. Fork = use that record to start a new session on any machine, restore the sandbox, and append the goal. With a prompt cache, the conversation part comes as incremental tokens; the cost of restoring the sandbox is a number the week 0 spike has to measure. Ask back = a read-only copy; it does not restore the sandbox and writes nothing. Fork is used only to branch at decision points, not to decompress context. The Executor records each checkpoint's context usage. Fork prefers checkpoints with usage below 60% \[Default\]; checkpoints above that are compressed first and then forked.

## 8. Communication

**D38 \[Derived P1, P4\] Agents do not send messages to each other.** All communication is writes to and reads from the repo. There are two reasons. Point-to-point messages cannot be audited or replayed, and P2P among 10,000 agents is a disaster. More importantly, only what is written into the repo gets indexed, searched and seen by the Librarian; messages do not.

**D17 \[Derived P5\] Reading is split into three channels, triggered by events and agent state, not by the clock.**

| Channel | Trigger | Content | Cost | Latency |
| --- | --- | --- | --- | --- |
| Hard interrupt | The Router check hits a lease | The current task is invalidated, or this agent's eval result has arrived | One KV read (after a hit, the Indexer writes interrupt:\<agent>, and the Router check reads only this key), on every tool hook or herd tool call | At the next check, seconds to minutes \[Hypothesis H3\]; Executor backstop at 5 minutes |
| Soft injection | Reaching a sync point: before claiming a task, before eval\_request, after a failure, at task end, after N consecutive failures | `diff_since(cursor, working_set)`, sorted by relevance, truncated at 500 tokens \[Default\] | 500 tokens | Minutes to hours |
| On-demand search | Called by the agent itself when it forms a hypothesis or hits an anomaly; partly forced by hooks | search returns the top-k claims and trace slices | Per call | Immediate |

**Routing rules (`router/rules.yaml`; change the rules, not the code)**

For each active lease `{agent, approach, target_claims, depends_on}`:

1. `result_ready` for a candidate this agent submitted → hard interrupt, with the content "go claim the follow-up task whose for\_agent points to you" (D28, D55).
2. Any claim in `target_claims` advances in status or is refuted → hard interrupt.
3. `approach` is frozen, dead\_end, or its budget drops to zero → hard interrupt with the decision attached.
4. Any claim in `depends_on` is refuted, and the refuted was issued by a human or the Critic or confirmed by a second source → hard interrupt. A single-source refuted only goes into the diff (D34).
5. A claim with author=human tags this approach → hard interrupt. Human intervention has the highest priority (P6).
6. A candidate is merged into the baseline (baseline\_advanced), and this lease's files\_touched overlaps the merged files → hard interrupt, with the content "rebase onto the new baseline". No overlap → soft interrupt; wait for the diff (D74).
7. Anything else → no push; wait for the diff.

**D18 \[Derived C2\] Relevance ranking and truncation of the diff.** `working_set` = the approach in the lease + target\_claims + recently searched claim ids. Ranking: verified in the same approach > dead\_end in the same approach > cross-approach insight > everything else. After truncation, one line is appended: "N more; use search to find them". The truncation cutoff is a charter parameter. The PI agent changes it based on the human's input; it is not a Planner decision (D15).

**D19 \[Derived P5\] Read more when stuck.** The more consecutive failures, the denser the sync points. After N failures, every tool call does a diff check until there is progress. Reason: consecutive failures are the strongest signal that "someone else may have done this already".

**D16 Cross-approach clicks are the Scout's dedicated job (§6)**, because no worker holds the details of two approaches at once, so they cannot be expected to emerge on their own. For leads worth pursuing, the Scout forks directly from its own context instead of writing a task description and handing it to someone else (D5).

**D23 \[Derived P6\] Human insights are written as claims.** The PI agent writes the human's words as a claim with author=human, with evidence pointing to the conversation checkpoint. Rule 5 makes it immediately visible to the approaches it names. Workers can use ask\_checkpoint to ask back the PI agent about the details of that conversation.

## 9. Eval

Eval is a scarce resource (C1). So the system's goal is to make every expensive eval land on the candidate that carries the most information, and the agent's job shifts from "generating candidates" to "predicting which candidate is worth an eval". Eval is also a plugin (C6). So this section first defines the interface, then the general mechanisms derived from the interface, and finally the differences among the three forms.

**D39 \[Constraint C6\] Eval is a set of signal sources. Each source declares its own properties, and everything is derived from those declarations.**

```yaml
# eval/sources/<id>.yaml   7 required fields, the rest optional; most external evals fill in only the required ones
id: bench_full
mode: pull | push            # Required. pull: submitted per candidate; push: arrives on its own schedule
latency: 6h                  # Required. From submission to result
budget: {per_day: 40, allocated_by: planner | human}   # Required
cost_machine_hours: 48       # Required. Machine hours for one run; for an external system, fill in its billing converted to machine hours; ladder ordering and the Budget meter both use it (D42)
trust: 0.9                   # Required. 0 to 1; sets the ladder position and whether it can move permanent budget
readable_by: [critic]        # Required. Decides where the definition lives: on the blackboard if it includes worker, otherwise in the private store (D10, D63)
kind: tool | dataset | query | flight | human          # Optional, default tool
attribution: candidate | exposure                      # Optional, default candidate
concurrency: 9               # Optional, default unlimited (the external system does its own queueing)
noise: {n_for_ci: 3}         # Optional, default 1; result always carries n (C3)
drift: fixed | drifting      # Optional, default fixed
escalate_on_request: false   # Optional; true for flight
compare_against: concurrent_control | none             # Optional; for observational signals
```

| source | Typical declaration | What it changes |
| --- | --- | --- |
| External eval tool | pull, candidate, hours, high trust, fixed | The base design |
| Local dataset | pull, candidate, minutes, medium trust, fixed | Automatically cut into subsets that form a cheaper tier; workers may overfit to it, so readable\_by is tightened |
| Kusto query | push, exposure, latency equals the data-pipeline lag, medium-high trust, drifting | Candidates register exposure first; the Critic checks for confounding; compare only against a concurrent control |
| Flight | push, exposure, days, a few concurrent, highest trust, drifting | Pushing to a flight goes through escalation; result carries significance |
| Human | pull, candidate, hours to days, high trust but noisy | Queued through the PI agent; the human's judgments become calibration samples for the metric |

**General mechanisms derived from the declarations**

- **Ladder \[Derived C1\]**: sources are ordered by cost\_machine\_hours and latency: "pass the cheap one before you get the expensive one". T0 to T3 are only position labels for the sources in a given problem, not hard-coded tiers.
- **Calibration \[Derived P3\]**: every non-top source keeps a correlation record against the higher-trust source one tier up. Below the threshold \[Default 0.3, 20 samples\] it is demoted automatically, and workers are told on submission that "this source is currently untrusted". This is the general form of the metric claim.
- **D12 \[Derived P3\] Only the highest-trust tier can change the Planner's budget allocation**; proxy scores can only set the ordering within the queue. This is a hard anti-Goodhart rule, and the Planner cannot override it. The cost is slow budget adjustment when the topline is sparse. This conservatism is a deliberate choice.
- **D40 \[Constraint C1; Hypothesis H5\] Quota and local proxies**: the top-level eval runs a few dozen times a day. For 50 workers, that is less than once per worker per day. So the main driver of the worker loop is T0 plus local proxies that the worker builds in its own sandbox. Local proxies are a first-class mechanism: results and configuration are written to local.json, the Eval service uses it to calibrate against the tier above, and proxies that correlate well are promoted to a formal tier. Quota: T1 ≤ 20 per worker per day \[Default\]; the Planner divides the top-level eval slots among approaches. The first-version queue is just quota plus FIFO; ordering waits until there is enough calibration data (D41, Deferred).
- **D37 \[Hypothesis H8\] Prediction records**: every submission carries predicted scores for each tier (predictions) and a rationale. When each tier's result comes back, that tier's error is recorded. Uses: training a proxy scorer (Deferred); calibrating agent judgment, where the weighting applies only to the information share and not to exploitation, since otherwise it rewards conservative candidates; and getting agents to submit fewer low-confidence things.
- **D28 \[Derived P9\] Results have a recipient**: result\_ready is bound to the submitter and to the submitter's checkpoint at submission time. The Indexer automatically generates a follow-up task at the front of the queue.
  - **D57 \[Derived C1, P3\] Closing the eval\_request loop**: see the tool table in §7 for the signature and return value. On enqueue, a claim with type=result is generated automatically and its claim\_id is returned. Each later tier's result is a status event on this claim. result\_ready carries candidate, claim\_id, submitter, submitter\_checkpoint, baseline, paired{mean, se, n\_items, n\_reps} and a per\_item pointer; absolute scores are diagnostic only (D56, D73). Two-phase confirmation uses confirm\_token; parents validates lineage; allocation state lives only in allocation/; parameters are in charter/params.yaml.

**Anti-hacking \[Derived P3, C3\]**

- D10: eval definitions, data, the holdout and the audit rationale area live in a private store that workers have no credentials for (D63), not in the blackboard repo. Read isolation by repo path cannot be done on git. Workers have only eval\_request and register\_exposure.
- D11: the Critic is the only role that can read eval code; workers cannot read the audit rationale area.
- The holdout is maintained by humans and no agent has ever seen it. It periodically rechecks the top-k; a difference from the upper-tier score beyond the threshold means automatic refuted plus an alert.
- A score jump of more than 2 historical standard deviations \[Default\] is an alert, not a celebration, and it automatically triggers the Critic.
- Refuted candidates and all their descendants are flagged.
- Egress allowlist (D75): the worker sandbox can reach only the model API, the herd tool service, declared context sources and package mirrors, the same in every arm. Otherwise workers could go and fetch the answers to public benchmarks.

**D66 \[Constraint C3; external review N1\] Two holdouts.** If in-run rechecks (the Critic checking the top-k, jump alerts) and the final verdict use the same holdout, the sharing arm touches the holdout more often, and the verdict is biased toward it. So there are two. `holdout_run` is for the Critic during the run. It has a usage cap \[Default 10 per arm per run\], the same for every arm. `holdout_verdict` is sealed: no role can request it at any point during the run. It evaluates each arm's final deliverable only once, after the run ends, with n = params.verdict\_n \[Default 5\], fixed and not adaptive. Both are cut on the first day the problem is set up. They are data, not code, so they exist from week 1.

**D73 \[Constraint C3; from the v3 review\] Paired measurement.** Each eval submission consists of the candidate and the baseline version it is based on. Both run on the same batch of data in the same time window. The result is the per-item difference: mean, standard error, number of items, and number of remeasures n. Pairing removes the effects of item difficulty and time window, which are usually the two largest parts of the noise. n counts only remeasures that the Eval service runs itself; cache hits do not count. For ratio metrics (such as hit rate), sum the numerators and the denominators separately before taking the difference, and use bootstrap for the interval. Candidate hash = baseline identifier + normalized payload (amends D69): the same diff applied to different baselines is a different candidate. The testbed's slow tier returns per-seed scores, so the paired difference between any two candidates can be computed after the fact without spending extra top-level eval. This requires the eval to be deterministic for a given seed (problem A's simulator is; problem B contains model calls, so it still has to run in the same batch and the same time window). Deterministic cheap tiers have no standard error and promote on "gain > 0".

**D74 \[Derived P1, D3, D64; from the v3 review\] Merge flow: the baseline advances step by step, with one remeasure for every merge.** D64 says the deliverable is a composition; this entry makes concrete how the composition happens. Judgment is still done by agents, and bookkeeping by code (P4).

- The baseline is versioned: baseline@0 is the original system, and each merged candidate advances it by one version. Only the Integrator, a piece of pure code, can write the baseline branch. Each version is recorded in baseline/\<k>.json: which candidates it contains, the paired results and the gate results.
- Verified candidates go first-in, first-out by verification time. Ordering by predicted gain would reward boasting.
- Before merging, rebase onto the current baseline. Textual conflict → issue a compose task (D3, D64). The executor is a fresh agent spawned outside the worker pool. It carries no arm's context, and its output goes back only to the Integrator. This is the same in every arm (in B1 the task cannot be handed to B1's workers, or isolation breaks; S2's workers can additionally take D64's "compose candidates" tasks, which counts as sharing). The combined result is requeued as a new candidate. No conflict, but the baseline has moved → do one paired remeasure against the current baseline; if the baseline has not moved, reuse the verification measurement and spend nothing extra. Remeasures go through the internal interface remeasure(candidate, baseline), which bypasses both needs\_confirmation and deduplication.
- Acceptance rule \[Default\]: merge if the paired gain exceeds 1 standard error (the standard error of a single paired measurement: the remeasure's if the baseline moved, the verification measurement's if it did not) and the candidate passes every gate. Otherwise record integration\_failed and give the author a follow-up task. Interaction: record an interaction\_found only when the earlier gain minus the remeasured gain exceeds 2σ√2. (A rule written as "less than 70% of the earlier gain" raises false alarms more than half the time, because the earlier measurement had passed a threshold and is biased high to begin with.) The Integrator only emits the event; an insight with tags=\[interaction\] is written only after the Critic rechecks it (D64). The only basis for this threshold is simulation. When eval is scarce, its net gain is about 2× that of the strict version and 2.9× that of "remeasure three times and look at the 95% lower bound" (1.2× to 1.6× under more realistic settings). The cost is that one-tenth to one-fifth of merges are slightly harmful patches (a total of −0.3 to −1.4 per run). The acceptance rule is chosen at preregistration and is not switched mid-run during H1. If the strict version is to be used (first screen at 0.5σ, then 3 fresh remeasures, 90% lower bound > 0), the pilot uses the strict version too. When estimating the noise σ, use the upper bound from A/A remeasures, not the point estimate; otherwise this rule degenerates into "merge if greater than 0".
- Every merge emits a baseline\_advanced event carrying files\_changed. Workers whose lease's files\_touched overlaps it receive a hard interrupt to rebase; everyone else receives a soft interrupt (§8 rule 6). files\_touched is written into the lease by the Executor's sidecar, which runs git diff --name-only on the sandbox at every heartbeat (D32). B1's workers do not receive this event and keep working on baseline@0, while the Integrator merges their verified candidates in the background; S1 and S2 do receive it (D58).
- Expensive gates are checked in a batch once every 3 merges. Baselines in between are marked guard\_pending, and work can continue on top of them. If a problem is found, bisect to find the culprit and write a new baseline version that excludes it (records are append-only; old ones are not changed). The deliverable is never a guard\_pending baseline.
- The closeout order at the end of a run is the same in every arm: stop accepting; run the outstanding gate checks on every guard\_pending baseline (a fixed off-budget quota; problems found are bisected by the normal flow); final spot check; freeze the deliverable = the last non-guard\_pending baseline; after all runs have ended, evaluate holdout\_verdict for all of them together. Attribution is done after holdout\_verdict has been evaluated, using only daily data and a fixed quota, and it does not affect the verdict. It is "remove one approach at a time", and it writes report/attribution.md: how much each approach contributed (with intervals), the difference between the total gain and the sum of the parts (that is, the interaction), and the dead ends. This report is itself a deliverable. There is no budget for ablation during the run; bad merges are caught by D72's spot checks, not by ablation.
- The merger is the same in every arm (round 30). The Integrator runs during the run in B0, B1, S1 and S2, with the same acceptance rule, gates, guard batches and spot-check configuration; remeasures spend each arm's own top-level eval slots. "Sharing" refers only to whether workers can receive baseline\_advanced and rebase. Each arm's deliverable = the last non-guard\_pending baseline of that arm's Integrator; the score of the single best candidate is recorded separately as the B1-single diagnostic column. There is no longer a "greedy merge with git apply after the end" step: with that step, you could not tell how much of S2's extra score came from sharing and how much from a better merger. The Integrator writes the baseline branch of the repo of the system being optimized; the blackboard repo's main is still written only by the merge queue (D56). These are two different repos.

**D75 \[Constraint; from the v3 review\] Egress allowlist.** The worker sandbox can reach only the model API, the herd tool service, declared context sources and package mirrors. Everything else is blocked, the same in every arm. The allowlist of context sources is in charter/egress.md; anything not listed there is blocked. Otherwise workers could go and fetch the answers to public benchmarks.

**D78 \[Derived P10; Deferred in round 30\] Trading tokens for eval: Deferred until after H1, as a separate A/B.** The original idea was that before a candidate enters the slow tier, 2 Critics each review it once, and a vetoed candidate takes no eval slot. The review pointed out three problems. Writing veto reasons as public claims would leak "why it scores" to workers (D11 keeps the rationale private precisely because it is a hacking manual). The Critic's LLM audit comes after the gate, so nobody performs it during H1. Turning it on only in S2 adds one more confound. So it is not done during H1. It will be tried later as a separate A/B, done at that point by a pre-screener who does not read eval code, the same in every arm. Two things are kept: fast-tier remeasures are unlimited; a compose task can be dispatched to several agents at once, but their versions are picked using only the fast tier, and only the one chosen version enters the slow tier.

**Three forms \[Constraint C6\]**

| Form | Top-level signal | Lower-tier signals | Difference from the base design |
| --- | --- | --- | --- |
| A Fixed eval | A given tool or dataset | T0, T1, local proxies | None. When the eval is noisy, result carries n and an interval |
| B Has a topline, but not fixed | Weekly large evaluations or online metrics; sparse | Proxies that the herd designs and calibrates itself | Changing the ruler is part of the work: metric claims; the queue gains an "improve measurement" task type; the Planner gets an extra measurement budget \[Default 15%\]; the Critic additionally checks whether proxy gains transfer to the topline; charter/progress.md must state the topline and the proxy boundary |
| C No metric | Human judgment, through the PI agent, with a budget | Consensus of the Critic plus blind rankings by several workers | The human is T2; candidates are documents, not code; once judgments accumulate, train a judge and move to B; the weakest guarantee of value; not recommended as a starting point |

A special case of form A: the thing being optimized is the harness itself. Eval = running a held-out task set with the candidate harness, at least n times per candidate. The workers themselves run on a frozen version, and candidates run only in the eval sandbox; otherwise the herd would be changing the floor under its own feet.

## 9.1 Dynamic allocation: breadth and depth of exploration

This section answers two questions. How do we make sure exploration is broad enough and also deep enough? When an agent finds a good approach, how do we dynamically give it more agents and machines? v1 hid this inside one sentence, "the Planner allocates budget", which was not concrete enough. The mechanical definitions follow.

**D49 \[Derived C1, C4\] The unit of allocation is the slot, not the machine.** There are two kinds of slot: the worker slot (one worker process plus a sandbox) and the eval slot (the daily quota of an external eval). Each approach has `target_slots[a]` and `eval_share[a]`. "Adding agents to an approach" mechanically means raising its target\_slots and its eval queue weight. Workers are not killed: after finishing the current task, they take tasks according to the new weights (lease caps are computed per approach). There is a hard interrupt only when an approach is frozen. "Adding machines to an approach" means that approach's workers get more sandbox concurrency (more local proxy runs) and a higher eval queue priority. Allocation state has exactly one home: allocation/\<local\_id>.json. It takes effect in merge `seq` order and is written by the allocation rule or by a Planner override; approach frontmatter does not store shares (D31). In the H1 control phase, the number of slots per approach is hard-coded in spec.md, and D49 to D51 are all turned off (D42 rule 1).

**D50 \[Derived P3, P7\] Separate fast reaction from making it permanent: the surge mechanism.** The top-level eval is too slow: waiting for it to come back before scaling up misses the window. Cheap signals are untrustworthy, and letting them move the budget directly is Goodhart. So:

| Phase | Condition | Action | Cap and time limit |
| --- | --- | --- | --- |
| Trigger | k consecutive candidates \[Default 3\] from one approach beat the global current best on a cheap tier, and the Critic has not vetoed | That approach's target\_slots doubles, and its eval\_share doubles | A single approach ≤ 40% of total slots \[Default\]; lasts until the next top-level result comes back, or 24 hours \[Default\] |
| Consolidate | The top-level eval verified a candidate from the surge period | The surge share becomes permanent budget | Still subject to the 40% cap |
| Reclaim | The top level did not confirm, or the time limit expired | Shares return to their pre-surge levels; released slots go into the exploration pool | The same approach cannot surge again within 48 hours \[Default\] |

This is consistent with D12: top-level signals decide the permanent budget; cheap signals can only borrow, not take.

**Depth comes from fork, not from headcount.** Workers brought in by a surge do not start from zero. They fork from the discoverer's checkpoint at the moment of discovery (D5), each with a different sub-goal: build variants, run ablations, combine with another approach, test robustness, add evidence. Ten agents that start from zero are breadth; only ten agents that inherit the context of the discovery are depth. The discoverer lists the sub-goals when the surge triggers \[Default: 5 template categories, which the discoverer can edit\]; this is part of what it writes into the claim.

**D51 \[Derived P7\] Breadth comes from hard floors, not from judgment.**

- The exploration pool's floor anneals over time: 50% for the first 3 days, 25% after that \[Default\]. A surge never takes it. Broad early, deep late. The tasks that workers in the pool take are: new approaches, long-tail approaches, and finding counterexamples to existing dead\_end claims.
- Any approach ≤ 40%; every active approach keeps ≥ 1 slot to stay alive, so that no approach starves before its turn comes.
- Workers can propose a new approach by writing a claim with type=question, tag=new\_approach. Such claims are approved automatically up to the cap on the number of active approaches \[Default 8\]; beyond that, the Planner or a human approves.
- Slots released when a dead\_end freezes an approach go back to the exploration pool, not to the current best approach.

**The first-version allocation rule is deterministic, not an LLM.** `target_slots[a] ∝ softmax(improvement_rate[a] / τ)`, where improvement\_rate is the rate at which the approach's best top-level signal value improved over the most recent window \[Default 24 hours\]. Only the top level is used, not proxies; otherwise it would contradict D12. The first version uses the approach's own best score; the second version switches to "the marginal increment after adding it to the current best composition" (D64, H13). Between top-level windows, the allocation does not change; changes can come only from capped, time-limited surges (D50). On top of that, apply the 25% pool, the 40% cap and the 1-slot keep-alive. The larger τ \[Default 1.0\], the more even the allocation. The Planner (an LLM) can override this rule only with a stated reason; the override itself is written into decisions and can be appealed. Reason: a deterministic rule is predictable and tunable, and it can land within a week. LLM allocation is a Deferred item, to be A/B tested once there is control data.

**One metric each for breadth and depth.** Breadth = number of active approaches × embedding diversity of candidate diffs. Depth = the longest lineage chain per approach and the number of corroborated insights. Both go on the dashboard, and every surge and every reclaim is recorded as an event.

**D64 \[Derived P1, P7; round 22\] The output of optimization is a composition, not the optimum of a single path.** In both Appendix B and the harness walkthrough, the biggest step came from composition: a-02's truncation fix plus a-04's verification loop. Different approaches each bring improvements in different places, and the final deliverable is the non-conflicting parts of them put together. So candidates are composable objects from the start. Lineage allows multiple parents. "Compose candidates c-a and c-b" is one of the task templates, done by a worker in a sandbox (conflicts need judgment; it is not a mechanical merge). Composite candidates go through the eval ladder and gates like ordinary candidates. The Critic gets one more check: the relation between the composite score and the scores of its parts (≈ sum → additive; < max → conflict; in between → partial overlap), written as a claim with type=insight, tags=\[interaction\]. This is knowledge that only composition can produce.

The implication for allocation: on composable problems, approaches are not bandit arms competing for one prize. They are a portfolio, and an approach's value is its marginal contribution to the current composition. So the exploration floor should be higher, and kills fewer. The second version of D51's improvement\_rate changes to "the increment after adding it to the current best composition" (H13).

**D65 \[round 22\] best-of-N is the name of a control arm, not the shape of the deliverable.** The name comes from the swarm-scaling literature and the round-2 discussion. It means only "N workers, each running isolated, sharing nothing". The v2 first draft let this name leak into the verdict ("pick one best candidate"), which contradicts D64. The fix: all arms' candidates are merged during the run by the same Integrator (D74, D58), and B1 differs only in that its workers cannot see the baseline advance. That way H1 tests the increment of "sharing during the run" over "no sharing under the same merger", not the increment of "merging or not" or "a good merger or not". The holdout score of the single best candidate is kept as the B1-single diagnostic column.

## 9.2 Stop rules, human attention, hard-constraint gates

These are three things the first-round PoC will run into, but which v1 did not cover. They are the same kind of problem as §9.1: when resources are limited, where should attention and trust go?

**D52 \[Derived C1, C5\] Stop rules.** A system with no stop rule keeps spending money. There are four kinds of stop:

| Trigger | Condition | Action |
| --- | --- | --- |
| Budget exhausted | The Budget meter reaches the cap in constraints.md | Stop automatically, report |
| Plateau | The top-level best score has not improved for N consecutive quota windows \[Default 3\], and the exploration pool produced no new corroborated insight in the same period | Emit a plateau event and escalate to the human, with options: continue, change the charter, stop. Default action on timeout: freeze all approaches |
| All frozen | All approaches are frozen or dead\_end | Stop automatically, report |
| Target met | The top-level score reaches the target in progress.md | Report; the human decides whether to continue |

There are two signals that it is time to change the charter rather than continue. The Librarian tags them and the PI agent presents them: dead\_end claims from several approaches all attribute the cause to the same constraint (the claim carries `blamed_constraint`); insights injected by the human fail twice in a row to produce a corroborated consequence. These two signals suggest that the problem definition may be wrong, not the execution.

**D53 \[Constraint C5\] Allocating human attention.** The time the human gives the herd each day is written in the charter (`attention_minutes_per_day` \[Default 30\]). The PI agent maintains a priority queue, and every item it presents carries "about how many minutes of your time this will take":

1. Escalations close to timing out
2. The Critic overturning the current best
3. Surge consolidations or reclaims that need human approval (only when the approach's share exceeds 30%)
4. surprise sampling: N per day \[Default 3\], sorted by the cross-approach rating the Librarian tags
5. top-k spot check against hacking: 1 per day
6. Routine reports

Anything over budget is held for the weekly report. What the human looked at and what they skipped are recorded as events. This is the most reliable source for taste.md: where the human actually spends attention is more accurate than what the human says they care about.

**D54 \[Derived P3; Constraint C3\] Hard constraints are gates, not scores.** Constraints in constraints.md that can be checked automatically (non-regression tests, latency cap, cost cap, unchanged permission model, security scans) are compiled into gates. They run before eval\_request as part of T0, and once more after the result is written back. A failure means immediate refuted. It does not enter the score, the Planner cannot trade it off, and only a human can change the gate list. Two kinds of thing are kept apart: a score can be traded off; a gate cannot. A candidate that fails a gate gets a claim with type=dead\_end, reason=gate:\<name>, so that the pre-submission search can warn others too. In harness self-optimization problems, "good score, but it broke something outside the task set" is the most common incident, and this is the line of defense against it.

## 10. Alignment and run-time human–machine interaction

**D20 \[Derived P6\] The herd does not read the human; it reads only the charter and the PI agent.** The discussion between the human and the PI agent is the densest information in the whole system (C5). Hence D21: the full discussion is stored, searchable, and open to ask-back; and D22: first-wave workers fork directly from the post-alignment context \[Hypothesis H10\].

**charter directory**

```
charter/
  status.md         draft | aligned | revising; nothing starts unless it is aligned
  problem.md        the problem itself, in its negotiated wording
  why.md            why you care; what result counts as interesting
  constraints.md    budget (eval slots, days; no token budget, P10), things not to touch; the Budget meter checks against it
  progress.md       what counts as progress and what is just motion; form B must state the topline and the proxy boundary
  taste.md          preferences: elegant vs brute force, conservative vs aggressive, approach types to skip outright; what counts as gaming
  non_goals.md      what is explicitly out of scope
  escalation.md     which decisions must go back to the human; default actions and timeouts
  assumptions.md    the understanding and assumptions the PI agent lists, each confirmed or overturned by the human
  disagreements.md  points with no agreement, where both sides agreed to proceed this way for now
  sources.md        list of signal sources, with trust and readable_by confirmed for each one (mandatory)
  egress.md         egress allowlist: model API, herd tools, context sources, package mirrors (D75)
  transcript.ptr    checkpoint pointer to the full alignment conversation
```

**Alignment protocol**

1. The human describes the problem. The PI agent first writes a teach-back: it restates the problem in its own words and lists the assumptions it made that the human did not state. A weak model says "got it"; a strong model says "you didn't say this, but I assumed these three things, is that right?" This is why the PI agent must use the strongest model.
2. The human edits. The PI agent proposes 3 to 5 initial approaches, each with a rationale and a prediction of its state after one day and after one week. One mechanical diversity check is run across the approaches using embedding distance (P7).
3. Disagreements are written explicitly into disagreements.md, not smoothed over.
4. The human signs, status → aligned. The PI agent generates spec.md and approaches/, and forks the first-wave workers from its own checkpoint (2 to 3 per approach \[Default\], each with a different goal). The remaining workers take routine tasks from the queue.
5. Any change to any charter file → status automatically becomes revising → workers on the affected approaches receive a soft injection (a hard interrupt if problem.md or constraints.md changed). After realigning, the PI agent sets it back to aligned.

**escalation.md \[Constraint C5\]**

```yaml
must_escalate:
  - kill_approach_with_budget_share_over: 0.3
  - total_spend_over_fraction: 0.8
  - any_change_to: [problem.md, constraints.md]
  - critic_refutes_current_best: true
  - any_source_with: escalate_on_request
default_when_unanswered:
  timeout_hours: 8
  action: freeze_affected_approach   # not kill, and not continue
```

When the human is offline, the Planner can neither decide on its own nor wait indefinitely. The default action is to freeze the affected approach and temporarily move its eval slots to other approaches; the human decides on return.

**Run-time console: four operations**

| Operation | What the human says | What the PI agent does | What lands in the repo |
| --- | --- | --- | --- |
| Ask | "Where is approach 3 stuck?" | Reads the digest; if needed, uses ask\_checkpoint to ask the relevant workers; verifies against the original text, then answers | Nothing written |
| Inject insight | "I think X and Y are related" | Writes a claim with author=human, evidence pointing to the conversation checkpoint, tagged with approaches | claims + events; rule 5 hard interrupt |
| Change course | "Stop working on approach 2" | Edits the charter, status → revising, sets it back after realigning | charter + events |
| Ask back | "What did worker 17 see yesterday?" | ask\_checkpoint(worker-17, ts, question) | Nothing written |

Escalation requests are presented in a fixed format: what needs to be decided, two or three options, the Planner's leaning and its reason, and the default action and timeout if no decision is made. By default there is one report after each top-level eval batch, three lines long: change in the best score, newly corroborated insights, budget adjustments. The PI agent does not make escalation decisions on the human's behalf, and does not change the charter on the human's behalf.

**What persists across problems is the PI agent, not the herd.** Each problem gets one charter and one herd, which disperses when the work is done. What the PI agent accumulates is the history of taste.md and the discussion records; alignment for the next problem starts from the previous problem's taste.

## 11. Experiment design

Each hypothesis in §3 maps to one experiment. The system earns the right to talk about scale only if it consistently beats the baseline under the same eval budget, so the baselines run first.

**Arms**

| Name | Configuration | Hypothesis tested |
| --- | --- | --- |
| B0 single strong agent | One agent on the strongest model, full search, its own notes file, long run, the same eval quota | Whether the problem should not be split at all (the converse of P1) |
| B1 best-of-N | N workers, each isolated, sharing nothing, the same eval quota | The value of parallelism itself |
| S1 shares scores only | The full system, but workers, the Librarian and the Planner can see only candidates and scores | Intermediate arm: pins down the difference between sharing scores and sharing insights |
| S2 full system | The design in this document, Planner off | H1 |
| S2+P | S2 plus the Planner | The Planner's increment |

Expected: S2 > S1 > B1 > B0 holds on eval-scarce problems; B0 > B1 holds on problems where "there is only one click, and the material fits in one context". No gap between S1 and S2 → insight sharing is not working; go back and check the Librarian and the diff ranking, rather than adding workers.

**D42 \[v1 review\] Four rules for the controls; if any one is missing, the result cannot be judged**

1. All arms use the same allocation policy: in the control phase the Planner is off, and everything is FIFO plus a fixed quota; the Planner is tested separately as S2 vs S2+P. Otherwise the value of communication gets mixed up with the allocation policy.
2. The unit of measure is eval machine hours, counting every source. The extra cheap eval that S2 uses does not come free.
3. Small before large, with multiple independent runs: 5 to 10 workers, at least 3 independent runs per arm (a lower bound; the actual number is computed from D72's pilot and statistical power). Wherever this document says "seed", it means one independent run: the LLM API is not reproducible, so the variation comes from the run itself. "Consistently beats" cannot be measured from a single run.
4. Preregistration has two steps. First, use D72's pilot (3 batches) to measure the between-arm and between-run variance. Then set the effect size and the number of runs from that (for example, "S2 beats B1+M by 1.5 points on holdout\_verdict, and a paired t-test is significant"). Seal it, then start the runs; no picking metrics after the fact. A threshold set before the variance is measured is a guess. See D72 for the detailed protocol.

**D62 The verdict score is not best-so-far.** With a noisy eval, the "best score so far" goes up just from running more times: the maximum of many noisy samples rises with the number of samples, regardless of the arm's true quality. The verdict protocol becomes the following. When each arm has used up its fixed eval budget, the deliverable is the last non-guard\_pending baseline of that arm's Integrator (D74: the merger is the same in every arm and runs during the run in all of them; it is not chosen by an LLM). The deliverable is re-evaluated n times \[Default 5\] on holdout\_verdict, and the mean is the arm's score. The deliverable is usually a composition, not a single point (D64, D65). The best\_score curve is for diagnosis only. A side benefit is that this also tests the arm's selection ability, which is exactly the ability needed when eval is scarce.

**D72 \[Constraint C3; from the v3 review\] Verdict protocol: three data splits, pilot first and then set the number of runs, paired t-test.** This entry turns D42 rule 4 and D66 into executable steps. It replaces the old bar of "1.5 points higher, and true in all 3 runs", which was set before the noise had been measured.

- **The data is split three ways**, cut on day one: one for routine use; one for in-run spot checks (holdout\_run); one for the final verdict (holdout\_verdict). The verdict split is touched only once in the whole experiment: all runs are evaluated on it together after they have all ended, and nobody sees any number until that evaluation is complete.
- **The in-run spot-check rule is the same in every arm**: one spot check every 2 merges, with a total no greater than D66's cap of 10, of which 3 are reserved for bisection and the final spot check. What gets checked is a segment: from baseline@j, the one checked last time, to the current baseline@k. Two fresh paired measurements are made on this segment at the same time, one on daily data and one on spot-check data. Flag only when the difference between them exceeds 2.5·√(σ\_daily² + σ\_spot²) (σ is the single-paired-measurement noise of each dataset, obtained from A/A remeasures; when the two datasets are the same size, this is 2.5σ√2). Then bisect to find the offending merge and roll it back. Its claim is set to quarantined, and only a human can lift that. You cannot compare against the gain measured at merge time: a measurement that got past the threshold is biased high to begin with, and this bias accumulates linearly with the number of merges, while the old 2σ√k threshold grows only as √k. In simulation, even with no overfitting at all, 58% to 99% of runs raised a false alarm. The version with two fresh measurements has about 4% false alarms per run. The fresh measurement on daily data uses the spot check's own separate capacity; it does not count against the hourly rate limit, and it does not count toward holdout\_run's cap of 10.
- **Pilot**: before the formal runs, B1+M and S2 each run 3 batches (within a batch, the arms run in parallel; B0 is cheap and can be brought along). Each deliverable is evaluated 5 times on spot-check data, which gives the paired noise σ\_paired and the between-run noise σ\_run. Each source also gets A/A remeasures. These 5 evaluations count toward that run's own cap of 10 spot checks, so a pilot run can do at most 5 in-run spot checks. The pilot does not enter the verdict. The pilot cannot run until all of S2's components have been built.
- **Preregistration**: after the pilot and before the formal runs, fix the following in writing: the minimum meaningful effect (set by a human), the number of runs, quota and latency, the verdict protocol, the acceptance rule (default or strict version; no switching mid-run), the exclusion rules, and the conclusion table. Number of runs = the smallest n that gives statistical power ≥ 80%, with σ\_run taken as 1.5 times the pilot estimate. (3 batches give only 2 degrees of freedom. 1.5 times is roughly a 64% upper bound; with the estimate pooled across the two arms it is about 78%. That is why the pilot runs 3 batches rather than 2.) If S2 has to beat two control groups at once, compute joint power: 90% each gives only about 83% jointly. Running only 3 times is not enough: the effect has to be 2.3 times the between-run noise to reach 80% power, and when the effect is 1.5 times the noise, there is only a 50% chance of detecting it. So every "3 runs" elsewhere in this document is only a lower bound, and the actual number is computed here.
- **Test**: one-sided paired t-test, α = 0.05, paired by batch. Within a batch, all arms run at the same time, each with its own independent run\_id namespace, on the same frozen build, so that model-provider drift cancels out in the differences. Also report the effect size and the 90% interval. If the budget does not cover n runs, label it an exploratory run: report the interval, and it does not pass the gate.
- **Why replace 3/3**: simulation shows that its false-positive rate drifts with the noise, and when the effect exactly equals the threshold, only about 12% pass. The paired t-test keeps a 5% false-positive rate at every noise level. The simulation scripts are in `herd-sim/` (not in the repo); the review is in §4 of *Research Herd v3 Review* (not in the repo).

**D58 Arm switch table.** Exactly what each arm turns on and what it turns off is signed along with the preregistration; otherwise "sharing" has more than one meaning.

| Switch | B0 | B1 | S1 | S2 |
| --- | --- | --- | --- | --- |
| repo\_read scope | All | spec.md, approaches/, own prefix | The above + candidates/ and result events | All |
| worker ctx contents | spec + own notes | spec + own claims | spec + own claims + global candidate score table | spec + digest + diff\_since |
| search | Own notes | Off | Indexes candidates only | Fully on |
| diff\_since | None | Off | result events only | Fully on |
| Routing rule 1 (own result\_ready) | On | On | On | On |
| Routing rules 2 to 5 | None | Off | Off | On |
| Similar-candidate search before eval\_request (needs\_confirmation) | None | Off | On | On |
| Hash dedup on the Eval service side | None | Off: duplicate candidates really run and are really billed; otherwise B1 gets sharing for free | On | On |
| D26 search for dead\_end before submission | None | Off | Off | On |
| D19 forced refresh after consecutive failures | None | Off | Off | On |
| Librarian | None | Off | Off | On |
| Critic / holdout | Automatic holdout recheck | Same | Same | Same |
| gate (D54) | On | On | On | On |
| Allocation | None | Slots per approach hard-coded in spec.md | Same | Same; D49 to D51 off |
| Planner | None | Off | Off | Off (S2+P runs separately) |
| fork / ask\_checkpoint | None | Off | Off | Off (separate H2 experiment) |
| Integrator (D74) | On, same configuration | On, same configuration; not visible to workers | On, same configuration | On, same configuration |
| baseline\_advanced pushed to workers, rule 6 | Single agent; sees its own baseline | Off: workers keep working on baseline@0 | On | On |
| Executor of compose tasks | Fresh agent from outside the pool | Same | Same | Same; in addition, workers can take compose-candidates tasks (D64) |
| Spot check (D72) | Same rule | Same | Same | Same |
| Egress allowlist (D75); restart that does not rely on checkpoint (D77) | On | On | On | On |
| Pre-review (D78) | Deferred; off in every arm during H1 | Same | Same | Same |
| map/ (D76) | Initial map, rerun on its own baseline | Only the initial map on baseline@0 | Updated on baseline\_advanced; cannot add scripts | Fully on; workers can add scripts |
| Final deliverable (D62, D65) | The last non-guard\_pending baseline of that arm's Integrator; the mean of n re-evaluations on holdout\_verdict is the arm's score; the score of the single best candidate is recorded separately as the B1-single diagnostic column (D62, D65, D74) | Same | Same | Same |

**Mapping hypotheses to experiments**

| Hypothesis | Experiment | Metric | Preregistered criterion (example; set before the runs start) |
| --- | --- | --- | --- |
| H1 sharing beats isolation | S2 (Planner off) vs B1 @ 5, number of runs computed from the D72 pilot, S1 as the intermediate arm | holdout score of the final deliverable (D62, D65) | S2 beats B1+M: preregistered after the pilot, with the minimum meaningful effect set by a human; one-sided paired t-test, α = 0.05; number of runs computed from the pilot noise for statistical power ≥ 80%, with the noise multiplied by 1.5 as a margin; also report the effect size and the 90% interval (D72) |
| H2 fork fidelity | Handoff experiment: one worker runs until it reaches a surprise claim; then three groups of 5 agents each continue for one hour (a summary group, a trace group and a fork group); candidates are scored with a cheap eval | Distribution of candidate scores in the three groups; consistency with the original worker's intent (blind rating) | Fork-group mean higher than the summary group's by 2× or more |
| H3 polling is enough | Record the actual latency of every hard interrupt; sample wasted work after the fact | Interrupt latency distribution; fraction of wasted work | Share of interrupts that reach the 5-minute fallback ≤ 10%; wasted work after an interrupt (tokens and tool calls between the event's merge and the agent's acknowledgment) is reported separately. The old "median < 5 minutes" was guaranteed by the 5-minute fallback, so it measured nothing |
| H4 single repo | Run continuously for one week | merge queue backlog, CI queueing | Median backlog < 2 minutes |
| H5 proxies can be calibrated | Correlation records | Correlation of local proxies and T1 with the top level | At least one tier ≥ 0.5 |
| H7 single Planner | 5, 10, 20 approaches | Planner run time per invocation, quality of decisions (human-rated) | Run time < 1/4 of the quota window |
| H8 prediction weighting | Prediction error vs subsequent success rate | Correlation | ≥ 0.3, otherwise drop the weighting |
| H9 Scout | From week 5, Scout on vs off | Reuse rate of cross\_approach insights | ≥ 1 insight cited ≥ 2 times per week |
| H10 first-wave fork | Half of the first wave forks, half reads the spec | Candidate diversity and cheap-eval scores after one day | Fork group is no worse, and its diversity is no lower |
| H11 cross-machine fork | Week-0 spike | Migration time, snapshot size | Migration < 2 minutes, snapshot < 1 GB |

**Four standing metrics**

| Metric | Definition | Collected by |
| --- | --- | --- |
| holdout score of the selected candidate under a fixed budget | Used for the verdict. When its budget runs out, each arm selects one candidate itself, which is re-evaluated n times on the holdout and averaged; the curve of best\_score against cumulative eval machine hours is for diagnosis only | Eval service |
| dead\_end re-walk count | Number of times a candidate whose similarity to a confirmed dead\_end exceeds the threshold is still submitted to T1 or above | Indexer, automatically; does not depend on labels inside the system under test |
| insight reuse count | Distribution of the number of times a type=insight claim is referenced through depends\_on or by top-level verified candidates | The Indexer's reuse\_count |
| Prediction error | Difference between predicted and actual, by agent and by approach | Eval service |

Auxiliary: embedding diversity of candidate diffs (convergence alert), correlations between tiers, the Critic's refuted rate, and the fraction of wasted work (labeled through post-hoc sampling; used only for diagnosis, not for the verdict). All arms record full traces, so they can be replayed afterward.

## 12. Resources and scale

**Machine allocation \[Constraint C7\], 100 machines**

| Use | Machines | Notes |
| --- | --- | --- |
| Worker + sandbox | 80 | 3 to 4 workers and sandboxes per machine; local proxy evals run here. When the eval is an external system, this is the cluster's main use |
| Local cheap eval tier | 12 | Enabled only when the problem has a subset or small instance that can run locally; otherwise merged into the worker pool |
| Repository, Indexer, Eval service (only a client of the external queue), Budget meter, object storage gateway | 4 | One hot standby replica each for git and object storage; the repository is bundled to object storage every hour |
| Spare | 4 | Replica failover, and temporary sandboxes during surge |

This table is illustrative. The actual numbers are computed from the declarations in sources.md. For problems whose eval itself runs on the cluster (such as form A's harness self-optimization), the worker pool gives up machines in line with the eval's usage.

**Models \[P10\]**: All roles use the same strongest model, with no tiering. First-wave workers are forked from the PI, so they are the same model anyway (D22). Before v2.2 there was a tiering table here (D47). It was withdrawn in round 29.

**Order-of-magnitude estimates**

Each worker runs continuously at about 2M to 5M tokens/hour. The instantaneous rate of 50 workers may reach tens of thousands of tokens per second. Tokens have no budget (P10). The only thing to check before launch is the API's concurrency and rate limits: they decide how many agents can run at the same time, and the harness must have backoff (C7).

**Scale limits \[Hypothesis H4, H7\]**

| Scale | Bottleneck | Handling |
| --- | --- | --- |
| 50 | None | This document's design |
| 200 to 500 | CI duration; the Router check's O(events × leases) | CI tiering is already done; shard the Router check by approach |
| 1,000+ | Single-repository clone and merge throughput; number of Librarians | Split repositories by approach; move the source of truth to a database, and git degrades to an audit log |

**Single-task duration \[Default 20 minutes to 2 hours\]**: For tasks shorter than 10 minutes, hard interrupts are pointless; reading at task boundaries is enough. Tasks longer than 4 hours must be split into segments with forced checkpoints. This default is also a precondition for H3 to hold.

## 13. Failure modes

Each one has an observable signal; otherwise the countermeasure cannot be triggered. The last column says which decision the pressure lands on. If the signal appears in the PoC, look up that decision's fallback in the register.

| Failure mode | Signal | Countermeasure | Pressure lands on |
| --- | --- | --- | --- |
| Reward hacking spreads through sharing | Score jumps; gap between top level and holdout; Critic refuted rate rises | D10 isolation; holdout; jumps trigger the Critic; refuted cascades to descendants; audit reasons are not public | D10, D11; residual: the Critic is fooled, human spot check |
| All workers converge | Embedding diversity of candidate diffs drops | Raise the exploration share; new approaches are forced to fork from different checkpoints; leases limit same-approach concurrency | D24; residual: there really is only one path |
| The blackboard turns into noise | High diff truncation rate; low search reuse rate; dead\_end re-walks rise | low\_signal tag; digest structure and caps; evidence is mandatory | D6, D14 |
| Junk claims | High share of question claims; reuse\_count stays at 0 for a long time | Allow a dead\_end that says "no findings this time"; reuse\_count feeds into queue weight | D4 must-write before ending |
| Nobody digests eval results | No follow-up candidate within 24 hours after result\_ready | D28 | D28; residual: checkpoint is too stale |
| False refuted cascade | Number of reinstated events; tasks revoked after a cascade | No cascade from a single source; second confirmation; reinstated | D34 |
| Planner as a single point of failure | Run fails or times out; decisions stop updating | Keep using the previous decisions; workers claim tasks on their own | H7 |
| Librarian bias | Low digest coverage | Workers can bypass it; the Critic spot-checks consistency | D14 |
| A hard interrupt breaks a worker in the middle of the click | Wasted work re-entering state after the interrupt | Only at tool call boundaries; checkpoint before the interrupt; attach "can fork to continue" | D17 |
| The human becomes the bottleneck | Escalation backlog; number of times default actions fire | Default actions and timeouts; batch presentation | C5; residual: the wrong default action was chosen |
| Rebase storm | Merge queue backlog | append-only; CI tiering; batch rebase | D31, H4 |
| git or object storage as a single point of failure | Service unreachable | Hot standby replica; hourly bundle | D30 |
| Dangling evidence pointers | CI fails to resolve them | Archive only, never delete; resolve at merge time | D33 |
| Lease misjudged during a long sandbox run | Two leases for the same task | Background heartbeat; check the process before reclaiming | D32 |
| API rate limiting | 429 ratio | Backoff; per-role quotas | C7 |
| Nobody enforces the budget | Cumulative usage exceeds constraints | Budget meter | v1 review |
| Prompt injection in claim bodies | Librarian or Planner reads unusual instructions | Keep only structured fields before anything enters the digest; the Planner reads only the structured part | D13, D15 |
| Prediction weighting rewards conservative candidates | Abnormally low variance in candidates from high-accuracy agents | Weighting applies only to the information share | D37, H8 |
| Cheap eval tier is uncorrelated with the top level | Low calibrated correlation | Automatic downgrade; fall back to T0 plus the top level | H5 |
| Fork inherits the wrong context | Workers from the same checkpoint go astray together | Limit on the number of first-wave forks; goals must differ; diversity check | D22, H10 |
| Form B's proxy gets gamed | The proxy rises, the topline does not | D12: only the topline moves the budget; the Critic checks transfer; proxy\_only flag | D12 |
| Composition conflicts or negative interactions | A composite candidate scores below the maximum of its parts | Composite candidates also go through gate and eval; the Critic writes an interaction claim; two conflicting approaches are no longer combined automatically | D64 |
| Leakage between arms | Other workers' candidate ids appear in a B1 worker's trace | Separate repository and namespace per run and per B1 worker; workers have no push credentials | D67, D68 |

## 14. Minimum viable version and build order

Prerequisite: the testbed (the separate document *Herd Testbed* (`herd-testbed.md`)) is built first, over 3 days, in parallel with the herd's week-0 spike. The eval that the herd uses in week 1 is the testbed's slow tier service. The first PoC round has only one purpose: to turn H1 into verified or refuted. For this there are two columns. Before the gate is what is required to validate H1; after the gate is integrated from week 4 on. Neither affects the H1 deferrals. What is cut is marked "Deferred" in the register, not deleted.

**The minimum viable version includes**

**Before the gate (required to validate H1, weeks 1 to 3)**

- Interfaces for the six ports, in-memory adapters, local Executor, MCP tool service (D59, D60); the four charter files needed before the gate are written by hand by a human: params.yaml, constraints.md, progress.md, status.md (the alignment process itself comes after the gate); repository conventions, state machine, append-only, synchronous CI checks, event schema (D30, D31, D9, D34, D56)
- Task queue and leases in KV (D55, D32)
- One worker's full loop and all hooks, including immediate push and the must-write before ending (D1, D35, D36, D4)
- Indexer: SQLite, embedding, diff\_since, search, follow-up tasks after result\_ready (D13, D18, D28)
- Router checks at tool call boundaries (D29)
- Eval service: source declarations, ladder derivation, dedup, prediction recording, quota plus FIFO, gate (D39, D37, D40, D54)
- One Librarian (D14)
- Automatic holdout re-check; the Critic's LLM audit goes after the gate
- Budget meter and simplified stop rules: budget exhausted, target met (D43; two of D52's rules)
- spec.md and the first approaches are written by hand by a human; slots per approach are hardcoded
- Arm switch table and preregistration (D42, D58)
- Integrator: first-in-first-out, rebase, compose tasks, paired remeasure, acceptance rule, guard batches, closeout order (D74); the same configuration in all four arms, and the testbed's two control arms use it too
- Paired measurement: result\_ready carries baseline and paired; the candidate hash includes the baseline identifier (D73, D69)
- Spot checks and pilot: the spot-check rule with two new measurements, a 3-batch pilot, A/A remeasure (D72)
- Egress allowlist (D75); the restart path that does not rely on checkpoints (D77, usable before the gate)
- Opportunity map: a human writes the first batch of measurement scripts by hand, and the Integrator reruns them after baseline\_advanced (D76)

**After the gate (integrated from week 4)**

- checkpoint, fork, ask\_checkpoint: the output of the week-0 spike (D5)
- PI agent, charter directory, escalation, the human attention queue (D20 to D23, D53)
- The Critic's LLM audit (D11)
- The Planner only writes overrides with reasons (D15)
- Deterministic allocation rule, surge, breadth floor (D49 to D51)
- The plateau and all-frozen stop rules (the other two rules of D52)

**Deferred until after H1 is validated, each with its own A/B**

- Scout (D16, H9)
- One Librarian per approach
- NATS push (only if H3 fails)
- Proxy scorer and novelty ranking (D41)
- ASHA early stopping
- Scaling beyond 50

&#91;embedded content: Build order · week-0 spike in parallel, four-week main line, one gate per week\]

How to read it: diamonds are gates. The week-3 gate is the first real test of H1. If it does not pass, go back and fix the communication layer; do not add workers.

0. Week 0 (in parallel with weeks 1 and 2, two weeks): spike H11. A checkpoint carries sandbox state and can be restored and continued on another machine. The output is a minimal migration implementation plus numbers for migration time and snapshot size. Unless this is done first, D5 and D22 are both just wishes.
1. Week 1: interface code for the six ports, in-memory adapters, local subprocess Executor, MCP tool service; repository directories and schema; state machine and event permissions; synchronous CI checks; the Indexer's SQLite; one worker's full loop; the Eval service's T0 and top levels, with dedup, prediction recording, result\_ready; two holdouts cut and sealed (D66); Budget meter. Run L0 to all green, then run B0.
2. Week 2: leases in KV; merge queue and tiered CI; cheap eval tier and correlation calibration; local proxy recording; embedding and search; quota plus FIFO; a separate run\_id namespace per run and a separate repository per worker for B1 (D67); workers get read-only clones (D68). Run testbed problem A until the scorecard is all green; add the Integrator and paired measurement (D73, D74), and switch the testbed's two control arms to use it.
3. Week 3: Router checks; diff\_since and cursor; all hooks; one Librarian; Critic and holdout. Run S2 (Planner off) vs B1 @ 5, with S1 as the intermediate arm. Once the components are complete, first run the 3-batch pilot per D72, then preregister, then run the formal H1; the number of runs is computed from statistical power. The week numbers are illustrative. Follow the gates, and push the schedule back if the time is not enough.
4. Week 4: k8s Executor adapter (the second adapter, which also tests H12); integrate checkpoint, fork, ask\_checkpoint, and run the handoff experiment to decide H2; PI agent and charter; Planner; scale to 20. Run S2 @ 20 and S2 vs S2+P.
5. Week 5 onward: A/B the deferred list item by item; scale to 50; let the system run continuously for a week on 20 workers to see how it breaks, then add features.

## 14.1 Acceptance ladder: how to know the backbone is right before connecting a real problem

**D70 \[Round 23\] Three levels of acceptance. Each level answers one question, and their time costs differ by an order of magnitude. Do not go to the next level until the previous one is all green.** A layer is missing between unit tests and H1. Hooking directly onto a big problem and running it for two days before finding out the backbone has a problem costs too much, and you cannot tell whether the system is broken or the hypothesis is wrong.

| Level | Question asked | Time | Configuration | What failure means |
| --- | --- | --- | --- | --- |
| L0 plumbing | Are the pipes and invariants right | Seconds | One process, in-memory adapters, scripted workers (no LLM) | Code bug |
| L1 testbed problems A, B | Do the mechanisms work as designed in the hands of real LLM workers | A: one round per two days; B: one round per two days | N LLM workers, local Executor, slow tier artificial scarcity, three arms with the same quota | The backbone is broken, or the problem does not put enough weight on combining; not that the hypothesis is wrong |
| L2 real problem | Does H1 hold | Days | §11 | The hypothesis is wrong, or the problem is not suitable |

**L0: the 20 assertions of the plumbing test** (one test per assertion; scripted workers act on a fixed policy; everything runs in a few seconds on in-memory adapters)

1. claim\_task selection order: for\_agent first, then priority, then the approach with the largest target\_slots gap (D55).
2. Write one claim → merging produces a seq → the Indexer replays from zero twice, and the state is exactly the same both times (D56).
3. eval\_request is two-phase: similar candidates exist → needs\_confirmation; call again with the token → queued, and the result claim has already been created (D57).
4. A second submission with the same baseline and the same payload → dedup\_hit, and the quota is not decremented; changing only the baseline identifier changes the hash (D69, D73).
5. result\_ready → the follow-up task's for\_agent is the submitter; when the submitter holds a lease, interrupt:\<agent> is written (D28).
6. A single-source refuted does not cascade; a Critic-sourced one cascades to depends\_on, and reinstated undoes it (D34).
7. gate fails → not counted in the score; writes dead\_end reason=gate:\<name> (D54).
8. lease TTL expires → the task goes back to the queue once and only once (D32).
9. Budget meter accumulates to the cap → stop event (D52).
10. Arm switch table: under the B1 configuration, search and diff\_since return empty, and routing rules 2 to 5 do not fire; S2 has everything on (D58).
11. Integrator: textual conflict → issues a compose task; no conflict but the baseline has moved → exactly one paired remeasure; gain not above 1 standard error → integration\_failed, and the author gets a follow-up task; under the B1 configuration, workers do not receive baseline\_advanced (D74, D58).
12. The worker role reads private:// → denied; a worker pushes to the repository → denied (D63, D68; the in-memory adapters must simulate permissions too).
13. result\_ready carries baseline, paired gain, standard error, n\_items, n\_reps; n\_reps does not count cache hits (D73).
14. baseline\_advanced's files\_changed intersects the lease's files\_touched → hard interrupt; no intersection → soft interrupt (§8 rule 6).
15. A guard batch fails → bisect, write a new baseline version that excludes the culprit, and leave old records unchanged; the deliverable is never guard\_pending (D74).
16. Spot check: a scripted overfitting candidate is flagged, reverted, and set to quarantined; in a clean scripted run, none of the 7 spot checks raises an alarm (D72).
17. A worker sandbox accesses a host outside the allowlist → denied (D75).
18. Context usage goes over 70% → write a claim and start a new session with the start pack; the process is killed with kill -9 → the sidecar writes a stub claim on its behalf (D77).
19. Arm fairness: the Integrator configuration is identical item by item across the four arms; whoever executes compose tasks is not in any arm's worker pool (D58).
20. Closeout order: budget exhausted → stop accepting → guard\_pending catch-up check → final spot check → freeze the deliverable; the deliverable is not guard\_pending (D74, D79).

**L1: the testbed's two problems (A simulator scheduling policy, B planted-bug harness)**

The problems themselves, the planted structure, the scorecard, the control groups and the build order are all in the separate document *Herd Testbed* (`herd-testbed.md`). Here we only describe its place in the ladder. Its value is as a positive control for the backbone. The problems are designed for sharing, so the herd must beat both the "single agent" and the "isolated" control groups. If it does not win, either the system is broken or the problem does not put enough weight on "combining". It cannot prove H1. The earlier synthetic problem, Knobs, was dropped. Its difficulty was that the material exceeds one context window, and an agent that uses grep and sampling can handle that (round 26). The reason that holds up is "number of approaches to iterate on × time to wait per round exceeds the wall-clock time"; A and B both have this structure.

**The questions L1 must answer** = the scorecard in §6 of the testbed document: breadth, composition, share of available gain captured, dead-end re-hits, conflict handling, quota discipline, prediction error, tilt, generalization. All are computed automatically from the recorder, not by a human reading. The original 10 questions are folded into it.

Move on to B only after problem A passes, and move on to L2 only after B passes. The testbed accepts three changes from the v3 review: the data is split into four parts (public, daily, spot-check, verdict); control groups are judged on the merged deliverable (B1+M, with the single best kept as a separate diagnostic); each caller sees only its own submission history. The v3 request to "plant two more levers" is not taken; it served that review's static division-of-labor control group. The L2 problem is chosen by a human (Appendix C), but artificial scarcity must be kept. Even if the real eval takes minutes, the Eval service pads latency to ≥ 10 minutes and holds the budget to 2 per hour for the whole team. Otherwise what is being tested is not the reason this design exists (C1).

## 15. Decision register

All decisions are listed in full by number. During implementation this is the checklist. When something goes wrong in the PoC, first use the signals in §13 to find which D the pressure lands on, then come here to check its status. Constraints do not move. For derived decisions, question the principle first. For hypotheses, switch straight to the fallback. Defaults are tuned directly. Rows marked "Deferred" are not in the minimum viable version.

| D# | Decision | Status and basis | What would overturn it | Fallback | § |
| --- | --- | --- | --- | --- | --- |
| D1 | Only one task form: advance the whole problem with method Y; every worker sees all the material | Derived P1 | The control group split by material is no worse | Allow the dispatcher to split by material (equivalent to giving up P1) | 7 |
| D2 | No up-front decomposition; a subproblem can only be raised during work by an agent that has seen the whole picture, and it must be statable without the parent context | Derived P1 | Same as above | Same as above | 7 |
| D3 | Any merge is done by one agent that holds search access to the original text; no combining of summaries | Derived P1 | The control group that combines summaries is no worse | Allow mechanical merging | 8 |
| D4 | Traces are kept in full; a worker must write ≥ 1 claim with evidence before it ends | Derived P2 | A high share of junk claims that tags cannot suppress | Change to a mandatory contribution summary before ending; traces are still kept | 7 |
| D5 | fork replaces handoff, used only for branching at decision points, not for context decompression; a checkpoint includes sandbox state and context usage | Derived P2; Hypothesis H2, H11 | H2: summary group ≈ fork group; H11: migration is too slow or the snapshot is too large | Trace pointers + ask back; or conversation + command-log replay | 7 |
| D6 | Summaries are only an index; they do not replace the original text | Derived P2 | None | None | 8 |
| D7 | Retrieval is pulled by the receiver when needed, not done by pushing summaries | Derived P2 | None | None | 8 |
| D8 | surprise is recorded separately; producing executable reproductions is encouraged | Derived P2 | surprise claims are never reused | Remove the tool, keep the evidence requirement | 7 |
| D9 | Each claim type has its own state machine | Derived P3; v1 review | None | None | 5 |
| D10 | Eval definitions, holdout and audit reasons are in a private store the worker has no credentials for; the blackboard holds only public metadata (git has no per-path read permissions) | Derived P3; Constraint C3 | None | None | 9 |
| D11 | The Critic audits high-scoring candidates; an audit is split into a public conclusion and a non-public reason; rate-limited | Derived P3 | Critic refuted rate is zero and there is no hacking | Lower the trigger frequency; do not remove it | 6 |
| D12 | Only the highest-trust signal can change the permanent budget; cheap signals can only trigger a capped, time-limited surge (D50) | Derived P3 (anti-Goodhart) | The topline is so sparse that the budget stays rigid for a long time | Allow calibrated proxies to move the budget at a discount, with the Critic checking transfer more often | 9 |
| D13 | Layer 0 is pure code: indexing, routing, merging, eval service, metering | Derived P4 | None | None | 6 |
| D14 | The Librarian is an editor, not a gatekeeper; workers can bypass it | Derived P4 | digest coverage < 80% | Check the digest structure, or add Librarians per approach | 6 |
| D15 | The Planner only writes decisions; it does not build the global view, does not fork, does not tune communication parameters | Derived P4; Hypothesis H7 | Planner run time exceeds 1/4 of the quota window | Shard by approach | 6 |
| D16 | Scout as a separate role makes cross-approach connections | Derived P4, P8; Hypothesis H9; Deferred | Reuse rate is zero | Remove it and rely on humans | 6 |
| D17 | Three channels: hard interrupt, soft injection, on-demand search | Derived P5 | The fixed-frequency control group is no worse | Fixed frequency | 8 |
| D18 | Cursor diff, ranked by relevance, truncated at 500 tokens | Derived C2; Default 500 | High diff truncation rate and high wasted work | Tune the truncation; change the ranking | 8 |
| D19 | After N consecutive failures, sync points become denser; N=3 | Derived P5; Default | None | Tune N | 8 |
| D20 | charter negotiation and signing; start only when aligned | Derived P6 | None | None | 10 |
| D21 | The full text of alignment discussions is stored, searchable, and can be asked back | Derived P6 | None | None | 10 |
| D22 | First-wave workers fork from the PI agent's context | Derived P6; Hypothesis H10 | The fork group is no better than the group that reads the spec | Everyone reads the spec | 10 |
| D23 | Human insights are written as claims with author=human; rule 5 hard interrupt | Derived P6 | None | None | 8 |
| D24 | approach portfolio; leases limit same-approach concurrency; budget per approach | Derived P7 | Diversity never drops | Loosen the concurrency limits | 6 |
| D25 | dead\_end is a first-class citizen, with a confirmed status | Derived P7 | None | None | 5 |
| D26 | Mandatory search for similar candidates and dead\_end before eval\_request | Derived P7 | Re-walk count is zero | Change it to a hint, not mandatory | 7 |
| D27 | At each level one agent holds that level's full picture; levels are divided by abstraction | Derived P8 | None | None | 8 |
| D28 | result\_ready binds to the submitter's checkpoint and automatically generates follow-up tasks | Derived P9; v1 review | None | None | 9 |
| D29 | Router checks use the harness's tool hook (on every tool call if there is one; otherwise only on herd tool calls), with an Executor 5-minute fallback SIGTERM; no NATS | Hypothesis H3 | More than 10% of interrupts reach the 5-minute fallback, or wasted work after interrupts is high | Add NATS push | 6 |
| D30 | A single git repository is the only source of truth | Derived P2, P4; Hypothesis H4 | Median merge queue backlog > 2 minutes | Split repositories by approach; or switch to a database | 5 |
| D31 | append-only; write only under your own prefix; directories per author | Derived C4, P2 | None | None | 5 |
| D32 | lease in KV, TTL 2 hours; the heartbeat is sent on a timer by the Executor sidecar, not dependent on tool calls | Constraint (heartbeat frequency); Default | None | Tune the TTL | 5 |
| D33 | Traces and checkpoints in object storage, archive only, never delete | Derived P2 | Storage cost is unacceptable | Delete unreferenced items older than 30 days | 5 |
| D34 | Event priority; a single-source refuted does not cascade; reinstated; ordering by merge sequence number | Derived P3; v1 review | None | None | 5 |
| D35 | Workers are stateless; principles are enforced by hooks, not by prompts | Constraint C4 | None | None | 7 |
| D36 | Each claim is pushed immediately when it is produced | Derived P5; v1 review | The commit rate overwhelms CI | Batch push, at most every 5 minutes | 7 |
| D37 | Submissions must include a prediction; prediction accuracy weights only the information share | Hypothesis H8 | Correlation between error and success rate < 0.3 | Record only, no weighting | 9 |
| D38 | Agents do not message each other; everything goes through the repository | Derived P1, P4 | None | None | 8 |
| D39 | Eval is a signal source plugin interface | Constraint C6 | None | None | 9 |
| D40 | Quota plus FIFO; local proxies are a first-class mechanism | Constraint C1; Hypothesis H5 | No tier has correlation ≥ 0.5 | Fall back to T0 plus the top level, reduce the number of workers | 9 |
| D41 | Proxy scorer and novelty ranking | Deferred | A/B once there is enough calibration data | Do not add | 9 |
| D42 | Four rules for controls: same allocation policy, machine-hour metering, multiple seeds, preregistration | v1 review | None | None | 11 |
| D43 | The Budget meter meters by role, approach and source | v1 review | None | None | 6 |
| D44 | ASHA early stopping | Deferred | A/B when the eval is a long job | Do not add | 9 |
| D45 | The charter's sources.md is a mandatory question | Derived C6 | None | None | 10 |
| D46 | The default escalation action is freeze, with an 8-hour timeout | Constraint C5; Default | Freezing causes more loss than continuing | Change the default action | 10 |
| D47 | Withdrawn (round 29, P10): originally model tiering; now all roles use the same strongest model | Round 29 | — | — | 12 |
| D48 | Jump threshold 2 standard deviations; Critic rate limit 6/hour; Scout every 6 hours; first wave 2 to 3 per approach | Default | None | Tune | Various |
| D49 | The allocation unit is the slot; adding agents = raising target\_slots and queue weight, not killing workers | Derived C1, C4 | The delay in switching approach (task duration) makes the surge window get missed | During surge, soft-inject "switch approach when done" into workers on low-priority approaches; if needed, hard interrupt after a checkpoint | 9.1 |
| D50 | surge: a cheap signal triggers a temporary boost (doubling, ≤40%, until the top-level result or 24 hours), which is made permanent only after top-level confirmation; surge workers fork from the discoverer's checkpoint and carry different sub-goals | Derived P3, P7; Default k=3, 40%, 24h, 48h cooldown | A very low share of surges confirmed by the top level (< 30%), meaning the cheap signal is too poor; or surge never triggers | Raise k, or allow only calibrated proxies to trigger; or remove surge and allocate only by the top level | 9.1 |
| D51 | Breadth hard floor: exploration ≥ 25%, single approach ≤ 40%, ≥ 1 slot per approach, new approaches auto-approved up to 8; the first-version allocation rule is a deterministic softmax rule that uses only top-level signals (consistent with D12), and an LLM can only override it with reasons | Derived P7; Default | The diversity metric keeps falling even with the floors; or a Planner override rate > 50%, meaning the rule is too poor | Tune the floors and τ; or hand allocation to an LLM Planner (bring a deferred item forward) | 9.1 |
| D52 | Stop rules: budget, plateau, all frozen, target met; two signals for changing the charter | Derived C1, C5; Default N=3 | Plateau false alarm (after stopping, a human finds there was still progress) | Increase N; on plateau, only report, do not stop | 9.2 |
| D53 | Human attention is allocated by budget and priority queue; what the human looked at is recorded as events that feed taste | Constraint C5; Default 30 minutes/day | The human always skips the queue and browses the repository directly, meaning the queue order is wrong | Reorder by the types the human actually opens | 9.2 |
| D54 | Hard constraints that can be checked automatically are compiled into gates, run before submission and after results, not counted in the score; the Planner cannot trade them off | Derived P3; Constraint C3 | None | None | 9.2 |
| D55 | Task queue in KV, fixed task schema; the Indexer maintains regular tasks and result\_ready follow-up tasks; claim\_task selection order | Derived C4; v2 review | None | None | 5 |
| D56 | Fixed event schema; seq is assigned by the serial process that merges into main; the Indexer replays by seq | Derived P3; v2 review | None | None | 5 |
| D57 | eval\_request carries a prediction per tier; the service automatically runs tier by tier and returns status; allocation state lives only in allocation/; parameters are in charter/params.yaml | Derived C1, P3; v2 review | None | None | 5, 9, 9.1 |
| D58 | The arm switch table is signed together with the preregistration; H1's gate is S2 (Planner off) vs B1, with S1 as the intermediate arm | v2 review | None | None | 11 |
| D59 | herd is an independent runtime with four layers; L2 depends only on the six ports; the cluster repository and the harness are both L0 adapters | Derived; v2 discussion | None | None | 4.1 |
| D60 | Hooks in three layers: herd tool hooks are on the MCP server side; interrupt checks use the harness's tool hook (optional capability supports\_tool\_hooks); heartbeat and fallback are in the Executor sidecar | Derived P4 | Some required hook cannot be implemented on the server side | That hook becomes a required capability of the Harness port, which narrows the set of harnesses that can be connected | 4.1 |
| D61 | One real adapter plus one in-memory adapter per port; the second real adapter comes after H1; the in-memory version is the regression test | Hypothesis H12 | Conclusions change after swapping adapters | Promote the leaked capability to an explicit field of the port | 4.1 |
| D62 | An arm's score = the mean holdout re-evaluation of the Integrator's last non-guard\_pending baseline when the fixed budget runs out, not best-so-far; a seed means an independent run | Constraint C3; external review | None | None | 11 |
| D63 | Two stores: the blackboard repository can be cloned in full; the private store holds eval definitions, holdout and audit reasons, and neither worker pods nor the PI agent have credentials for it (the PI's context is inherited by forks) | Constraint (git has no per-path read permissions); external review | None | None | 5, 9 |
| D64 | The output is a composition, not a single point: multi-parent candidates, a composition task template, interaction claims; approaches are a portfolio, with a higher exploration floor | Derived P1, P7; round 22 | The problem itself is not composable (a single proof, mutually exclusive solutions) | The charter marks composable=false, and the merge flow degrades to taking the single best | 9.1 |
| D65 | best-of-N is only the name of a control arm; all arms' candidates are merged during the run by the same Integrator; B1+M = workers cannot see the baseline advance; B1-single as a diagnostic | Round 22 | None | None | 9.1, 11 |
| D66 | Two holdouts: holdout\_run is for the in-run Critic, with a usage cap; holdout\_verdict is sealed, evaluates only the final deliverable, and has a fixed n | Constraint C3; external review N1, N5 | None | None | 9 |
| D67 | Isolation through physical separation: each run has its own run\_id repository, KV and quota; B1 has a separate repository per worker; S1 uses a filtered mirror repository | Constraint (git/shell cannot restrict reads); external review N7 | None | None | 5 |
| D68 | Worker clones are read-only; push credentials exist only in the MCP tool service | Derived D31; external review N8 | None | None | 5 |
| D69 | Candidate format: manifest.yaml + payload (patch / config / artifact); hash = baseline identifier + payload; similarity is computed on the payload only (round 30) | External review C8 | None | None | 5 |
| D70 | Three-level acceptance: L0 plumbing with 12 assertions (seconds), L1 testbed problems A, B (days), L2 real problem (days); do not go to the next level until the previous one is all green; L2 keeps artificial scarcity | Round 23; external review A4 | L1 is all green but L2 fails systematically, meaning the testbed problems miss some mechanism | Add structure aimed at that mechanism to the testbed problems, and go back to L1 | 14.1 |
| D71 | Before the PoC, first validate herd behavior on the testbed's two problems (A simulator scheduling policy, B planted-bug harness), with three arms on the same quota and the same time; the herd's Eval service is a thin client of the testbed's slow tier service, with the same interface as a real external eval, so switching to a real project does not change the herd | Rounds 25, 26 | The herd wins but the scorecard misses its targets; or it ties/loses but meets the targets | For the former, check the recorder to see where the score comes from; for the latter, change the problem, not the herd; if targets are missed, change the corresponding part per the table in §5 of the testbed document | 14.1; *Herd Testbed* (`herd-testbed.md`) |
| D72 | Verdict protocol: the data is split into three parts; pilot first, then set the number of runs by statistical power; one-sided paired t-test; spot check = two new measurements of the same baseline segment (daily vs spot-check data), comparing the difference, threshold 2.5σ√2, the same for all arms (changed in round 30) | Rounds 27, 28 (v3 review) | The number of runs computed from the pilot is far over budget; or the effect measured after preregistration is much smaller than the minimum meaningful effect set by a human | For the former, downgrade to exploratory runs and report it truthfully; for the latter, first check whether the noise estimate is too small, then consider changing the problem | 9; 14 |
| D73 | Paired measurement: the candidate and the baseline run in the same batch in the same time window, reporting per-item differences and the standard error; the candidate hash includes the baseline identifier; the slow tier returns per-seed scores | Same as above | The variance of the paired differences is not smaller than that of the absolute scores; or the same diff applied to different baselines is deduplicated as the same candidate | For the former, check whether the data batches on both sides are really aligned; for the latter, check the hash implementation | 9 |
| D74 | Merge flow (Integrator, same configuration in all four arms, running during the run in all of them): the baseline advances step by step, first-in-first-out, rebase before merging, conflicts handed to a new agent outside the pool to merge, one paired remeasure if the baseline has moved, merged only if the gain exceeds 1 standard error and it passes the gate; interactions recorded at 2σ√2; attribution report after the run (changed in round 30) | Same as above | Near-zero patches make up more than one fifth of what gets merged; or the deliverable's score is lower than the paired score of some individual candidate | For the former, switch to the strict version of the acceptance rule; for the latter, it is an interaction, and the attribution report must be able to point out which two candidates | 9; 8 |
| D75 | Egress allowlist: worker sandboxes can reach only the model API, the herd tool service, declared context sources and package mirrors | Same as above | Access outside the allowlist shows up in the recorder; or the worker's answers closely match public material | Seal the sandbox, mark the candidates refuted; check the adapter's network configuration | 9 |
| D76 | The opportunity map is only shared information, not a basis for assigning work; adds three directories, map/, baseline/, report/, and matching event types; the first batch of measurement scripts is written by a human when building the problem and rerun by the Integrator; S1 receives the updates | Same as above | The PI or Planner starts assigning people one-to-one by map entries (equivalent to going back to v3's static division of labor); or nobody reads the map | For the former, state plainly in the Planner's prompt that the map is not a task list; for the latter, remove the map, and that does not count as a mistake | 5 |
| D77 | Restart path that does not rely on checkpoints: triggered at 70% of the window, auto-compaction off, a four-item start pack, the sidecar writes a stub claim on the agent's behalf if the process is killed; usable before the gate, the same for all arms | Same as above | After a restart the agent redoes work already done more than once; or the share of interrupts reaching the 5-minute fallback exceeds 10% (H3) | Include a few more tool calls in the start pack; check whether the usage calculation lags | 7 |
| D78 | Deferred (round 30): pre-screening becomes a separate A/B after H1, done then by a pre-screener who does not read the eval code, the same for all arms; keep unlimited fast tier remeasures, and pick among multiple compose versions with the fast tier only | Round 29 (P10) | Critic vetoes are uncorrelated with slow tier results (vetoed candidates do no worse when run on the slow tier anyway) | Reduce to 1, or drop the pre-screening | 9 |
| D79 | Closeout order is the same for all arms: stop accepting → guard\_pending catch-up check → final spot check → freeze the deliverable → after all runs end, evaluate holdout\_verdict together; attribution comes after the verdict and uses daily data only; the acceptance rule is chosen at preregistration and not switched during the run | Round 30 (Opus review) | The two arms' deliverables handle guard\_pending differently; or attribution touched the verdict data | Check whether the closeout quota is the same across arms; switch attribution to daily data | 9 |

## Appendix A: Independent review of v1

This appendix is a historical record of v1. The section numbers and P numbers in the table refer to v1's numbering; for the corresponding v2 decisions, see the register in §15. The review was done by an agent that had not taken part in the earlier discussion and saw only the exported document. It raised 12 points, 5 of them severe. I adopted every one of them, in full or in part, and the body has been revised according to the table below. I keep this record so that readers know where the first draft went wrong and which changes were forced.

| # | Severity | Problem raised by the review | Resolution | Where changed |
| --- | --- | --- | --- | --- |
| 1 | Severe | The definition of verified conflicts across P4, §4 and §5. CI can only verify a result, so insight and dead\_end stay at proposed forever, while P4 forbids building on anything that is not verified. That effectively shuts off S2's core hypothesis | Adopted. Split the state machine by claim type and add a unified event-permission table; allow citing a proposed insight, but its status must be marked | §2 P4, §4 state-machine table |
| 2 | Severe | Eval results have no receiver. eval\_request is asynchronous, T2 takes hours, and the lease is released when the task ends, so no one owns the loop of "what did my candidate score, and what do I change next" | Adopted. The result\_ready event is bound to the submitter's checkpoint and automatically generates a follow-up task | §5 Eval service, §6 hook, §7 rule 1 |
| 3 | Severe | The write rules and the directory layout are inconsistent: the lease refreshes its heartbeat on every tool call, yet it lives in git; claims and candidates have no agent prefix, so the CI path check cannot run; result.json sits in a worker-writable directory and can be forged | Adopted. Moved the lease to KV; claims and events get per-author directories; inside candidates, files are split by writer, and only the Eval service can write result.json | §4 layout and lease |
| 4 | Severe | Eval throughput does not match demand: 10 concurrent T2 runs for 50 workers means one run per worker every 5 hours; there are only 6 sandbox machines; workers will inevitably build their own proxy evals in the sandbox, which are not in the resource plan and also bypass calibration | Adopted. The local proxy becomes a recorded, calibrated, first-class mechanism; sandboxes increased to 15 machines; the per-worker quota is made explicit | §8 quota and local proxy, §12 machine table |
| 5 | Severe | The experiment cannot reach a verdict: each of the four arms needs the full budget and runs only once, so under LLM variance "consistently beats" cannot be measured; B1 allocates eval slots differently from S2's Planner, which confounds the value of communication with the allocation strategy; only T2 runs are counted, while S2 also consumes T1 and T3; wasted work is labeled by the Librarian, which is inside the system under test | Adopted. All arms use the same fixed allocation strategy; metering is in eval machine-hours; 5 to 10 workers, 3 seeds, preregistered effect size; wasted work is replaced by the dead\_end re-walk count, counted automatically by the Indexer | §11 metrics table and fairness |
| 6 | Moderate | The Critic is a leak channel: it is the only role that reads eval code, yet it writes "why this scored" into an audit that everyone can read. That is exactly the hacking propagation path that §8 warns about. Its trigger conditions also contradict T3 being "very rare" | Adopted. The audit is split into a public conclusion and a rationale section that workers cannot read; rate limits are added to the trigger | §5 Critic, §8 |
| 7 | Moderate | The refuted cascade has no brake: verifier flakiness produces false refuteds; there is no undo path; the priority when CI and the Critic conflict is undefined; cross-machine timestamps are unreliable | Adopted. Event priority is human > Critic > CI; a refuted from a single source does not cascade; add the reinstated event; order by merge sequence number | §4 accompanying rules, §7 rule 4 |
| 8 | Moderate | Visibility latency contradiction: the loop pushes only when a task ends, so sharing latency equals task duration, yet §7 says hard interrupts take seconds | Adopted. Each claim, surprise and eval\_request is pushed immediately when it is produced; the commit rate is recalculated to 300/hour, and CI is tiered | §5 merge queue, §6 loop |
| 9 | Moderate | Missing failure modes: git and the object store are single points of failure; after traces are deleted at 30 days, evidence dangles; there is no heartbeat during long sandbox calls; API rate limiting; no component enforces the token budget; prompt injection in claim bodies | Adopted. Each one is added to §13; add the Budget meter component; traces are only archived, never deleted | §5 Budget meter, §13 |
| 10 | Moderate | The first version is over-designed: NATS hard interrupts, proxy models, ASHA, novelty ranking, a per-approach Librarian and the big-picture pass were all scheduled before the Week 3 gate; Week 4 was crammed with too much; the cross-machine portability of fork and ask\_checkpoint is the biggest engineering risk, yet there is no spike for it | Mostly adopted. Hard interrupts changed to polling at tool call boundaries, and NATS is deferred; proxy models, ASHA, novelty, the per-approach Librarian and Scout are all deferred to Week 5 onward, each with its own A/B; a Week 0 spike is added; Week 4 scales to 20 rather than 50. ASHA stays in the design, because it is the main lever on eval throughput | §5 Router, §14 |
| 11 | Minor | Parameters are undefined or gameable: weighting by prediction accuracy rewards conservative candidates; candidate embeddings are undefined for code artifacts; "a batch" and the k in top-k are undefined; there are two problem.md files, one at the root and one under charter | Adopted. Weighting applies only to the information share; the embedding is taken on the diff relative to the parent; one batch = one one-hour quota window; the root file is renamed spec.md | §4, §5, §8 |
| 12 | Minor | P5 conflicts with §7: in the big-picture pass the Planner forks workers and tunes diff truncation, so it is turning into the "strong agent managing the blackboard" that P5 warns about | Adopted. The big-picture pass becomes a separate Scout role; diff parameters belong to the charter; the Planner only writes decisions | §5 Scout and Planner, §7 |

**The review's overall judgment**

The biggest risk is not scale. It is broken loops: eval results with no receiver, insights that can never become verified, and too little throughput. Together, these three would leave 50 workers waiting or repeating work most of the time. The hypothesis most worth testing first is "with the same eval budget, shared insight beats isolated best-of-N". It must be tested with 5 to 10 workers, a fixed allocation strategy and multiple seeds: first establish the gap between S1 and B1, and only then talk about 50. Before that, the cross-machine portability of fork and ask\_checkpoint should be done first as a separate spike, because it decides whether P2 and P7 can hold.

I agree with this judgment. The first draft put its attention on designing the communication layer and missed the most basic loop: "who picks up the eval result when it comes back". This is the easiest mistake to make when deriving downward from principles in a discussion: the principles are all correct, but no one walked through one worker's day from start to finish. The walkthrough in Appendix B was rechecked after the revisions; the Day 1, hour 6 row is the loop that was added.

**Points still open after the revision**

- What to do if the Critic itself is fooled: for now there are only human spot checks.
- If the correlation between the local proxy and T1 never rises, the whole "eval scarcity" design degrades into best-of-N plus deduplication.
- If the Week 0 spike finds that sandbox snapshots are too large and restores too slow, fork must be downgraded to "conversation + command-log replay", and P2's fidelity takes a hit.

**v2 external review (2026-10-18, 5 items, all adopted)**

| Problem | Why it is right | Where changed |
| --- | --- | --- |
| H1's scores are not trustworthy: best-so-far rises with the number of noisy evals; seed is undefined | The maximum of several noisy samples rises with the number of samples, independent of quality; LLM APIs cannot be seeded | D62; §11 metrics table and D42 rule 3 |
| Workers can read hidden material: git clone has no per-path read permission | readable\_by is an empty promise inside a single repository | D63; D10 rewritten; §5 directories, §9 declarations |
| Hooks miss most tool calls: Claude Code's built-in Bash, Read and Edit do not go through the MCP server | "All hooks live on the server side" holds only for herd tools | D60 changed to three layers; heartbeat moved to the Executor sidecar; the Harness port gains supports\_tool\_hooks; D29, D32, H3 |
| fork cannot fix a full context: the child process starts at the limit | fork copies the entire conversation | §7 hook table changed to compaction + continuation task; D5 restricts what fork is used for, and the checkpoint records context usage |
| D51 contradicts D12: proxy scores enter the softmax, which amounts to touching the permanent budget | D12 allows only the top level to touch the permanent budget | D51 changed to consume only the top level; between windows there is only surge |

**v2b implementation-readiness review (2026-10-18, independent agent, implementer's perspective, 12 items, all adopted)**

Conclusion: there are no structural errors; what is missing is interface-level fields and consistency. Changes made: event file names now use local\_id, and the merge queue writes seq to `events/_seq/` at merge time (D56); each arm's rule for selecting its final candidate goes into D58; eval\_request automatically generates a result claim and returns claim\_id, result\_ready carries the full set of fields, two-phase confirmation uses confirm\_token, and parent is added (D57); source declarations gain a required cost\_machine\_hours; the Model port gains embed; the Harness port gets signatures for build / export\_state / import\_state; a lease's target\_claims is maintained server-side, by parsing the goal and on each repo\_write; the Router check reads only the KV key interrupt:\<agent>; D58 gains rows for routing rules, service-side deduplication, D19 and ctx composition; the four hand-written charter files are added before the gate; event.role gains librarian, claim.author gains eval\_service, and frontmatter gains tags and blamed\_constraint; D34's priority order now covers all roles; the PI agent holds no private-store credentials; D19's "failure" now has a definition; the H2 experiment moves to Week 4.

**v2 external review, second batch (2026-10-18, from another agent's verification file, all adopted)**

| ID | Problem | Where changed |
| --- | --- | --- |
| N1 | In-run rechecks and the final verdict use the same holdout, which favors the sharing arms | D66 two holdouts |
| N4 | B1 runs before the holdout exists; the timelines in §14 and Appendix C disagree | The holdout is split on day 1; the 5 workers on the first day in Appendix C are a smoke run, not a formal arm |
| Threshold | The 3/3 criterion was fixed before variance was measured | D42 rule 4 changed to two steps: first a trial run to measure variance, then seal |
| N5 | How n grows for the final selection is undefined | D66: n = params.verdict\_n, fixed |
| A4 | A PoC with minute-scale evals cannot measure scarcity | D70: L1 and L2 both keep artificial scarcity |
| N7 | Arms are not isolated: B1 workers can read other workers' candidates; the 12 runs are not separated | D67 |
| N8 | Workers can git push directly and bypass the tool checks | D68 |
| C8 | The candidate format is undefined | D69 |
| N2 | There are no checkpoints before Week 4, but result\_ready references one | D55: in the H1 phase, seed\_context and submitter\_checkpoint may be empty |

## Appendix B: Walkthrough of a hypothetical case

This walks every mechanism through one hypothetical problem (the walkthrough was written for v1; in v2 the mechanisms are numbered D1 to D58, and the logic is unchanged). All scores, counts and agent numbers are illustrative. They show how the mechanisms mesh; they are not predictions.

**Problem**

Raise an open-source 7B model's score on a math-reasoning benchmark from 61 to 70 by changing only the post-training data recipe, not the model. Eval: T2 = 6 hours of SFT on 8 GPUs, then evaluation; eval is an external system with 10 concurrent runs and about 40 T2 eval slots per day. T1 = 40 minutes on a 1B model + a benchmark subset. T0 = data-format and deduplication statistics, in seconds. T3 = a holdout variant of the benchmark, maintained by humans.

**Day 0: Alignment (two hours)**

| Step | What happened | Written to the repo |
| --- | --- | --- |
| Human describes | "61 to 70, change only the data, no benchmark contamination of any kind" | transcript.ptr |
| Teach-back | The PI agent restates it and lists three default assumptions: only SFT is allowed; data sources are limited to the existing 3 data pools; 70 means the T3 score | assumptions.md |
| Human corrects | The first is wrong: RL is allowed, but with a small budget; the second is right: synthetic data is allowed, but its source must be labeled; the third is right | assumptions.md updated |
| Taste | "I want to know why it works, not pile up data"; "one filtering rule beats ten tricks" | taste.md |
| Approaches | The PI proposes 5: a-01 filter out wrong solutions with a verifier; a-02 difficulty curriculum; a-03 synthetic multi-step reasoning data; a-04 format consistency; a-05 small-budget RL | approaches/ |
| Disagreement | The PI thinks a-04 is low value; the human insists on keeping it, with a 10% budget | disagreements.md |
| Escalation rules | Killing an approach that holds more than 30% of the budget needs the human; a T3 drop of more than 2 points needs the human; the Critic overturning the current best needs the human; freeze after an 8-hour timeout | escalation.md |
| Sign | status → aligned. The PI forks the first wave of 12 workers from its own checkpoint (2 to 3 per approach, each with a different goal); the other 38 pick up regular tasks from the task queue | status.md, checkpoints/pi/ |

**Day 1: Launch**

| Time | Role | Event | Mechanism |
| --- | --- | --- | --- |
| Hours 0 to 2 | All workers | Read spec.md and the digest (empty at this point), analyze the data pools in the sandbox, submit many T0 runs | The task shape is "advance the whole problem"; there are no slices like "read data pool X" |
| Hour 3 | worker-07 (a-01) | Submits candidate c-9f3a, predicted 64.0, rationale "the verifier filters out 18% of wrong solutions". The hook's search finds no similar candidate. T1 is +2.1 relative to baseline; it enters the T2 queue | Mandatory search before eval\_request; submissions carry a prediction |
| Hour 4 | worker-19 (a-01) | Submits a nearly identical candidate. The hash differs, but the hook reports "similarity 0.93 to c-9f3a, already in the T2 queue". worker-19 withdraws it and works on a variant of the verifier instead | Deduplication happens before submission, saving one T2 run |
| Hour 6 | Eval service | First batch of T2 results: c-9f3a actually scores 63.4, an error of -0.6. The result\_ready event is bound to the checkpoint worker-07 had when it submitted; worker-07 has already finished that task, so the Indexer generates a follow-up task seeded from that checkpoint, and worker-07 itself picks it up and continues | Prediction recording |
| Hour 6.5 | Librarian-a01 | Triggered by the 10th commit, it writes the a-01 digest and distills insight c-0102: "the difficulty distribution of the filtered data shifts clearly to the left" | The Librarian distills insights from diffs |
| Hour 8 | worker-23 (a-03) | Finds that T1 failures on multi-step problems in the synthetic data concentrate in the "unit conversion" subcategory. Calls surprise() to write a claim, with a runnable notebook that reproduces it attached. Calls fork() to start a sibling that pursues this lead, and continues on its own main line | A lead becomes an executable artifact; fork replaces handoff |

**Day 2: First allocation, first reuse, first hard interrupt**

| Time | Role | Event | Mechanism |
| --- | --- | --- | --- |
| Early morning | Planner | Runs for the first time once the first batch of complete T2 results is in. a-01 leads; a-04 has made no progress. Writes decisions: a-01 share 35%, a-04 cut to 5% (not killed, because disagreements.md has a record of it), exploration 30% | The Planner reads digests, not traces; decisions carry a rationale |
| Morning | worker-31 (a-02) | Fails T1 3 times in a row. The hook forces a digest refresh and a search; it sees c-0102 "difficulty shifts left after filtering" and realizes that its curriculum split points were based on the pre-filtering distribution. After it changes the split points, T1 +1.4 | Read more when stuck; insights are reused across approaches, reuse\_count +1 |
| Afternoon | Critic-1 | Audits a-04's high-scoring claim c-0211 and finds that the "format consistency effect" actually comes from a bug in the eval parser. Emits a refuted event and writes an audit claim | The Critic is the only role that can read eval code |
| Afternoon | Router | The target\_claims in worker-12's lease include a child claim of c-0211, and depends\_on hits the refuted → hard interrupt, with the audit attached. worker-12 receives it at the next tool call boundary, checkpoints, releases the lease and picks up a new task | Rule 4; interrupts take effect only at tool call boundaries |

**Day 3: Reward hacking and an escalation**

| Time | Role | Event | Mechanism |
| --- | --- | --- | --- |
| Early morning | worker-41 (a-03) | Candidate c-77be scores T1 +5.8, 2.5 times the historical best | A jump of more than 2 standard deviations → an alert, not a celebration |
| Early morning | Eval service | Automatically sends c-77be to the Critic and pauses its T2 | A jump automatically triggers the Critic |
| Noon | Critic-2 | The audit finds that the synthetic data's generation templates are rewrites of benchmark questions: semantic contamination. Requests T3: -0.3. Emits refuted; c-77be's two descendants are marked refuted as well | Only the Critic can request T3 |
| Noon | Router | Two workers building on c-77be get hard interrupts | Rule 4 |
| Afternoon | Planner → PI agent | Hits the "Critic overturns the current best" escalation rule. The PI brings it to the human: "Does rewrite-level similarity count as contamination?" Three options: A, ban any synthetic data whose similarity to a benchmark template exceeds 0.8; B, ban only the original questions; C, allow it but report T3 separately. The Planner leans toward A. Timeout default: freeze a-03 | The escalation format is fixed |
| Afternoon | Human | Chooses A. The PI edits constraints.md, sets status → revising, soft-injects into a-03's workers, and changes it back to aligned | Course changes go through the charter, not through prompts |
| Evening | Librarian-a03 | Writes a dead\_end claim: "synthetic data whose templates derive from the benchmark in any form is a dead end; T3 does not rise". From then on, the search before every a-03 eval\_request hits it | dead\_end is a first-class citizen |

**Day 4: The big-picture pass and a human insight**

| Time | Role | Event | Mechanism |
| --- | --- | --- | --- |
| Early morning | Scout (big-picture agent) | Reads all digests and spot-checks surprise claims. Finds that the leftward difficulty shift after a-01's filtering and the shortage of data in the second half of a-02's curriculum are two sides of the same thing. Writes a cross\_approach insight and forks worker-52 directly from its big-picture context, with goal: "filtering + targeted synthesis to fill the difficulty gap left after filtering, using only non-benchmark sources" | The cross-approach click comes from one mind that holds the full picture; fork writes no task description |
| Morning | Human → PI agent | "I remember a paper saying that keeping a certain share of wrong solutions plus critiques in SFT data actually helps." The PI writes a claim with author=human, with evidence pointing to this conversation, tagged a-01 | Human insight enters the store without loss |
| Morning | Router | Rule 5 hits, and a-01's workers see it through a hard interrupt. worker-07 uses ask\_checkpoint to ask the PI whether the human mentioned a specific proportion in that conversation, gets "no, the human isn't sure either", and forks a sibling to sweep the proportion | Ask back |

**Days 5 to 7: Convergence**

| Time | Role | Event | Mechanism |
| --- | --- | --- | --- |
| Day 5 | worker-52 | Candidate c-c1d0: predicted 67.5, T1 +4.9, T2 66.8, T3 66.5. The Critic's audit passes: the score comes from difficulty coverage, not contamination. verified | The cross-approach insight pays off |
| Day 5 | worker-07's fork | The sweep of the critique-data proportion finds 15% best, giving T2 +0.8 on top of c-c1d0. Merged candidate c-e4a2: T2 67.9 | The human insight pays off |
| Day 6 | Planner | Puts a-05's small-budget RL on top of c-e4a2. T2 69.2, T3 68.9 | Budget follows progress |
| Day 7 | PI agent → Human | Report: T3 68.9, the target of 70 is not reached; by the definition in progress.md (+4 per week counts as significant progress), this is significant progress. Suggested focus for next week: raise the RL budget, close a-04 | A three-line report |

**The accounting after seven days (illustrative)**

| Metric | Value | Notes |
| --- | --- | --- |
| T2 usage | 268 / 280 | Deduplication and T1 screened out about 60% of submissions |
| Best T3 | 61 → 68.9 | Over the same period, the B1 best-of-N control group reached 65.1; S1, sharing only scores, reached 66.3 |
| dead\_end re-walk count | 7 | 4 of them were cross-approach dead\_ends ranked too low in the diff ordering, so workers didn't see them. Change the ranking rule |
| insight reuse | 5 of 12 have reuse ≥ 2; the highest is 7 | The highest is c-0102 "difficulty shifts left after filtering" |
| Median prediction error | 0.9 points | worker-07 and worker-52 are the most accurate; their queue weights are raised |
| Candidate diversity | A convergence alert fired on Day 2 | Recovered after exploration was raised from 30% to 40% |
| Critic refuted | 3 | One eval bug, one semantic contamination, one descendant refuted along with its parent |
| Handoff experiment | Summary group +0.6, trace group +1.7, fork group +3.1 (T1 mean) | fork and summary differ by 5x; the communication-layer budget is adjusted accordingly |

**What this case verified, and what it did not**

Verified: each of the nine principles played a role in at least one event; under eval scarcity, the search and prediction before submission save a large number of T2 runs; hard interrupts are useful when a refuted propagates; human input enters without loss through both paths, the charter and claims.

Not verified: whether the Planner's allocation decisions are optimal (there is no control); what the system would do if the big-picture pass had not found that click; whether 50 workers really beat a single strong agent (that needs a comparison with B0, and this case did not run B0).

## Appendix C: PoC kickoff pack (the first instruction to the implementation agent)

This page can be used as-is as the first message to the implementation agent. Its first deliverable is not code. It is an implementation plan, and code gets written only after the human approves it.

Prerequisite: the first 3 days of the testbed (the separate document *Herd Testbed*, `herd-testbed.md`) are done first, and all four prechecks pass; the herd's Eval service is only a client of the testbed's slow-tier service. **Scope**: the "before the gate" column of §14. Do not build fork, the PI agent, the Planner, the Critic's LLM audit, or surge. The goal: by the end of Week 3, produce a preregistered verdict for S2 (Planner off) vs B1 @ 5 workers (compute the number of runs from the D72 pilot; if the budget is not enough, label them exploratory runs), and so turn H1 into verified or refuted.

**First deliverable (Day 1): an implementation plan with five parts**

1. interfaces.md: the schemas for task, event (including the Integrator's new events), lease (including files\_touched), eval\_request (including baseline) and baseline/\<k>.json, following §5 and §9, with field types and error codes filled in; the final version of the arm switch table (D58); the format and first set of rules for `router/rules.yaml`; the keys of `charter/params.yaml`; the private store's layout and credential boundary (D63); the holdout re-evaluation process from D62.
2. Port interface code: write the six ports of §4.1 as interface files, plus in-memory adapters and a local-subprocess Executor. This is the boundary of herd as an independent runtime, and it comes before integrating any cluster or harness. The first Harness adapter uses Claude Code headless; herd tools are exposed through the MCP tool service, with their hooks on the server side; the interrupt check uses Claude Code's PreToolUse hook; heartbeat and backstop live in the Executor sidecar (the three layers of D60). k8s is the Executor's second adapter, integrated in Week 4; see the mapping table below.
3. Repository skeleton: the directories from §5, with empty files and schema examples.
4. Milestones for Weeks 1 to 3, with the gate at the end of each week as set in §14.
5. The answer column of the question table below. Do not guess any that are left blank.

**Default mapping for the k8s Executor adapter (the second adapter, Week 4; the implementation agent fills in the details after reading the cluster repo)**

| Design component | k8s resource | Notes |
| --- | --- | --- |
| Worker × N | One Deployment, replicas = total slot count | Allocation between approaches is queue weight, not pod count; surge changes only the weights, and scales only if necessary |
| sandbox | One ephemeral pod per task, or a sub-container inside the worker pod | The working directory is mounted on a PVC, or tarred to the object store when the task ends, as one half of the checkpoint |
| git repo + CI hook | A single-replica StatefulSet runs the git server; pre-receive runs the sync checks, post-receive calls the Indexer | Hot-standby replica following the existing pattern |
| merge queue | Single-replica Deployment, serial ff-merge, assigns seq | The single replica is deliberate: seq must be globally monotonic |
| Indexer | Deployment, receives the post-receive webhook | SQLite plus a vector extension, or a database that already exists in the cluster |
| KV: leases, task queue, eval queue state | Redis or an equivalent that already exists in the cluster | If there is none, add one |
| Eval service | Deployment: a client for the external eval system plus a queue | With an external eval, it is thin |
| Budget meter | Part of the Indexer, or a small standalone service |  |
| Object store: traces, checkpoints | The cluster's existing one |  |
| Librarian, Critic, PI agent | One Deployment or CronJob each | The PI agent is a long-lived single replica |
| Inter-pod communication layer | Only for infrastructure RPC: workers calling the Eval service, reading and writing KV | Not for agents to message each other (D38); otherwise the blackboard is incomplete |

**Questions you must ask the human first**

| Question | Why you cannot guess |
| --- | --- |
| Which concrete problem the PoC uses; the eval's latency, daily quota and concurrency; whether there is a holdout | Determines the ladder, the quota, and whether a local proxy is needed. The system itself is validated on the testbed's problems A and B, not on the real problem; for the real problem at L2, even if eval is fast, the slow-tier service must enforce artificial scarcity (latency ≥ 10 minutes, 2 per hour for the whole team) |
| Preregistration for the gate: effect size, whether N is 5 or 10, number of seeds, eval machine-hours per arm | A comparison without preregistration cannot reach a verdict (D42) |
| Which harness and which model ID (the same for all roles); the API's concurrency and rate limits | Determines the hook mechanism and the checkpoint format; determines how many agents can run at once |
| Who hand-writes spec.md and the first approaches; how many slots per approach | There is no PI agent in the H1 phase |
| Access to the cluster repo; which services already exist in the cluster (KV, object store, git, image registry, database); the worker pod's resource spec and the cap on the total number of pods | Determines technology choices |
| Each source's trust and readable\_by (D45); whether the private store is a separate repo or an object-store bucket; how worker pod credentials are isolated | Determines whether definitions go on the blackboard or in the private store (D63); if isolation cannot be achieved, all anti-hacking measures fail |

**After the plan is approved: three things for the first day**

1. Using the in-memory adapters, run a miniature version of the whole herd end to end in one process: one toy problem, a fake SignalSource (returns scores after a fixed delay), two workers, completing the loop claim\_task → eval\_request → result\_ready → follow-up task. This is the regression test for every later change.
2. Swap in real git, KV and the local-subprocess Executor. With the same L2 code, start 5 isolated workers on testbed problem A and run them for one hour as a smoke run (not a formal arm). Get the first best\_score diagnostic curve, and walk once through the Integrator (merge, remeasure, spot check, closeout) and the holdout\_verdict flow to confirm that the verdict pipeline works end to end. B1 is the base of every arm, so running it first wastes the least; 5 to 10 workers fit on one large machine, and k8s is needed only at 20 or more.
3. Write the first thing that breaks as a new row in §13.

**Definition of done for the first round**

- The 20 L0 assertions and the scorecard for testbed problem A are all green (§14.1); then run S2 vs B1+M @ 5, with the number of runs computed from the D72 pilot (if the budget is not enough, label them exploratory runs), and produce the result by the preregistered criteria. Win, lose or no verdict: all three count as done.
- The dashboard for the four standing metrics is viewable.
- Every D in the register that this round touched is marked "verified / refuted / not exercised".
- A retrospective: which hooks and defaults made a difference, and what the defaults change to for the next round.

**Don't**

- Don't write all the components before running anything. Run B1 first.
- Don't start a comparison without preregistration.
- Don't decide on your own which problem the PoC uses.
- When you hit something the document doesn't define, check §15 first; if it isn't there, add a D row marked "decided during implementation". Don't decide it silently.
