# Load-based admission: above 85% busy, only start nothing new.
def admit(job, state):
    return (state.busy_cores + job.cores) <= 0.85 * state.total_cores
