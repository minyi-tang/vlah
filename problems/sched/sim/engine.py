"""SimPy cluster simulator for one site. Public (workers can read it).

Durations progress in "work" units. All running jobs at a site share one progress rate
(1.0, or 1/slowdown while the site is overloaded), so completions are tracked on a single
virtual clock V and only the next completion needs rescheduling when the rate changes.
"""

from __future__ import annotations

import heapq
from pathlib import Path

import simpy
import yaml

from testbed.sandbox.proc import HostDied, HostProcess, HostTimeout

SIM_DIR = Path(__file__).resolve().parent
HOST_FILES = [SIM_DIR / "policy_host.py", SIM_DIR / "policy_api.py"]


class PolicyDriver:
    """Runs a candidate's policy.py in a sandboxed child process."""

    def __init__(
        self,
        candidate_dir: Path | None,
        site: int,
        site_profile: dict,
        timeout: bool,
        budget_s: float,
    ):
        extra = {}
        params = {}
        if candidate_dir is not None:
            candidate_dir = Path(candidate_dir)
            if (candidate_dir / "policy.py").exists():
                extra["policy.py"] = candidate_dir / "policy.py"
            if (candidate_dir / "params.yaml").exists():
                params = (
                    yaml.safe_load((candidate_dir / "params.yaml").read_text()) or {}
                )
        self.proc = HostProcess(HOST_FILES, "policy_host.py", extra)
        r = self.proc.request(
            {
                "op": "init",
                "site": site,
                "params": params,
                "site_profile": site_profile,
                "timeout": timeout,
                "budget_s": budget_s,
            },
            deadline_s=60,
        )
        self.load_error = r.get("load_error")
        self.wall_deadline = 30.0 if timeout else 600.0

    def request(self, msg):
        return self.proc.request(msg, deadline_s=self.wall_deadline)

    def close(self):
        self.proc.close()


class SimFailure(Exception):
    pass


