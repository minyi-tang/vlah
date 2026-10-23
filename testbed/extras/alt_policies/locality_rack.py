# Prefer the job's own rack; fall back to anything.
def place(job, free, state):
    fit = [m for m in free if m.free_cores >= job.cores]
    if not fit:
        return None
    own = [m for m in fit if job.rack_pref is not None and m.rack == job.rack_pref]
    return (own or fit)[0].id
