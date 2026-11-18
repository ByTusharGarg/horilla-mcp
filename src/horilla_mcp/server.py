"""MCP server exposing Horilla HRMS (employees, leave, attendance) as tools.

Configure with environment variables:
  HORILLA_URL       e.g. http://localhost:8000
  HORILLA_USERNAME  Horilla login (the server acts as this user)
  HORILLA_PASSWORD
or, for an app that already signed the user in (e.g. the HR Assistant), instead of the
username and password:
  HORILLA_ACCESS_TOKEN, HORILLA_REFRESH_TOKEN  the tokens from Horilla's /api/auth/login/
  HORILLA_EMPLOYEE  optional: the login response's "employee" object, as JSON

Delegated mode (`--http --delegated`, or HORILLA_MCP_DELEGATED=1) serves many users from one
stateless process: it never logs in itself, and each tool call carries the calling user's
Horilla access token in its `_meta` (see DELEGATED_TOKEN below). Any copy of the server can
answer any call, so it scales horizontally behind a plain load balancer. A token in `_meta`
is also honoured in the other modes, for that one call.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import HorillaClient, HorillaError

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DECIDE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

Breakdown = Literal["full_day", "first_half", "second_half"]


def _load_dotenv() -> None:
    """Read KEY=VALUE lines from a .env in the project root; real env vars take precedence."""
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# `_meta` keys a caller uses to run one tool call as a specific Horilla user.
DELEGATED_TOKEN = "horilla-mcp/access-token"  # that user's Horilla access token (JWT)
DELEGATED_EMPLOYEE = "horilla-mcp/employee"  # optional: their login "employee" object


def delegated_mode() -> bool:
    return os.environ.get("HORILLA_MCP_DELEGATED", "").lower() in ("1", "true", "yes")


def make_client() -> HorillaClient:
    """Build the Horilla client from the environment. Tests replace this function."""
    url = os.environ.get("HORILLA_URL") or None
    if os.environ.get("HORILLA_ACCESS_TOKEN") or os.environ.get("HORILLA_REFRESH_TOKEN"):
        # Token session: never fall back to the .env user, so a signed-in user's server
        # can't silently act as someone else.
        me = os.environ.get("HORILLA_EMPLOYEE")
        return HorillaClient(
            url or "http://localhost:8000",
            access_token=os.environ.get("HORILLA_ACCESS_TOKEN") or None,
            refresh_token=os.environ.get("HORILLA_REFRESH_TOKEN") or None,
            me=json.loads(me) if me else None,
        )
    _load_dotenv()
    try:
        url = os.environ.get("HORILLA_URL", "http://localhost:8000")
        user = os.environ["HORILLA_USERNAME"]
        password = os.environ["HORILLA_PASSWORD"]
    except KeyError as e:
        raise RuntimeError(f"Missing environment variable {e.args[0]}") from None
    return HorillaClient(url, user, password)


def make_delegated_client(access_token: str, me: dict[str, Any] | None) -> HorillaClient:
    """A client for one call made on behalf of another user. Tests replace this function."""
    _load_dotenv()
    return HorillaClient(os.environ.get("HORILLA_URL", "http://localhost:8000"),
                         access_token=access_token, me=me)


@asynccontextmanager
async def lifespan(_: MCPServer) -> AsyncIterator[HorillaClient | None]:
    if delegated_mode():
        yield None  # no user of its own: every call must carry one
        return
    client = make_client()
    try:
        yield client
    finally:
        await client.aclose()


@asynccontextmanager
async def _horilla(ctx: Context) -> AsyncIterator[HorillaClient]:
    """The Horilla client for this call: the caller's user from `_meta` if given, else the
    server's own login."""
    meta = ctx.request_context.meta or {}
    token = meta.get(DELEGATED_TOKEN)
    if token:
        employee = meta.get(DELEGATED_EMPLOYEE)
        client = make_delegated_client(str(token), employee if isinstance(employee, dict) else None)
        try:
            yield client
        finally:
            await client.aclose()
        return
    client = ctx.request_context.lifespan_context
    if client is None:
        raise ToolError(f"No Horilla user for this call: this server runs in delegated mode, so send "
                        f"the user's Horilla access token in _meta[{DELEGATED_TOKEN!r}].")
    yield client


