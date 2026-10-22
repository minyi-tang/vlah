# Shortest estimate first, but anything that waited over 90 minutes goes to the front.
def order(queue, state):
    old = sorted((j for j in queue if j.wait_so_far > 5400), key=lambda j: j.enqueue_t)
    young = sorted((j for j in queue if j.wait_so_far <= 5400), key=lambda j: j.est_dur)
    return [j.id for j in old + young]
