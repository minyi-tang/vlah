# Random but valid decisions: should not look like any ingredient.
import random

RANDOM = random.Random(7)


def order(queue, state):
    ids = [j.id for j in queue]
    RANDOM.shuffle(ids)
    return ids


def place(job, free, state):
    fit = [m for m in free if m.free_cores >= job.cores]
    return RANDOM.choice(fit).id if fit else None
