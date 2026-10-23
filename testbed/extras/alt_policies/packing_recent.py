# Put a job where its user ran most recently.
def place(job, free, state):
    fit = [m for m in free if m.free_cores >= job.cores]
    if not fit:
        return None

    def last(m):
        t = m.recent_users.get(job.user)
        return -1e18 if t is None else t

    best = max(fit, key=last)
    return best.id
