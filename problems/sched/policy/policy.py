"""Production scheduling policy (baseline): FIFO, admit everything, first-fit, never preempt.

See sim/policy_api.py for the hook contract and the State fields.
"""


def order(queue, state):
    # FIFO by the time each job entered the queue
    return [j.id for j in sorted(queue, key=lambda j: (j.enqueue_t, j.id))]


def admit(job, state):
    return True


def place(job, free, state):
    # first machine (by id) with enough free cores
    for m in sorted(free, key=lambda m: m.id):
        if m.free_cores >= job.cores:
            return m.id
    return None


def preempt(running, state):
    return []
