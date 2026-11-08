---
status: active
parent: null
---
# Queue ordering

Investigate `order(queue, state)`, starting from the baseline's enqueue-time
order. The public observations include quick jobs waiting behind other work;
the map separates waiting time by size, estimated duration and user.

Compare ordering rules on the same public seeds and inspect every site's p99,
not only the average. Estimates are noisy; a duration-based rule is a hypothesis,
not evidence that a job is actually short. Account for long-waiting jobs and
verify that the output remains a permutation of the queue.

Keep the first candidate focused on ordering so its effect can be distinguished
from admission, placement and preemption changes. Predict its official score
before requesting a slow run and record the result even when it does not help.
