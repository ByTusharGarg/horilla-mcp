"""MCP server exposing Horilla HRMS (employees, leave, attendance) as tools.

Configure with environment variables:
  HORILLA_URL       e.g. http://localhost:8000
  HORILLA_USERNAME  Horilla login (the server acts as this user)
  HORILLA_PASSWORD
"""

from __future__ import annotations

import argparse
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


def make_client() -> HorillaClient:
    """Build the Horilla client from the environment. Tests replace this function."""
    _load_dotenv()
    try:
        url = os.environ.get("HORILLA_URL", "http://localhost:8000")
        user = os.environ["HORILLA_USERNAME"]
        password = os.environ["HORILLA_PASSWORD"]
    except KeyError as e:
        raise RuntimeError(f"Missing environment variable {e.args[0]}") from None
    return HorillaClient(url, user, password)


@asynccontextmanager
async def lifespan(_: MCPServer) -> AsyncIterator[HorillaClient]:
    client = make_client()
    try:
        yield client
    finally:
        await client.aclose()


mcp = MCPServer(
    "horilla-hrms",
    instructions=(
        "Tools for the Horilla HRMS: look up employees, check leave balances, apply for leave, "
        "approve or reject leave requests, view holidays, and clock in or out. All actions run as "
        "the configured Horilla user, with that user's permissions. Dates use YYYY-MM-DD. Before "
        "applying for leave, call list_leave_types or get_my_leave_balance to find the leave_type_id. "
        "Confirm with the user before approving, rejecting or applying for leave."
    ),
    lifespan=lifespan,
)


async def _call(ctx: Context, method: str, path: str, *, params=None, json=None) -> Any:
    client: HorillaClient = ctx.request_context.lifespan_context
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
    client: HorillaClient = ctx.request_context.lifespan_context
    if client.me is None:
        await _call(ctx, "GET", "leave/available-leave/")  # forces a login
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
    args = parser.parse_args()

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
