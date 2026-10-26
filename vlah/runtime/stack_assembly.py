"""The composition root for a run on the stack (#129): the local assembly with the
services a distributed herd has.

Workers run in containers (the Docker executor, #128), the run's state lives in
Redis (the compose service vlah stack up starts), and every worker clones its view
of the blackboard over HTTP with its own token (the git HTTP service, D67, D68)
instead of by path. herd.yaml may say any of this itself; the stack only fills in
what it leaves out, so the same problem folder runs on one machine or on the stack
without edits. Everything else is LocalAssembly's.
"""

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

from vlah.adapters.store.git.store import GitVersionedStore
from vlah.adapters.store.git_http.server import (
    GitHttpService,
    ServedGitHttp,
    ViewAccess,
    clone_environment,
)
from vlah.core.blackboard.api import Blackboard
from vlah.core.blackboard.blackboard import MirrorBlackboard, StoreBlackboard
from vlah.core.problem.api import AdapterChoice, HerdConfig
from vlah.core.types.ids import ActorId
from vlah.ports.errors import NotSupportedError
from vlah.runtime.assembly import RunComponents, RunContext
from vlah.runtime.local_assembly import (
    DOCKER_EXECUTOR,
    KEY_VALUE_STORE,
    LocalAssembly,
    free_port,
)

STACK_REDIS_URL = "redis://127.0.0.1:6379/0"
REDIS_STORE = "redis"
# The clones are made by the executor's sidecar on this machine, so the service
# binds and advertises loopback; a pod executor (#131) advertises a Service name.
GIT_HTTP_HOST = "127.0.0.1"


def stack_herd_config(herd_config: HerdConfig, redis_url: str) -> HerdConfig:
    """herd.yaml with the stack's defaults where it is silent: Docker workers and Redis."""
    executor = herd_config.executor
    if executor.kind != DOCKER_EXECUTOR:
        executor = AdapterChoice(kind=DOCKER_EXECUTOR, settings=executor.settings)
    else:
        pass
    stores = dict(herd_config.stores)
    if KEY_VALUE_STORE not in stores:
        stores[KEY_VALUE_STORE] = AdapterChoice(
            kind=REDIS_STORE, settings={"url": redis_url}
        )
    else:
        pass
    return herd_config.model_copy(update={"executor": executor, "stores": stores})


def repository_of(view: Blackboard) -> Path:
    """The bare repository behind a worker's view; only git-backed views are served."""
    if isinstance(view, (StoreBlackboard, MirrorBlackboard)) and isinstance(
        view.store, GitVersionedStore
    ):
        return view.store.repository_path
    else:
        raise NotSupportedError(
            f"only a git-backed blackboard can be served over HTTP, not "
            f"{type(view).__name__}"
        )


@dataclass(kw_only=True)
class GitViewPublisher:
    """runtime.isolation.ViewPublisher over one GitHttpService."""

    service: GitHttpService
    access_by_agent: dict[ActorId, ViewAccess] = field(
        default_factory=dict[ActorId, ViewAccess]
    )

    def publish(self, run_id: str, agent: ActorId, view: Blackboard) -> str:
        access = self.service.register_view(run_id, agent, repository_of(view))
        self.access_by_agent[agent] = access
        return access.url

    def settings(self, agent: ActorId) -> dict[str, str]:
        access = self.access_by_agent.get(agent)
        return clone_environment(access.token) if access is not None else {}


@dataclass(kw_only=True)
class StackAssembly(LocalAssembly):
    redis_url: str = STACK_REDIS_URL
    served_git: list[ServedGitHttp] = field(default_factory=list[ServedGitHttp])

    def build(self, context: RunContext) -> RunComponents:
        herd_config = stack_herd_config(context.problem.herd_config, self.redis_url)
        stack_context = dataclasses.replace(
            context,
            problem=dataclasses.replace(context.problem, herd_config=herd_config),
        )
        served = ServedGitHttp.start(
            GitHttpService(host=GIT_HTTP_HOST, port=free_port())
        )
        self.served_git.append(served)
        publisher = GitViewPublisher(service=served.service)
        self.view_settings = publisher.settings
        components = super().build(stack_context)
        return dataclasses.replace(components, view_publisher=publisher)

    def close(self) -> None:
        super().close()
        for served in self.served_git:
            served.close()
        self.served_git.clear()
