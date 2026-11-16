"""Live smoke test: calls every MCP tool against a running Horilla instance.

Usage (from the project root, with Horilla running):
    python scripts/smoke_test.py                 # read-only tools, as HORILLA_USERNAME
    python scripts/smoke_test.py --write \\
        --employee michael.brown@horilla.com \\
        --manager christopher.miller@horilla.com

--write also runs the full leave flow (employee applies -> manager approves,
then a second request that the manager rejects) and clock-in/clock-out as the
employee. It creates real records in Horilla, so run it against a demo database.
Employee and manager use HORILLA_PASSWORD unless SMOKE_PASSWORD is set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, timedelta
from typing import Any

from mcp.client import Client

from horilla_mcp import server
from horilla_mcp.client import HorillaClient

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def as_user(username: str | None) -> None:
    """Point the server's client factory at a specific Horilla user."""
    server._load_dotenv()
    url = os.environ.get("HORILLA_URL", "http://localhost:8000")
    user = username or os.environ["HORILLA_USERNAME"]
    password = os.environ.get("SMOKE_PASSWORD") if username else None
    password = password or os.environ["HORILLA_PASSWORD"]
    server.make_client = lambda: HorillaClient(url, user, password)


async def call(c: Client, label: str, tool: str, args: dict[str, Any] | None = None, expect_error=False) -> Any:
    res = await c.call_tool(tool, args or {})
    text = "".join(getattr(b, "text", "") for b in res.content)
    try:
        data = json.loads(text)
    except ValueError:
        data = text
    ok = bool(res.is_error) == expect_error
    detail = text[:110].replace("\n", " ")
    results.append((PASS if ok else FAIL, label, detail))
    print(f"[{PASS if ok else FAIL}] {label}: {detail}")
    return data


def next_weekday(start: date, skip: int) -> str:
    d, n = start, 0
    while True:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
            if n > skip:
                return d.isoformat()


async def read_only() -> None:
    as_user(None)
    async with Client(server.mcp) as c:
        tools = await c.list_tools()
        print(f"{len(tools.tools)} tools: {', '.join(t.name for t in tools.tools)}\n")
        me = await call(c, "get_my_profile", "get_my_profile")
        emps = await call(c, "search_employees", "search_employees", {"search": "michael"})
        if isinstance(emps, dict) and emps.get("results"):
            await call(c, "get_employee", "get_employee", {"employee_id": emps["results"][0]["id"]})
        await call(c, "list_leave_types", "list_leave_types")
        await call(c, "list_leave_requests (requested)", "list_leave_requests", {"status": "requested"})
        await call(c, "list_holidays", "list_holidays")
        await call(c, "bad date is rejected", "apply_leave",
                   {"leave_type_id": 1, "start_date": "16-11-2026"}, expect_error=True)
        _ = me


async def write_flow(employee: str, manager: str) -> None:
    as_user(employee)
    async with Client(server.mcp) as c:
        bal = await call(c, "employee: get_my_leave_balance", "get_my_leave_balance")
        day1, day2 = next_weekday(date.today(), 20), next_weekday(date.today(), 22)
        half = {"start_date_breakdown": "first_half", "end_date_breakdown": "first_half"}
        # Find a leave type that needs no attachment: the first one Horilla accepts.
        r1 = None
        for row in sorted(bal.get("results") or [], key=lambda r: -(r.get("available_days") or 0)):
            if (row.get("available_days") or 0) < 1:
                continue
            lt = row["leave_type_id"]
            lt_id = lt["id"] if isinstance(lt, dict) else lt
            res = await c.call_tool("apply_leave", {"leave_type_id": lt_id, "start_date": day1, **half,
                                                    "description": "MCP smoke test (approve)"})
            text = "".join(getattr(b, "text", "") for b in res.content)
            if not res.is_error:
                r1 = json.loads(text)
                results.append((PASS, f"employee: apply_leave {day1} (type {lt_id})", text[:80]))
                print(f"[{PASS}] employee: apply_leave {day1} half day, leave type {lt_id}")
                break
            print(f"[info] leave type {lt_id} refused: {text[:120]}")
        if r1 is None:
            results.append((FAIL, "employee: apply_leave", "no leave type accepted the request"))
            print(f"[{FAIL}] employee: apply_leave: no leave type accepted the request")
            return
        r2 = await call(c, f"employee: apply_leave {day2} half day", "apply_leave",
                        {"leave_type_id": lt_id, "start_date": day2, **half,
                         "description": "MCP smoke test (reject)"})
        await call(c, "employee: list_my_leave_requests", "list_my_leave_requests")
        await call(c, "employee: cannot approve own leave", "approve_leave_request",
                   {"leave_request_id": r1.get("id", 0)}, expect_error=True)
        await call(c, "employee: clock_in", "clock_in")
        await call(c, "employee: clock_out", "clock_out")
        await call(c, "employee: list_my_attendance", "list_my_attendance")

    as_user(manager)
    async with Client(server.mcp) as c:
        await call(c, "manager: list_leave_requests", "list_leave_requests", {"status": "requested"})
        await call(c, "manager: approve_leave_request", "approve_leave_request", {"leave_request_id": r1["id"]})
        await call(c, "manager: reject_leave_request", "reject_leave_request",
                   {"leave_request_id": r2["id"], "reason": "Smoke test: overlapping release week"})

    as_user(employee)
    async with Client(server.mcp) as c:
        mine = await call(c, "employee: statuses after decision", "list_my_leave_requests")
        status = {r["id"]: r.get("status") for r in mine.get("results", [])}
        for rid, want in ((r1["id"], "approved"), (r2["id"], "rejected")):
            got = status.get(rid)
            results.append((PASS if got == want else FAIL, f"request {rid} is {want}", f"got {got}"))
            print(f"[{PASS if got == want else FAIL}] request {rid} is {want}: got {got}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--employee")
    ap.add_argument("--manager")
    a = ap.parse_args()
    asyncio.run(read_only())
    if a.write:
        if not (a.employee and a.manager):
            sys.exit("--write needs --employee and --manager")
        print()
        asyncio.run(write_flow(a.employee, a.manager))
    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
