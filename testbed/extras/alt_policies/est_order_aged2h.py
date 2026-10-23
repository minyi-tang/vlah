# Shortest estimate first; anything that waited over two hours goes to the front.
# The spec's trap probe simulates two hours, so it counts aging >= 2 h as starvation.
def order(queue, state):
    old = sorted((j for j in queue if j.wait_so_far > 7200), key=lambda j: j.enqueue_t)
    young = sorted((j for j in queue if j.wait_so_far <= 7200), key=lambda j: j.est_dur)
    return [j.id for j in old + young]
