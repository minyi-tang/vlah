"""FastAPI wrapper for the slow-eval core (spec 11.3). Every request except POST /runs must
carry ``X-TB-Run``; requests that act for someone carry ``X-TB-Actor``."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from testbed.slow_eval.core import SYSTEM_ACTORS, ApiError, SlowEval


def create_app(
    data_dir: Path,
    clock=None,
    inline: bool = False,
    core: SlowEval | None = None,
    publisher: bool = True,
) -> FastAPI:
    se = core or SlowEval(data_dir, clock=clock, inline=inline)
    app = FastAPI(title="herd-testbed slow eval")
    app.state.core = se

    if publisher and not inline:

        def loop():
            while True:
                try:
                    se.publish_due()
                except Exception:
                    pass
                time.sleep(0.5)

        threading.Thread(target=loop, daemon=True).start()

    @app.exception_handler(ApiError)
    def _api_error(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.code, content=exc.body)

    def need_run(x_tb_run):
        if not x_tb_run:
            raise ApiError(400, {"reason": "missing X-TB-Run header"})
        se.run_row(x_tb_run)
        return x_tb_run

    def trusted(key):
        return bool(key) and key == se.system_key

    def need_system(key):
        # Not a user system (the spec rules that out): one per-service key that the runner and the
        # integrator hold and the agents' sandbox cannot read. It keeps workers from spending
        # someone else's quota, writing run-level events, or reading the reference score.
        if not trusted(key):
            raise ApiError(403, {"reason": "testbed only"})

    def actor_ok(run_id, actor, key, token):
        if actor in SYSTEM_ACTORS and not trusted(key):
            raise ApiError(
                403, {"reason": f"actor {actor!r} is reserved for the testbed"}
            )
        se.check_actor(run_id, actor, token, trusted(key))
        return actor

    @app.post("/runs")
    def runs(body: dict = Body(...), x_tb_key: str | None = Header(None)):
        need_system(x_tb_key)
        return se.create_run(body)

    @app.post("/submit")
    def submit(
        body: dict = Body(...),
        x_tb_run: str | None = Header(None),
        x_tb_key: str | None = Header(None),
        x_tb_token: str | None = Header(None),
    ):
        return se.submit(
            need_run(x_tb_run), body, trusted=trusted(x_tb_key), token=x_tb_token
        )

    @app.post("/fast")
    def fast(
        body: dict = Body(...),
        x_tb_run: str | None = Header(None),
        x_tb_actor: str | None = Header(None),
        x_tb_key: str | None = Header(None),
        x_tb_token: str | None = Header(None),
    ):
        run = need_run(x_tb_run)
        return se.fast(
            run, body, actor_ok(run, x_tb_actor or "anon", x_tb_key, x_tb_token)
        )

    @app.get("/status/{ticket}")
    def status(
        ticket: str,
        x_tb_run: str | None = Header(None),
        x_tb_actor: str | None = Header(None),
        x_tb_key: str | None = Header(None),
        x_tb_token: str | None = Header(None),
    ):
        run = need_run(x_tb_run)
        return se.status(run, ticket, actor_ok(run, x_tb_actor, x_tb_key, x_tb_token))

    @app.get("/history")
    def history(
        x_tb_run: str | None = Header(None),
        x_tb_actor: str | None = Header(None),
        x_tb_key: str | None = Header(None),
        x_tb_token: str | None = Header(None),
    ):
        run = need_run(x_tb_run)
        return se.history(run, actor_ok(run, x_tb_actor, x_tb_key, x_tb_token))

    @app.get("/meta")
    def meta(x_tb_run: str | None = Header(None)):
        return se.meta(need_run(x_tb_run))

    @app.post("/events")
    def post_events(
        body: dict = Body(...),
        x_tb_run: str | None = Header(None),
        x_tb_actor: str | None = Header(None),
        x_tb_key: str | None = Header(None),
        x_tb_token: str | None = Header(None),
    ):
        run = need_run(x_tb_run)
        if not trusted(x_tb_key):
            se.check_actor(run, x_tb_actor, x_tb_token, False)
        return se.post_events(
            run, body.get("events") or [], x_tb_actor, trusted=trusted(x_tb_key)
        )

    @app.get("/events")
    def get_events(
        since: int = 0,
        x_tb_run: str | None = Header(None),
        x_tb_key: str | None = Header(None),
    ):
        need_system(x_tb_key)
        return se.events(need_run(x_tb_run), since)

    @app.get("/candidate/{h}")
    def candidate(
        h: str, x_tb_run: str | None = Header(None), x_tb_key: str | None = Header(None)
    ):
        need_system(x_tb_key)
        return se.candidate(need_run(x_tb_run), h)

    @app.post("/final")
    def final(
        body: dict = Body(...),
        x_tb_run: str | None = Header(None),
        x_tb_actor: str | None = Header(None),
        x_tb_key: str | None = Header(None),
        x_tb_token: str | None = Header(None),
    ):
        run = need_run(x_tb_run)
        return se.final(
            run, body, actor_ok(run, x_tb_actor or "anon", x_tb_key, x_tb_token)
        )

    @app.post("/final_check")
    def final_check(
        body: dict = Body(...),
        x_tb_run: str | None = Header(None),
        x_tb_key: str | None = Header(None),
    ):
        need_system(x_tb_key)
        return se.final_check(need_run(x_tb_run), body)

    @app.get("/calibration")
    def calibration(
        x_tb_run: str | None = Header(None), x_tb_key: str | None = Header(None)
    ):
        need_system(x_tb_key)
        return se.calibration(need_run(x_tb_run))

    return app
