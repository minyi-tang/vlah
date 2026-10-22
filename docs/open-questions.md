# Open questions

Only @minyi-tang answers these. Agents must not guess them (v2.2 Appendix C: "the questions that must be asked first"). When an answer is given, write it in the Answer column with the date, and link the issue or PR that acts on it.

## Before the first real run (Appendix C)

| Question | Why it can't be guessed | Answer |
|---|---|---|
| Which problem does the PoC use? What are the judge's latency, daily quota and concurrency? Is there a holdout? | It sets the ladder, the quota and whether local proxies are needed. The system itself is validated on testbed problems A and B, not on a real problem. A real problem keeps artificial scarcity (latency ≥ 10 minutes, 2 runs per hour for the whole herd) even if its judge is fast | |
| Preregistration: the minimum effect worth detecting, N = 5 or 10 workers, the number of independent runs, and eval machine-hours per arm | A comparison that wasn't preregistered can't be judged (D42, D72) | |
| Which harness and model id (one model for every role)? What are the API's concurrency and rate limits? | They decide the hook mechanism and the checkpoint format, and how many agents can run at once | |
| Who writes `spec.md` and the first approaches? How many slots per approach? | There is no PI agent in the H1 phase | |
| Access to the cluster repo; which services exist (KV, object storage, git, image registry, database); worker pod size and maximum count | They decide which adapters to build (Wave 3) | |
| Each source's `trust` and `readable_by` (D45). Is the private store a separate repo or a bucket? How are worker credentials isolated? | They decide whether definitions live on the blackboard or in the private store (D63). If isolation fails, every anti-hacking measure fails | |

## Design decisions still pending

| Question | What depends on it | Answer |
|---|---|---|
| P11: enforce only the short boundary list and make the other rules prompt suggestions (one-page §4)? | Which mechanisms are enforced by default; the switches exist either way | |
| Start the minimal herd with zero LLM roles (no Librarian, Planner or Scout until the scorecard shows a need)? | Whether the Librarian comes before the H1 comparison (Wave 2) | |
| Put problem C (self-improvement) last? | The order after the real-problem phase | |
| Which code runs the H1 control arm: the herd runtime with the B1 preset, or the testbed's own isolated runner? | If it's the testbed runner, the arms differ in tools, prompts and egress, which D58 is meant to prevent | |

## Inconsistencies in the spec

The issues already take a position on each of these. See [spec/README.md](spec/README.md#known-inconsistencies-in-the-originals).

| Inconsistency | Position the issues take | Answer |
|---|---|---|
| D70 says 12 L0 assertions; §14.1 lists 20 | 20 | |
| §14.1 splits the data four ways; D72 says three | The herd uses three; the testbed's public split is the fourth | |
| §0.1 row 30 vs §14 on whether the weekly plan rolls over | The weeks are illustrative; progress follows the gates | |
| TB§11.3: is "total `ceil(96/N)`" per actor or per arm? | Open; the testbed agents decide | |
| H2: "2× or more" of what? | Open; H2 is Wave 3 | |
