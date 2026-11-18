"""Offline tests: the MCP server runs in memory against a fake Horilla API (httpx.MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.client import Client

from horilla_mcp import server
from horilla_mcp.client import HorillaClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeHorilla:
    """Records requests and answers like Horilla's REST API."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.logins = 0
        self.expire_next = False  # make the next authenticated call return 401 once
        self.refresh_fails = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if path == "/api/auth/login/":
            self.logins += 1
            body = json.loads(req.content)
            if body["password"] != "secret":
                return httpx.Response(401, json={"error": "Invalid credentials"})
            return httpx.Response(200, json={"access": f"tok{self.logins}", "refresh": "r1",
                                             "employee": {"id": 2, "full_name": "Michael Brown"}})
        if path == "/api/auth/refresh/":
            if self.refresh_fails:
                return httpx.Response(401, json={"detail": "Token is blacklisted"})
            return httpx.Response(200, json={"access": "refreshed"})
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"detail": "token expired"})
        if path == "/api/leave/holiday/":
            return httpx.Response(200, json={"count": 1, "next": None, "results": [{"id": 1, "name": "Diwali"}]})
        if path == "/api/employee/list/employees/":
            return httpx.Response(200, json={"count": 40, "next": "http://x/?page=2", "results": [{"id": 2}]})
        if path == "/api/leave/user-request/" and req.method == "POST":
            body = json.loads(req.content)
            if body["leave_type_id"] == 3:
                return httpx.Response(400, json={"attachment": ["This field is required."]})
            return httpx.Response(201, json={"id": 99, **body, "status": "requested"})
        if path.startswith("/api/leave/approve/"):
            return httpx.Response(200)
        if path.startswith("/api/leave/reject/"):
            return httpx.Response(200)
        return httpx.Response(404, json={"detail": "Not found."})


@pytest.fixture
def fake(monkeypatch):
    fake = FakeHorilla()
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setattr(server, "make_client",
                        lambda: HorillaClient("http://horilla.test", "michael", "secret", transport=transport))
    return fake


async def call(tool: str, args: dict | None = None):
    async with Client(server.mcp) as c:
        res = await c.call_tool(tool, args or {})
        text = "".join(getattr(b, "text", "") for b in res.content)
        return res.is_error, text


