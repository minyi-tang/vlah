"""The MCP tool server: herd tools over streamable HTTP, one bearer token per agent (D60, D68).

Any harness that speaks MCP can be a worker without code changes. The token in a
request identifies the agent; the tools are HerdTools methods called for it. Two
plain HTTP endpoints serve the harness's tool hook: POST /hook/check returns the
pending hard interrupt (or null), POST /hook/ack acknowledges it; any other id
is a 409.
"""

from dataclasses import dataclass

import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl, JsonValue
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from vlah.core.types.ids import ActorId
from vlah.ports.harness import HookConfig, ToolEndpoint
from vlah.tools.herd_tools import HerdTools, ToolReply

MCP_PATH = "/mcp"
HOOK_CHECK_PATH = "/hook/check"
HOOK_ACK_PATH = "/hook/ack"
BEARER_PREFIX = "Bearer "


@dataclass(frozen=True, kw_only=True)
class AgentTokenVerifier:
    tools: HerdTools

    async def verify_token(self, token: str) -> AccessToken | None:
        agent = self.tools.agent_for_token(token)
        if agent is None:
            return None
        else:
            return AccessToken(token=token, client_id=agent, scopes=["herd"])


def calling_agent() -> ActorId:
    access_token = get_access_token()
    if access_token is None:
        raise PermissionError("herd tools need the agent's bearer token")
    else:
        return ActorId(access_token.client_id)


def reply_json(reply: ToolReply) -> dict[str, JsonValue]:
    return reply.model_dump(mode="json")


class McpToolServer:
    def __init__(self, *, tools: HerdTools, host: str, port: int) -> None:
        self.tools = tools
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.server = MCPServer(
            name="vlah-herd",
            instructions=(
                "Herd tools. Write claims with repo_write, ask for a judge run with "
                "eval_request (predictions and a rationale are required), and read or "
                "search the shared blackboard. A reply may carry an interrupt: act on it first."
            ),
            token_verifier=AgentTokenVerifier(tools=tools),
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(self.base_url),
                resource_server_url=AnyHttpUrl(f"{self.base_url}{MCP_PATH}"),
                # Tokens are opaque per-agent secrets this server issued itself, so
                # there is no audience claim to check.
                validate_token_resource=False,
            ),
        )
        self.register_tools()
        self.register_hook_routes()

    def register_tools(self) -> None:
        tools = self.tools

        @self.server.tool(description="Read a file from the shared blackboard.")
        def repo_read(path: str) -> dict[str, JsonValue]:
            return reply_json(tools.repo_read(calling_agent(), path))

        @self.server.tool(
            description=(
                "Create new files under your own prefix (claims/<you>/...). Files are "
                "append-only; every claim needs at least one evidence pointer."
            )
        )
        def repo_write(files: dict[str, str]) -> dict[str, JsonValue]:
            return reply_json(tools.repo_write(calling_agent(), files))

        @self.server.tool(
            description="Search claims, digests and traces on the blackboard."
        )
        def search(query: str, limit: int = 10) -> dict[str, JsonValue]:
            return reply_json(tools.search(calling_agent(), query, limit))

        @self.server.tool(
            description=(
                "Ask for a judge run on a candidate directory in your sandbox. Give a "
                "predicted score for every judge level and a rationale. If similar work "
                "exists you get needs_confirmation and a confirm_token: call again with it."
            )
        )
        def eval_request(
            candidate_dir: str,
            predictions: dict[str, float],
            rationale: str,
            parents: list[str] | None = None,
            confirm_token: str | None = None,
            baseline: str | None = None,
        ) -> dict[str, JsonValue]:
            return reply_json(
                tools.eval_request(
                    calling_agent(),
                    candidate_dir,
                    predictions,
                    rationale,
                    tuple(parents or ()),
                    confirm_token,
                    baseline,
                )
            )

        @self.server.tool(
            description="Record something unexpected; no conclusion is needed."
        )
        def surprise(note: str, pointer: str, approach: str) -> dict[str, JsonValue]:
            return reply_json(tools.surprise(calling_agent(), note, pointer, approach))

    def register_hook_routes(self) -> None:
        tools = self.tools

        def agent_from(request: Request) -> ActorId | None:
            authorization = request.headers.get("authorization", "")
            if authorization.startswith(BEARER_PREFIX):
                return tools.agent_for_token(authorization.removeprefix(BEARER_PREFIX))
            else:
                return None

        @self.server.custom_route(HOOK_CHECK_PATH, methods=["POST"])
        async def hook_check(request: Request) -> JSONResponse:
            agent = agent_from(request)
            if agent is None:
                return JSONResponse({"error": "unknown agent token"}, status_code=401)
            else:
                interrupt = tools.hook_check(agent)
                return JSONResponse(
                    {
                        "interrupt": (
                            None
                            if interrupt is None
                            else interrupt.model_dump(mode="json")
                        )
                    }
                )

        @self.server.custom_route(HOOK_ACK_PATH, methods=["POST"])
        async def hook_ack(request: Request) -> JSONResponse:
            agent = agent_from(request)
            if agent is None:
                return JSONResponse({"error": "unknown agent token"}, status_code=401)
            else:
                try:
                    body: object = await request.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and isinstance(body.get("interrupt_id"), str):
                    interrupt_id = str(body["interrupt_id"])
                    if tools.hook_ack(agent, interrupt_id):
                        return JSONResponse({"acknowledged": True})
                    else:
                        return JSONResponse(
                            {"error": f"{interrupt_id} is not pending for {agent}"},
                            status_code=409,
                        )
                else:
                    return JSONResponse(
                        {"error": "interrupt_id is required"}, status_code=400
                    )

    def app(self) -> Starlette:
        return self.server.streamable_http_app(
            streamable_http_path=MCP_PATH, host=self.host
        )

    def endpoint_for(self, agent: ActorId) -> ToolEndpoint:
        return ToolEndpoint(
            url=f"{self.base_url}{MCP_PATH}",
            bearer_token=self.tools.token_by_agent[agent],
        )

    def hook_config_for(self, agent: ActorId) -> HookConfig:
        return HookConfig(
            interrupt_check_url=f"{self.base_url}{HOOK_CHECK_PATH}",
            interrupt_ack_url=f"{self.base_url}{HOOK_ACK_PATH}",
        )

    def claims_written(self, agent: ActorId, task_id: str) -> tuple[str, ...]:
        return self.tools.claims_written(agent, task_id)

    def serve(self) -> None:
        uvicorn.run(self.app(), host=self.host, port=self.port, log_level="warning")
