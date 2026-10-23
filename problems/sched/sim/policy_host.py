"""Child-process host for a scheduling policy. Standalone (stdlib only): copied into a
temp dir next to policy_api.py and the candidate's policy.py, then run with ``python -I``.

It runs the per-pass scheduling loop locally (one IPC round trip per pass) and answers
with the decisions; the parent simulator re-validates every decision.
"""

import json
import os
import pickle
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _block_network():
    import socket

    class _NoNet(socket.socket):
        def __init__(self, *a, **k):
            raise PermissionError("network is disabled for policies")

    socket.socket = _NoNet
    socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(
        PermissionError("network disabled")
    )


_block_network()

import policy_api as api  # noqa: E402

_stdin = sys.stdin.buffer
_stdout = sys.stdout.buffer
sys.stdout = sys.stderr  # policy prints must not corrupt the protocol


def _recv():
    hdr = _stdin.read(4)
    if len(hdr) < 4:
        return None
    (n,) = struct.unpack(">I", hdr)
    return pickle.loads(_stdin.read(n))


def _send(obj):
    data = json.dumps(obj).encode()
    _stdout.write(struct.pack(">I", len(data)) + data)
    _stdout.flush()


class Host:
    def __init__(self, msg):
        self.site = msg["site"]
        self.params = msg.get("params") or {}
        self.profile = msg.get("site_profile") or {}
        self.timeout = msg.get("timeout", True)
        self.budget = msg.get("budget_s", 0.05)
        self.hooks = {
            "order": api.baseline_order,
            "admit": api.baseline_admit,
            "place": api.baseline_place,
            "preempt": api.baseline_preempt,
        }
        self.fallback = dict(self.hooks)
        self.load_error = None
        path = os.path.join(HERE, "policy.py")
        if os.path.exists(path):
            import importlib.util

            try:
                spec = importlib.util.spec_from_file_location("policy", path)
                mod = importlib.util.module_from_spec(spec)
                sys.modules["policy"] = mod
                spec.loader.exec_module(mod)
                for name in self.hooks:
                    fn = getattr(mod, name, None)
                    if callable(fn):
                        self.hooks[name] = fn
            except Exception as e:  # import failure voids the evaluation
                self.load_error = f"{type(e).__name__}: {e}"
        self.queue = {}  # id -> api.Job, insertion order = enqueue order
        self.calls = 0
        self.timeouts = 0
        self.errors = []

    # ------------------------------------------------------------------ helpers
    def _timed(self, name, *args):
        """Call a hook; return (value, ok). ok=False means: use the baseline answer."""
        self.calls += 1
        t0 = time.process_time()
        try:
            v = self.hooks[name](*args)
        except Exception as e:
            self.timeouts += 1
            if len(self.errors) < 5:
                self.errors.append(f"{name}: {type(e).__name__}: {e}")
            return None, False
        if self.timeout and time.process_time() - t0 > self.budget:
            self.timeouts += 1
            return None, False
        return v, True

    def _state(self, now, event, machines, running):
        api.CLOCK[0] = now
        mlist = [api.Machine(m[0], m[1], m[2], m[3], m[4], m[5]) for m in machines]
        rlist = [api.Running(*r) for r in running]
        pur, purc = {}, {}
        for r in rlist:
            pur[r.user] = pur.get(r.user, 0) + 1
            purc[r.user] = purc.get(r.user, 0) + r.cores
        total = sum(m.cores for m in mlist)
        busy = total - sum(m.free_cores for m in mlist)
        st = api.State(
            now=now,
            site=self.site,
            queue=list(self.queue.values()),
            machines=mlist,
            running=rlist,
            per_user_running=pur,
            per_user_running_cores=purc,
            busy_cores=busy,
            total_cores=total,
            params=self.params,
            site_profile=self.profile,
            event=event,
        )
        return st

    def _sync(self, msg):
        if msg.get("reset_queue") is not None:
            self.queue = {t[0]: api.Job(*t) for t in msg["reset_queue"]}
        for jid in msg.get("remove", ()):
            self.queue.pop(jid, None)
        for t in msg.get("add", ()):
            self.queue[t[0]] = api.Job(*t)

    # ------------------------------------------------------------------ ops
    def do_pass(self, msg):
        self._sync(msg)
        now, event = msg["now"], msg["event"]
        st = self._state(now, event, msg["machines"], msg["running"])
        jobs = {j.id: j for j in st.queue}
        q, ok = self._timed("order", list(st.queue), st)
        if ok:
            try:
                q = list(q)
                if len(q) != len(jobs) or set(q) != set(jobs):
                    raise ValueError
            except Exception:
                ok = False
                self.timeouts += 1
        if not ok:
            q = api.baseline_order(st.queue, st)
        mach = {m.id: m for m in st.machines}
        starts = []
        for jid in q:
            job = jobs[jid]
            a, ok = self._timed("admit", job, st)
            if ok and not a:
                continue
            free = [m for m in st.machines if m.free_cores > 0]
            mid, ok = self._timed("place", job, free, st)
            if ok and mid is not None:
                m = mach.get(mid)
                if m is None or m.free_cores < job.cores or m.free_cores <= 0:
                    ok = False
                    self.timeouts += 1
            if not ok:
                mid = api.baseline_place(job, free, st)
            if mid is None:
                break
            m = mach[mid]
            m.free_cores -= job.cores
            m.recent_users[job.user] = now
            m.running_users[job.user] = m.running_users.get(job.user, 0) + 1
            st.busy_cores += job.cores
            st.running.append(
                api.Running(job.id, mid, job.user, job.cores, now, job.est_dur)
            )
            st.per_user_running[job.user] = st.per_user_running.get(job.user, 0) + 1
            st.per_user_running_cores[job.user] = (
                st.per_user_running_cores.get(job.user, 0) + job.cores
            )
            try:
                st.queue.remove(job)
            except ValueError:
                pass
            starts.append([jid, mid])
        preempts = []
        if event == "arrival":
            v, ok = self._timed("preempt", list(st.running), st)
            if ok and v:
                running_ids = {r.job_id for r in st.running}
                try:
                    preempts = [x for x in v if x in running_ids]
                    if len(preempts) != len(list(v)):
                        self.timeouts += 1
                except Exception:
                    preempts = []
                    self.timeouts += 1
        return {"starts": starts, "preempts": preempts}

    def do_call(self, msg):
        """Single hook call on a constructed state (probes)."""
        self._sync(msg)
        st = self._state(
            msg["now"], msg.get("event", "tick"), msg["machines"], msg["running"]
        )
        hook = msg["hook"]
        jobs = {j.id: j for j in st.queue}
        if hook == "order":
            args = (list(st.queue), st)
        elif hook == "admit":
            args = (jobs[msg["job_id"]], st)
        elif hook == "place":
            free = [m for m in st.machines if m.free_cores > 0]
            args = (jobs[msg["job_id"]], free, st)
        else:
            args = (list(st.running), st)
        v, ok = self._timed(hook, *args)
        if not ok:
            v = self.fallback[hook](*args)
        try:
            json.dumps(v)
        except TypeError:
            v = list(v) if v is not None else None
        return {"value": v, "ok": ok}


def main():
    host = None
    while True:
        msg = _recv()
        if msg is None or msg.get("op") == "exit":
            if msg is not None:
                _send({"bye": True})
            return
        op = msg["op"]
        if op == "init":
            host = Host(msg)
            _send({"load_error": host.load_error})
            continue
        before_calls, before_to = host.calls, host.timeouts
        if op == "pass":
            out = host.do_pass(msg)
        else:
            out = host.do_call(msg)
        out["calls"] = host.calls - before_calls
        out["timeouts"] = host.timeouts - before_to
        out["errors"] = host.errors[:3]
        _send(out)


if __name__ == "__main__":
    main()
