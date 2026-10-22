# Pure shortest-estimate-first (the trap).
def order(queue, state):
    return [j.id for j in sorted(queue, key=lambda j: (j.est_dur, j.id))]
