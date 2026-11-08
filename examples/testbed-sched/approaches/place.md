---
status: active
parent: null
---
# Machine placement

Investigate `place(job, free, state)`. Public observations say that some jobs'
running times depend on where they land and that a user's first job after a pause
can be slow to get going. These are symptoms, not explanations.

The public interface exposes free cores, racks, a job's rack preference and
recent/running users on a machine. Test whether using those observations changes
the service-backed fast results compared with the baseline's first fitting
machine. Never return a machine outside the eligible list or without enough
cores.

Use the map to distinguish average gains from regressions in a site or job
class. Keep evidence for and against each placement hypothesis and do not claim
that a local smoke simulation measures the service's machine behavior.
