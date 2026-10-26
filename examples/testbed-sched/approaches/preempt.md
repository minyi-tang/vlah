---
status: active
parent: null
---
# Preemption

Investigate `preempt(running, state)`. The baseline never preempts. The public
interface permits killing selected running jobs on an arrival, but requeued
jobs restart from scratch rather than resuming.

Measure whether any reduction in queued waiting time justifies lost execution
and repeated requeues. Use elapsed time, requested resources and queue state
only as the interface exposes them. Check every site's p99 and record
counterexamples; preemption is not presumed beneficial.

Begin with a small, independently measurable change and compare it with the
non-preempting baseline through the fast service. Retain candidate lineage and
a score prediction for any official request, so interactions with other policy
hooks can be assessed during integration.
