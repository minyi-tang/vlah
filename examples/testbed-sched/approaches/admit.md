---
status: active
parent: null
---
# Admission

Investigate `admit(job, state)`: the baseline admits every job, while the public
interface permits skipping a job for the current pass. Public observations
include time-of-day waves when jobs and their neighbors take longer.

Use queue, occupancy and per-user observations to test when deferring a job
helps waiting time and when it merely moves the wait elsewhere. Measure the
per-class and per-site consequences, including jobs repeatedly deferred.
Do not infer a site's machine behavior from an observed symptom.

Keep changes separate from ordering where possible; if an interaction is
necessary, describe and measure both changes. Use service-backed fast evidence,
then a predicted slow-tier result, before claiming the admission rule helps.
