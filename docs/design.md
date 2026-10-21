# VLAH design overview

This page explains why VLAH is built the way it is and how we will test that it works. To use VLAH, start with the [README](../README.md) and the [quickstart](quickstart.md).

VLAH runs many LLM agents together on one hard, wide optimization problem, for cases where the only trustworthy judge is slow and scarce. The design documents call it Research Herd.

> **Status:** design phase. The first code will be the testbed (see [Roadmap](#roadmap)).

## The problem

Some optimization problems are too big for one agent:

- **They are wide.** Five to ten different areas could each improve the goal, and nobody can list them all in advance.
- **They need a lot of context.** Understanding one area takes more reading than fits in one agent's context window.
- **The judge is scarce.** The evaluation that counts is slow, rate-limited and hard to fool, for example a 30-minute benchmark that can run twice an hour.
- **The answer is a combination.** The best result usually combines improvements from several areas, rather than being the single best attempt.

**Example:** raise an agent harness's prompt-cache hit rate. Several changes each help a little, and some of them overlap:

- reordering the prompt;
- making tool definitions deterministic;
- stripping timestamps;
- moving retrieved documents later in the prompt;
- cache-aware truncation.

The deliverable is the set of changes that work together.

## The idea

A herd of agents shares what it learns while it works, and plain code, not an agent, guards the scarce judge.

- **Each worker takes one direction and attacks the whole problem.** The directions are a portfolio, not fixed lanes.
- **A shared blackboard.** Every result, insight and dead end is written as a claim to an append-only repository. Workers can search it, and relevant updates are routed to them.
- **The judge is rationed by code.** An eval service owns the quota. It drops duplicates, runs cheap checks before expensive ones, measures each candidate paired against the current baseline, and enforces gates.
- **The baseline ratchets forward.** Verified improvements are merged one at a time onto a moving baseline. Each one is re-measured and kept only if it still helps, so the deliverable is a combination of improvements.
- **Agents judge; code keeps the books.** Plain code decides who sees what and what counts, so those decisions can be audited.

## What must always hold

Any version of VLAH must keep four invariants:

1. **Nothing learned is lost.** Everything learned is kept, and anyone who needs it can find it.
2. **No path is unknowingly walked twice.** No one retraces a path someone already walked without knowing it.
3. **No judge call is wasted.** Every scarce evaluation goes where it can change the next decision.
4. **Results combine.** The deliverable combines every improvement that works with the others, not just the best single attempt.

Some constraints come from the environment and hold however strong the model is:

- the judge is scarce;
- each agent's context window is finite;
- human attention is about 30 minutes a day;
- workers can't see the evaluation definition or the hidden data;
- network egress is restricted to an allowlist.

Tokens are not a constraint. Every role uses the strongest model. Anything that costs model calls but no judge calls can be done redundantly.

Everything else is scaffolding for today's models, and should shrink as models get stronger. That includes the roles, the blackboard layout, the routing rules and the default values.

## The hypothesis

The core claim is not yet proven. **With the same wall-clock time and the same number of judge calls, agents that share during the run should beat the same number of agents that work in isolation and are merged by the same procedure.**

The baseline to beat is not one agent. It is a herd with unlimited tokens whose members don't talk to each other. The bet is on structure, not on compute.

## How we'll know

1. **Testbed.** Two synthetic problems built so that sharing should matter:
   - A, a scheduling policy for a simulator;
   - B, an agent harness with planted structure.

   Both use artificial scarcity: a slow judge with about 2 calls per hour for the whole herd. An automatic scorecard measures:
   - breadth and composition;
   - how much of the available gain was captured;
   - repeated dead ends and conflict handling;
   - quota discipline and prediction error;
   - generalization.
2. **Controlled comparison.** A single agent, isolated agents and the herd each get the same quota and the same time. Steps:
   1. Run a pilot to measure noise.
   2. Pre-register the effect size and the number of runs.
   3. Score each arm's final deliverable on a sealed holdout with a one-sided paired test.
3. **Real problems,** only after the herd wins on the testbed.

## Architecture

VLAH is a standalone runtime. It reaches the outside world only through ports, so it can be connected to any cluster, agent harness or storage stack.

```
L3  Problem instance   charter, spec, eval sources, initial directions     one per problem
L2  Herd runtime       blackboard, claims and events, task queue,
                       eval service, integrator, routing, stop rules,
                       roles                                               what we build
L1  Ports              Executor, Harness, SignalSource, Store, Human, Model
L0  Adapters           local process / k8s / Ray; Claude Code / Agent SDK;
                       git / Redis / S3; CLI / Slack                       swappable
```

Every port has an in-memory adapter, so the whole herd can run a toy problem in one process in seconds. That run is the regression test for every change.

## Roadmap

1. **Testbed:** problem A runs end to end, and the scorecard is produced automatically.
2. **Minimal herd:** the smallest set of components that keeps the four invariants, running on problem A.
3. **Controlled comparison:** run on problem A first, then on problem B.
4. **Real problems,** together with the human alignment loop.
5. **Self-improvement:** the herd optimizes its own code, judged by the testbed.
