# Aggressive backfill written another way: never block the queue; skip jobs that do not fit.
def admit(job, state):
    return any(m.free_cores >= job.cores for m in state.machines)