mcp = MCPServer(
    "horilla-hrms",
    instructions=(
        "Tools for the Horilla HRMS: look up employees, check leave balances, apply for leave, "
        "approve or reject leave requests, view holidays, and clock in or out. All actions run as "
        "one Horilla user (the server's own login, or the caller's user in delegated mode), with "
        "that user's permissions. Dates use YYYY-MM-DD. Before "
        "applying for leave, call list_leave_types or get_my_leave_balance to find the leave_type_id. "
        "Confirm with the user before approving, rejecting or applying for leave."
    ),
    lifespan=lifespan,
)


async def _call(ctx: Context, method: str, path: str, *, params=None, json=None) -> Any:
    async with _horilla(ctx) as client:
        try:
            return await client.request(method, path, params=params, json=json)
        except HorillaError as e:
            raise ToolError(str(e)) from None


def _results(data: Any) -> Any:
    """Unwrap DRF pagination, keeping the count so the model knows when to page."""
    if isinstance(data, dict) and "results" in data:
        out = {"count": data.get("count"), "results": data["results"]}
        if data.get("next"):
            out["has_more"] = True
        return out
    return data


def _check_date(name: str, value: str | None) -> None:
    if value is None:
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{name} must be a date in YYYY-MM-DD format, got {value!r}") from None


# ---- profile & employees -------------------------------------------------


@mcp.tool(annotations=READ)
async def get_my_profile(ctx: Context) -> Any:
    """Return the employee record of the Horilla user this server is logged in as."""
    async with _horilla(ctx) as client:
        if client.me is None:
            try:
                await client.request("GET", "leave/available-leave/")  # a password login fills `me`
            except HorillaError as e:
                raise ToolError(str(e)) from None
        if client.me is None:
            raise ToolError("Profile not available: pass the login's employee object in "
                            f"_meta[{DELEGATED_EMPLOYEE!r}].")
        return client.me


@mcp.tool(annotations=READ)
async def search_employees(ctx: Context, search: str | None = None, page: int = 1) -> Any:
    """Search employees by name. Leave `search` empty to list everyone you can see.
    Returns ids, names, job details and contact info, 20 per page."""
    return _results(await _call(ctx, "GET", "employee/list/employees/", params={"search": search, "page": page}))


@mcp.tool(annotations=READ)
async def get_employee(ctx: Context, employee_id: int) -> Any:
    """Get one employee's full record by id (from search_employees)."""
    return await _call(ctx, "GET", f"employee/employees/{employee_id}/")


# ---- leave ----------------------------------------------------------------


@mcp.tool(annotations=READ)
async def list_leave_types(ctx: Context) -> Any:
    """List the leave types defined in Horilla (id, name, payment, limits)."""
    return _results(await _call(ctx, "GET", "leave/leave-type/"))


@mcp.tool(annotations=READ)
async def get_my_leave_balance(ctx: Context) -> Any:
    """Leave types assigned to the current user, with available and carry-forward days."""
    return _results(await _call(ctx, "GET", "leave/available-leave/"))


@mcp.tool(annotations=READ)
async def list_my_leave_requests(ctx: Context, page: int = 1) -> Any:
    """The current user's own leave requests, newest first, with their status."""
    return _results(await _call(ctx, "GET", "leave/user-request/", params={"page": page}))


