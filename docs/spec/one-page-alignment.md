# Research Herd: One-page Alignment

2026-10-19 · Minyi · Reading this page is enough to start work. *Research Herd Design* (file `research-herd-design-v2.2.md`) and *Herd Testbed* (`herd-testbed.md`) are reference books: look things up by number; you don't need to read them through.

*English translation of the Chinese original (`zh/one-page-alignment.md`).*

## 1. What we are building

There is an external judge that is slow, scarce and cannot be fooled. There is a space of approaches that nobody can list in full in advance. There is a herd of agents and a limited amount of wall-clock time. Goal: with the same wall-clock time and the same number of judge calls, deliver a better result than any single agent could produce alone.

The control group is not "one agent". It is "a herd of agents with unlimited tokens that don't talk to each other". What has to win is the structure, not the compute. Right now "it can win" is still a hypothesis; the testbed exists to test it.

## 2. What cannot change

Four invariants. Every version of the herd must satisfy them:

1. No loss: everything learned is kept, and others can get it when they need it.
2. No repetition: nobody unknowingly re-walks a path that has already been walked.
3. No wasted judging: every scarce eval lands where it can change the next decision.
4. Combinable: the deliverable is the combination of all valid attempts, not the best single one.

Constraints set by the environment, independent of how strong the model is: the judge is scarce; each agent's context is limited; human attention is 30 minutes a day; workers cannot see the eval definitions or the hidden data; egress goes only through the allowlist.

What the scorecard measures does not change: breadth, composition, share of available gain captured, dead-end re-hits, conflict handling, quota discipline, prediction error, tilt, generalization. How we achieve these can change.

## 3. What can change

Every D-numbered decision in the design doc and every number marked "default". The roles (PI, Planner, Critic, Librarian, Scout), the blackboard layout, the routing rules and the allocation formula are scaffolding that today's models need, not definitions. When a stronger model arrives, cut first and add later, and use the testbed to check that the cuts were right.

## 4. Three working principles

**Models and tokens are not constraints (P10).** Don't add mechanisms to save tokens. Don't use weaker models to save money. Every role uses the same strongest model. Anything that spends only model calls and no evals can be done redundantly.

**Govern the boundaries, not the paths (P11, pending your approval).** The harness enforces only these: the eval quota and the queue; dedup by hash; append-only records; the merger (the same copy in every arm, with the same configuration), plus its pre-merge gates and paired remeasure; invisible eval definitions; the egress allowlist; the lease as a declaration that "I am working on this"; submissions carry predictions. How agents explore, what they read, when they read it, and what they do after a failure are all only suggestions. They are written in the prompt and can be ignored. Add rules in response to failures, not to imagination: only when a scorecard item clearly loses to "isolated" do you add one mandatory rule for that item. Add one at a time, and rerun after each one.

**Self-improvement is step three, not step one.** The herd itself is a problem pack (problem C): candidate = a change to the herd code; fast tier = unit tests and scripted self-tests; slow tier = a testbed run on problems A and B using that version of the herd. The judge is out of its hands. A human takes a look before anything merges into trunk. After switching to a stronger model, the first task is to cut what is no longer needed.

## 5. Build order

Every step has a "done" criterion. Don't move to the next step until it is met.

1. **Testbed.** *Herd Testbed* §11 can be handed directly to a coding agent. Done: the merger (pure code shared by all three arms) is built and passes scripted tests; the "lone wolf" and "isolated" arms run end to end on problem A; the scorecard is produced automatically; and the three scripted self-tests (perfect herd, lone wolf, quota hog) give the expected results.
2. **Minimal herd (pending your approval).** Build only what is on the mandatory list in §4, plus one shared blackboard and N workers with advisory prompts. Use the testbed's merger as is; the only change is that it pushes "baseline advanced" to the workers. No Planner, Librarian or Scout. First see which scorecard item drops without them. Done: the herd arm runs end to end on problem A.
3. **Three-arm comparison.** Run the three arms on problem A with the same quota and the same time, following the verdict protocol (design doc D72: pilot first, then set the number of runs). Done: the herd beats "isolated" on three items: composition, dead-end re-hits and quota tilt. If it loses, go back to step 2 and add one rule based on the failure.
4. **Problem B**, same as above.
5. **Real project.** Only at this point do we need the PI and the human alignment protocol (design doc §10).
6. **Problem C**: let the herd change itself.

## 6. Three things still waiting for your call

1. P11's split between mandatory and suggested (§4). Once you approve it, I will downgrade the few items in the design doc that cross the line to suggestions.
2. The minimal herd starts with zero roles (§5 step 2). This differs from what the design doc says now: "PI and Librarian exist from week one".
3. Problem C goes in step six; the first five steps don't touch it.

## 7. How to use the reference books

*Research Herd Design*: §0.1 is the trail of why we decided things the way we did; look it up by round. §9 is how evals are used. §14 is the validation plan. The decision register at the end is indexed by D number; each entry lists its failure signal and what to change. *Herd Testbed*: §6 is the scorecard; §11 is the implementer spec.
