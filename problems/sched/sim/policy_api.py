"""What a scheduling policy sees. Public: workers read this to write policy.py.

A policy is a Python file defining any of four hooks (missing hooks fall back to the
baseline) plus a params.yaml whose contents arrive as ``state.params``:

    order(queue, state)     -> list of job ids, a permutation of [j.id for j in queue]
    admit(job, state)       -> bool: may this job start now? (False = skip it this pass)
    place(job, free, state) -> machine id from ``free`` with free_cores >= job.cores, or None
    preempt(running, state) -> list of running job ids to kill and requeue (from scratch)

The simulator runs, per site, on every arrival, completion and 60 s tick:

    q = order(queue, state)
    for job in q:
        if not admit(job, state): continue
        m = place(job, free_machines, state)
        if m is None: break                      # head-of-line blocking
        start(job, m)
    if event == 'arrival':
        for v in preempt(running, state): requeue(v)

Each hook call is limited to 50 ms of CPU; over-budget, crashing or invalid calls use
the baseline answer for that call. More than 1% such calls voids the whole evaluation.
Module-level variables persist for the whole evaluation of one site.
"""

CLOCK = [0.0]  # current simulated time; Job.wait_so_far reads it


class Job:
    __slots__ = (
        "id",
        "site",
        "user",
        "submit_t",
        "cores",
        "est_dur",
        "rack_pref",
        "enqueue_t",
        "wait_base",
        "requeues",
    )

    def __init__(
        self,
        id,
        site,
        user,
        submit_t,
        cores,
        est_dur,
        rack_pref,
        enqueue_t,
        wait_base,
        requeues,
    ):
        self.id = id
        self.site = site
        self.user = user
        self.submit_t = submit_t
        self.cores = cores
        self.est_dur = (
            est_dur  # noisy estimate of the true duration (true duration is hidden)
        )
        self.rack_pref = rack_pref  # preferred rack (site 3 only), else None
        self.enqueue_t = enqueue_t  # when it (re)entered the queue
        self.wait_base = wait_base  # wait accumulated before the last requeue
        self.requeues = requeues

    @property
    def wait_so_far(self):
        return self.wait_base + (CLOCK[0] - self.enqueue_t)

    def __repr__(self):
        return f"Job({self.id}, user={self.user}, cores={self.cores}, est={self.est_dur:.0f})"


class Machine:
    __slots__ = ("id", "rack", "cores", "free_cores", "recent_users", "running_users")

    def __init__(self, id, rack, cores, free_cores, recent_users, running_users):
        self.id = id
        self.rack = rack
        self.cores = cores
        self.free_cores = free_cores
        self.recent_users = recent_users  # user -> last time a job of that user started or ended here (last hour)
        self.running_users = (
            running_users  # user -> number of that user's jobs running here now
        )

    def __repr__(self):
        return (
            f"Machine({self.id}, rack={self.rack}, free={self.free_cores}/{self.cores})"
        )


class Running:
    __slots__ = ("job_id", "machine_id", "user", "cores", "start_t", "est_dur")

    def __init__(self, job_id, machine_id, user, cores, start_t, est_dur):
        self.job_id = job_id
        self.machine_id = machine_id
        self.user = user
        self.cores = cores
        self.start_t = start_t
        self.est_dur = est_dur

    @property
    def elapsed(self):
        return CLOCK[0] - self.start_t


class State:
    """What a hook sees at one moment. ``site_profile`` is the site's plant only: ``id``,
    ``machines``, ``cores_per_machine`` and ``racks``. The workload and how machines behave
    under it are not described anywhere a policy can read; measure them with ``tb fast``.
    """

    __slots__ = (
        "now",
        "site",
        "queue",
        "machines",
        "running",
        "per_user_running",
        "per_user_running_cores",
        "busy_cores",
        "total_cores",
        "params",
        "site_profile",
        "event",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


# ------------------------------------------------------------- baseline (also policy/policy.py)


def baseline_order(queue, state):
    return [j.id for j in sorted(queue, key=lambda j: (j.enqueue_t, j.id))]


def baseline_admit(job, state):
    return True


def baseline_place(job, free, state):
    for m in sorted(free, key=lambda m: m.id):
        if m.free_cores >= job.cores:
            return m.id
    return None


def baseline_preempt(running, state):
    return []