class SiteSim:
    def __init__(
        self,
        profile: dict,
        common: dict,
        jobs: list[dict],
        driver: PolicyDriver,
        scale: float = 1.0,
        horizon: float | None = None,
        drain_cap: float | None = None,
        initial_running: list[tuple] | None = None,
    ):
        self.profile = profile
        self.site = profile["id"]
        self.common = common
        self.jobs = jobs
        self.driver = driver
        self.horizon = common["horizon_s"] if horizon is None else horizon
        self.drain_cap = common["drain_cap_s"] if drain_cap is None else drain_cap
        self.tick_s = common["tick_s"]
        self.max_calls = common.get("max_hook_calls_per_site")
        mech = profile.get("mechanisms", {}) or {}
        self.overload = mech.get("overload")
        self.affinity = mech.get("rack_affinity")
        self.startup = mech.get("startup")
        n = max(1, round(profile["machines"] * scale))
        racks = profile.get("racks", 1)
        per_rack = max(1, n // racks)
        cpm = profile["cores_per_machine"]
        self.machines = [
            {
                "id": i,
                "rack": min(racks - 1, i // per_rack),
                "cores": cpm,
                "free": cpm,
                "recent": {},
                "users": {},
            }
            for i in range(n)
        ]
        self.total_cores = n * cpm
        self.busy = 0
        self.env = simpy.Environment()
        self.queue: dict[int, dict] = {}
        self.running: dict[int, dict] = {}
        self.heap: list = []
        self.V = 0.0
        self.last_t = 0.0
        self.rate = 1.0
        self.wake = None
        self.pending_add: list = []
        self.pending_remove: list = []
        self.arrivals_done = False
        self.finished = self.env.event()
        self.calls = 0
        self.timeouts = 0
        self.errors: list[str] = []
        self.started_log: list[tuple] = []  # (t, job_id, machine_id) for probes
        self.stats = {"starts": 0, "cold_starts": 0, "off_rack": 0, "overloaded_s": 0.0}
        self._seq = 0
        self._first = True
        for j in jobs:
            j["wait"] = 0.0
            j["start_t"] = None
            j["requeues"] = 0
        for jid, mid, user, cores, remaining in initial_running or []:
            self._start_initial(jid, mid, user, cores, remaining)

    # ----------------------------------------------------------- virtual clock
    def _advance(self):
        now = self.env.now
        if self.rate < 1.0:
            self.stats["overloaded_s"] += now - self.last_t
        self.V += (now - self.last_t) * self.rate
        self.last_t = now

    def _update_rate(self):
        new = 1.0
        if self.overload and self.busy > self.overload["busy_frac"] * self.total_cores:
            new = 1.0 / self.overload["slowdown"]
        if new != self.rate:
            self._advance()
            self.rate = new
            self._poke()

    def _poke(self):
        if self.wake is not None and not self.wake.triggered:
            self.wake.succeed()

    # ----------------------------------------------------------- job lifecycle
    def _job_tuple(self, j):
        return (
            j["id"],
            j["site"],
            j["user"],
            j["submit_t"],
            j["cores"],
            j["est_dur"],
            j["rack_pref"],
            j["enqueue_t"],
            j["wait"],
            j["requeues"],
        )

    def _enqueue(self, j):
        j["enqueue_t"] = self.env.now
        self.queue[j["id"]] = j
        self.pending_add.append(self._job_tuple(j))

    def _start_initial(self, jid, mid, user, cores, remaining):
        m = self.machines[mid]
        m["free"] -= cores
        m["users"][user] = m["users"].get(user, 0) + 1
        m["recent"][user] = 0.0
        self.busy += cores
        v_end = self.V + remaining
        self.running[jid] = {
            "id": jid,
            "machine": mid,
            "user": user,
            "cores": cores,
            "start_t": 0.0,
            "est_dur": remaining,
            "v_end": v_end,
        }
        heapq.heappush(self.heap, (v_end, self._seq, jid))
        self._seq += 1

    def _start(self, j, mid):
        now = self.env.now
        m = self.machines[mid]
        self._advance()
        work = j["true_dur"]
        self.stats["starts"] += 1
        if self.affinity and j["rack_pref"] is not None and m["rack"] != j["rack_pref"]:
            work *= self.affinity["penalty"]
            self.stats["off_rack"] += 1
        if self.startup:
            warm = (
                m["users"].get(j["user"], 0) > 0
                or (now - m["recent"].get(j["user"], -1e18)) <= self.startup["window_s"]
            )
            if not warm:
                work += self.startup["overhead_s"]
                self.stats["cold_starts"] += 1
        j["wait"] += now - j["enqueue_t"]
        if j["start_t"] is None:
            j["start_t"] = now
        del self.queue[j["id"]]
        self.pending_remove.append(j["id"])
        m["free"] -= j["cores"]
        m["users"][j["user"]] = m["users"].get(j["user"], 0) + 1
        m["recent"][j["user"]] = now
        self.busy += j["cores"]
        v_end = self.V + work
        self.running[j["id"]] = {
            "id": j["id"],
            "machine": mid,
            "user": j["user"],
            "cores": j["cores"],
            "start_t": now,
            "est_dur": j["est_dur"],
            "v_end": v_end,
        }
        heapq.heappush(self.heap, (v_end, self._seq, j["id"]))
        self._seq += 1
        self.started_log.append((now, j["id"], mid))
        self._update_rate()
        self._poke()

    def _release(self, r):
        m = self.machines[r["machine"]]
        m["free"] += r["cores"]
        m["users"][r["user"]] -= 1
        if m["users"][r["user"]] == 0:
            del m["users"][r["user"]]
        m["recent"][r["user"]] = self.env.now
        self.busy -= r["cores"]

    def _preempt(self, jid):
        r = self.running.pop(jid)
        self._release(r)
        j = self.jobs_by_id[jid]
        j["requeues"] += 1
        self._enqueue(j)
        self._update_rate()

    def _complete(self, jid):
        r = self.running.pop(jid)
        self._release(r)
        self._update_rate()

    # ----------------------------------------------------------- scheduling pass
    def _snapshot(self):
        now = self.env.now
        horizon = now - 3600.0
        machines = []
        for m in self.machines:
            recent = {u: t for u, t in m["recent"].items() if t >= horizon}
            machines.append(
                (m["id"], m["rack"], m["cores"], m["free"], recent, dict(m["users"]))
            )
        running = [
            (r["id"], r["machine"], r["user"], r["cores"], r["start_t"], r["est_dur"])
            for r in self.running.values()
        ]
        return machines, running

    def schedule(self, event):
        machines, running = self._snapshot()
        msg = {
            "op": "pass",
            "now": self.env.now,
            "event": event,
            "add": self.pending_add,
            "remove": self.pending_remove,
            "machines": machines,
            "running": running,
        }
        self.pending_add, self.pending_remove = [], []
        if (
            self._first
        ):  # the child may hold a queue from an earlier simulation; replace it
            msg["reset_queue"] = [self._job_tuple(j) for j in self.queue.values()]
            msg["add"], msg["remove"] = [], []
            self._first = False
        try:
            resp = self.driver.request(msg)
        except (HostDied, HostTimeout) as e:
            raise SimFailure(f"policy process failed: {e}")
        self.calls += resp["calls"]
        self.timeouts += resp["timeouts"]
        if self.max_calls and self.calls > self.max_calls:
            raise SimFailure(
                f"policy made more than {self.max_calls} hook calls at one site (runaway queue)"
            )
        if resp.get("errors") and len(self.errors) < 5:
            self.errors.extend(resp["errors"][: 5 - len(self.errors)])
        for jid, mid in resp["starts"]:
            j = self.queue.get(jid)
            if (
                j is None
                or not (0 <= mid < len(self.machines))
                or self.machines[mid]["free"] < j["cores"]
            ):
                self.timeouts += 1  # parent-side validation (only reachable if the host was tampered with)
                continue
            self._start(j, mid)
        for jid in resp["preempts"]:
            if jid in self.running and jid in self.jobs_by_id:
                self._preempt(jid)
        if self.arrivals_done and not self.queue and not self.finished.triggered:
            self.finished.succeed()

    # ----------------------------------------------------------- processes
    def _arrivals(self):
        for j in self.jobs:
            dt = j["submit_t"] - self.env.now
            if dt > 0:
                yield self.env.timeout(dt)
            self._enqueue(j)
            self.schedule("arrival")
        self.arrivals_done = True
        if not self.queue and not self.finished.triggered:
            self.finished.succeed()

    def _ticks(self):
        while True:
            yield self.env.timeout(self.tick_s)
            self.schedule("tick")

    def _completions(self):
        while True:
            self._advance()
            while self.heap and (
                self.heap[0][2] not in self.running
                or self.running[self.heap[0][2]]["v_end"] != self.heap[0][0]
            ):
                heapq.heappop(self.heap)
            self.wake = self.env.event()
            if not self.heap:
                yield self.wake
                continue
            v_end = self.heap[0][0]
            dt = max(0.0, (v_end - self.V) / self.rate)
            t = self.env.timeout(dt)
            yield t | self.wake
            if not t.processed and not t.triggered:
                continue
            self._advance()
            done = False
            while self.heap and self.heap[0][0] <= self.V + 1e-6:
                _, _, jid = heapq.heappop(self.heap)
                r = self.running.get(jid)
                if r is not None and r["v_end"] <= self.V + 1e-6:
                    self._complete(jid)
                    done = True
            if done:
                self.schedule("completion")

    def run(self):
        self.jobs_by_id = {j["id"]: j for j in self.jobs}
        self.env.process(self._arrivals())
        self.env.process(self._ticks())
        self.env.process(self._completions())
        end = self.horizon + self.drain_cap
        self.env.run(until=self.finished | self.env.timeout(end))
        now = self.env.now
        for j in self.queue.values():  # censored at the drain cap
            j["wait"] += now - j["enqueue_t"]
        return self.jobs


def run_site(profile, common, jobs, candidate_dir, timeout=True, scale=1.0, **kw):
    """Simulate one site with the candidate policy. Returns a dict with waits and timing stats."""
    driver = PolicyDriver(
        candidate_dir, profile["id"], profile, timeout, common["hook_budget_s"]
    )
    try:
        if driver.load_error:
            return {"failed": f"policy import failed: {driver.load_error}"}
        sim = SiteSim(profile, common, jobs, driver, scale=scale, **kw)
        try:
            sim.run()
        except SimFailure as e:
            return {"failed": str(e), "calls": sim.calls, "timeouts": sim.timeouts}
        return {
            "jobs": sim.jobs,
            "calls": sim.calls,
            "timeouts": sim.timeouts,
            "errors": sim.errors,
            "stats": dict(sim.stats, overloaded_s=round(sim.stats["overloaded_s"], 1)),
        }
    finally:
        driver.close()