@mcp.tool(annotations=WRITE)
async def apply_leave(
    ctx: Context,
    leave_type_id: int,
    start_date: str,
    end_date: str | None = None,
    description: str = "",
    start_date_breakdown: Breakdown = "full_day",
    end_date_breakdown: Breakdown = "full_day",
) -> Any:
    """Apply for leave as the current user. Dates are YYYY-MM-DD; end_date defaults to
    start_date. Use first_half/second_half breakdowns for half days. The request goes to
    the user's reporting manager for approval."""
    _check_date("start_date", start_date)
    _check_date("end_date", end_date)
    body = {
        "leave_type_id": leave_type_id,
        "start_date": start_date,
        "end_date": end_date or start_date,
        "start_date_breakdown": start_date_breakdown,
        "end_date_breakdown": end_date_breakdown,
        "description": description or "Applied via Horilla MCP",
    }
    try:
        return await _call(ctx, "POST", "leave/user-request/", json=body)
    except ToolError as e:
        if "'attachment'" in str(e):
            raise ToolError(
                "This leave type requires a supporting document (for example a medical "
                "certificate), which can't be attached through this tool. Ask the user to apply "
                "for it in the Horilla web app, or choose a leave type that needs no attachment."
            ) from None
        raise


@mcp.tool(annotations=READ)
async def list_leave_requests(
    ctx: Context,
    status: Literal["requested", "approved", "rejected", "cancelled"] | None = None,
    page: int = 1,
) -> Any:
    """Leave requests from employees you manage (needs manager or HR permission).
    Filter by status, e.g. 'requested' for requests waiting for a decision."""
    return _results(await _call(ctx, "GET", "leave/request/", params={"status": status, "page": page}))


@mcp.tool(annotations=DECIDE)
async def approve_leave_request(ctx: Context, leave_request_id: int) -> Any:
    """Approve a pending leave request (manager/HR only). Confirm with the user first."""
    return await _call(ctx, "PUT", f"leave/approve/{leave_request_id}/")


@mcp.tool(annotations=DECIDE)
async def reject_leave_request(ctx: Context, leave_request_id: int, reason: str) -> Any:
    """Reject a leave request with a reason the employee will see (manager/HR only).
    Confirm with the user first."""
    return await _call(ctx, "PUT", f"leave/reject/{leave_request_id}/", json={"reason": reason})


@mcp.tool(annotations=READ)
async def list_holidays(ctx: Context) -> Any:
    """Company holidays configured in Horilla."""
    return _results(await _call(ctx, "GET", "leave/holiday/"))


# ---- attendance -------------------------------------------------------------


@mcp.tool(annotations=WRITE)
async def clock_in(ctx: Context) -> Any:
    """Clock the current user in for today."""
    return await _call(ctx, "POST", "attendance/clock-in/", json={})


@mcp.tool(annotations=WRITE)
async def clock_out(ctx: Context) -> Any:
    """Clock the current user out."""
    return await _call(ctx, "POST", "attendance/clock-out/", json={})


@mcp.tool(annotations=READ)
async def list_my_attendance(ctx: Context, page: int = 1) -> Any:
    """The current user's attendance records (check-in/out times, worked hours)."""
    return _results(await _call(ctx, "GET", "attendance/my-attendance/", params={"page": page}))


class BearerAuth:
    """ASGI middleware: every HTTP request must carry `Authorization: Bearer <token>`."""

    def __init__(self, app: Any, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if not secrets.compare_digest(headers.get(b"authorization", b""), self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})
                return
        await self.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(description="Horilla HRMS MCP server")
    parser.add_argument("--http", action="store_true",
                        help="serve Streamable HTTP at /mcp instead of stdio (needs MCP_AUTH_TOKEN)")
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "9000")))
    parser.add_argument("--delegated", action="store_true",
                        help="HTTP only: no login of its own; each call carries its user's Horilla token")
    args = parser.parse_args()
    if args.delegated:
        if not args.http:
            raise SystemExit("--delegated needs --http.")
        os.environ["HORILLA_MCP_DELEGATED"] = "1"

    if not args.http:
        mcp.run(transport="stdio")
        return

    _load_dotenv()
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("HTTP mode needs MCP_AUTH_TOKEN set to a random string of 24+ characters.")
    import uvicorn

    app = mcp.streamable_http_app(host=args.host, stateless_http=True, json_response=True)
    uvicorn.run(BearerAuth(app, token), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