async def test_lists_all_tools(fake):
    async with Client(server.mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert {"apply_leave", "approve_leave_request", "reject_leave_request", "clock_in",
            "search_employees", "get_my_leave_balance"} <= names


async def test_logs_in_and_sends_bearer_token(fake):
    is_error, text = await call("list_holidays")
    assert not is_error
    assert json.loads(text) == {"count": 1, "results": [{"id": 1, "name": "Diwali"}]}
    api_call = fake.requests[-1]
    assert api_call.headers["authorization"] == "Bearer tok1"


async def test_pagination_flags_more_pages(fake):
    _, text = await call("search_employees", {"search": "mi"})
    data = json.loads(text)
    assert data["count"] == 40 and data["has_more"] is True
    assert fake.requests[-1].url.params["search"] == "mi"


async def test_refreshes_token_after_401(fake):
    fake.expire_next = True
    is_error, _ = await call("list_holidays")
    assert not is_error
    assert fake.requests[-1].headers["authorization"] == "Bearer refreshed"


@pytest.fixture
def token_session(monkeypatch):
    """A server started with tokens from a login another app made (the HR Assistant)."""
    fake = FakeHorilla()
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setattr(server, "make_client", lambda: HorillaClient(
        "http://horilla.test", access_token="given", refresh_token="r9",
        me={"id": 7, "full_name": "Emma Lee"}, transport=transport))
    return fake


async def test_token_session_uses_given_tokens_without_logging_in(token_session):
    is_error, _ = await call("list_holidays")
    assert not is_error
    assert token_session.logins == 0
    assert token_session.requests[-1].headers["authorization"] == "Bearer given"
    _, text = await call("get_my_profile")
    assert json.loads(text)["full_name"] == "Emma Lee"


async def test_token_session_refreshes_then_reports_expiry(token_session):
    token_session.expire_next = True
    is_error, _ = await call("list_holidays")
    assert not is_error and token_session.requests[-1].headers["authorization"] == "Bearer refreshed"
    token_session.expire_next = True
    token_session.refresh_fails = True
    is_error, text = await call("list_holidays")
    assert is_error and "session has expired" in text
    assert token_session.logins == 0  # never falls back to a password login


def test_make_client_prefers_tokens_from_env(monkeypatch):
    monkeypatch.setenv("HORILLA_URL", "http://horilla.test")
    monkeypatch.setenv("HORILLA_ACCESS_TOKEN", "a1")
    monkeypatch.setenv("HORILLA_REFRESH_TOKEN", "r1")
    monkeypatch.setenv("HORILLA_EMPLOYEE", '{"id": 7, "full_name": "Emma Lee"}')
    monkeypatch.setenv("HORILLA_USERNAME", "admin")
    monkeypatch.setenv("HORILLA_PASSWORD", "secret")
    client = server.make_client()
    assert client.me == {"id": 7, "full_name": "Emma Lee"}
    assert client._access == "a1" and client._username is None


def test_client_needs_credentials_or_tokens():
    with pytest.raises(ValueError):
        HorillaClient("http://horilla.test")


@pytest.fixture
def delegated(monkeypatch):
    """Delegated mode: no login of its own; each call names its user's token in _meta."""
    fake = FakeHorilla()
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setenv("HORILLA_MCP_DELEGATED", "1")
    monkeypatch.setattr(server, "make_client", lambda: pytest.fail("delegated mode must not log in"))
    monkeypatch.setattr(server, "make_delegated_client", lambda token, me: HorillaClient(
        "http://horilla.test", access_token=token, me=me, transport=transport))
    return fake


async def call_as(token: str | None, tool: str, args: dict | None = None, employee=None):
    meta = {}
    if token:
        meta[server.DELEGATED_TOKEN] = token
    if employee:
        meta[server.DELEGATED_EMPLOYEE] = employee
    async with Client(server.mcp) as c:
        res = await c.call_tool(tool, args or {}, meta=meta or None)
        return res.is_error, "".join(getattr(b, "text", "") for b in res.content)


async def test_delegated_calls_run_as_the_token_in_meta(delegated):
    is_error, _ = await call_as("emma-token", "list_holidays")
    assert not is_error and delegated.requests[-1].headers["authorization"] == "Bearer emma-token"
    await call_as("chris-token", "list_holidays")
    assert delegated.requests[-1].headers["authorization"] == "Bearer chris-token"
    _, text = await call_as("emma-token", "get_my_profile", employee={"id": 8, "full_name": "Emma Lee"})
    assert json.loads(text)["full_name"] == "Emma Lee"
    assert delegated.logins == 0


async def test_delegated_mode_refuses_calls_without_a_user(delegated):
    is_error, text = await call_as(None, "list_holidays")
    assert is_error and "delegated mode" in text and delegated.requests == []


async def test_expired_delegated_token_asks_to_sign_in_again(delegated):
    delegated.expire_next = True
    is_error, text = await call_as("old-token", "list_holidays")
    assert is_error and "session has expired" in text


async def test_apply_leave_defaults_end_date(fake):
    is_error, text = await call("apply_leave", {"leave_type_id": 2, "start_date": "2026-12-15"})
    assert not is_error
    sent = json.loads(fake.requests[-1].content)
    assert sent["end_date"] == "2026-12-15"
    assert sent["start_date_breakdown"] == "full_day"


async def test_apply_leave_rejects_bad_date_without_calling_api(fake):
    is_error, text = await call("apply_leave", {"leave_type_id": 2, "start_date": "15/12/2026"})
    assert is_error and "YYYY-MM-DD" in text
    assert fake.requests == []


async def test_apply_leave_explains_required_attachment(fake):
    is_error, text = await call("apply_leave", {"leave_type_id": 3, "start_date": "2026-12-15"})
    assert is_error and "supporting document" in text


async def test_reject_sends_reason(fake):
    is_error, _ = await call("reject_leave_request", {"leave_request_id": 7, "reason": "Release week"})
    assert not is_error
    req = fake.requests[-1]
    assert req.method == "PUT" and req.url.path == "/api/leave/reject/7/"
    assert json.loads(req.content) == {"reason": "Release week"}


async def test_api_errors_become_tool_errors(fake):
    is_error, text = await call("get_employee", {"employee_id": 12345})
    assert is_error and "404" in text


async def test_bearer_auth_middleware():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guarded = server.BearerAuth(app, "x" * 32)
    transport = httpx.ASGITransport(app=guarded)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/mcp")).status_code == 401
        assert (await c.post("/mcp", headers={"Authorization": "Bearer wrong"})).status_code == 401
        ok = await c.post("/mcp", headers={"Authorization": "Bearer " + "x" * 32})
        assert ok.status_code == 200
    assert seen == ["/mcp"]
