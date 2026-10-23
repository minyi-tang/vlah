# Strict per-user fair share by running jobs, written another way.
def _share(state):
    users = set(state.per_user_running) | {j.user for j in state.queue}
    total = sum(state.per_user_running.values())
    return 1.5 * max(1, total) / max(1, len(users))


def order(queue, state):
    cap = _share(state)
    return [
        j.id
        for j in sorted(
            queue,
            key=lambda j: (state.per_user_running.get(j.user, 0) >= cap, j.enqueue_t),
        )
    ]


def admit(job, state):
    cap = _share(state)
    if state.per_user_running.get(job.user, 0) < cap:
        return True
    return not any(
        state.per_user_running.get(j.user, 0) < cap
        for j in state.queue
        if j.user != job.user
    )
