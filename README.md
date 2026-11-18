# Horilla HRMS MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants such as Claude work with
[Horilla](https://www.horilla.com/), the open-source HRMS. Employees can check leave balances,
apply for leave and clock in or out in plain language; managers can review and approve or reject
their team's leave requests.

```
You:     "Apply for a half day of casual leave on 15 December, I have a dentist appointment."
Claude:  calls get_my_leave_balance -> apply_leave(leave_type_id=2, start_date="2026-12-15",
         start_date_breakdown="first_half", ...)
         "Done. Request #130 for the morning of 15 Dec is waiting for Christopher's approval."
```

Every action runs as one Horilla user, through Horilla's own REST API, so Horilla's
permission rules apply: an employee can't approve their own leave, and a manager sees only their team.

## Tools

| Tool | What it does | Who can use it |
|---|---|---|
| `get_my_profile` | The logged-in employee's record | Everyone |
| `search_employees` | Search employees by name (paginated) | Everyone (results limited by permissions) |
| `get_employee` | One employee's full record | Self, managers, HR |
| `list_leave_types` | Leave types defined in Horilla | Everyone |
| `get_my_leave_balance` | Available and carry-forward days per leave type | Everyone |
| `list_my_leave_requests` | Own leave requests and their status | Everyone |
| `apply_leave` | Apply for full or half-day leave | Everyone |
| `list_leave_requests` | Team leave requests, filterable by status | Managers, HR |
| `approve_leave_request` | Approve a request | Managers, HR |
| `reject_leave_request` | Reject a request with a reason | Managers, HR |
| `list_holidays` | Company holidays | Everyone |
| `clock_in` / `clock_out` | Attendance check-in and check-out | Everyone |
| `list_my_attendance` | Own attendance records | Everyone |

Read-only tools are marked `readOnlyHint`, and approve/reject are marked `destructiveHint`, so MCP
clients can ask the user before running them.

## How it works

```
Claude (Code / Desktop)  --MCP (stdio or HTTP)-->  horilla-mcp  --REST + JWT-->  Horilla (Django)  -->  PostgreSQL
```

- `src/horilla_mcp/client.py`: async Horilla API client. Logs in at `/api/auth/login/`, sends the
  JWT as a bearer token, and refreshes it automatically when Horilla returns 401.
- `src/horilla_mcp/server.py`: the MCP tools, built with the official MCP Python SDK (`MCPServer`,
  SDK 2.x). Validates dates before calling Horilla, unwraps pagination, and turns Horilla's API
  errors into clear tool errors the model can act on.

## Prerequisites

| Need | Version | Used for |
|---|---|---|
| Python | 3.12, 3.13 or 3.14 for Horilla; 3.11+ for this server | Horilla's `spacy` dependency doesn't support Python 3.15 yet |
| PostgreSQL | 14+ (tested with 18) | Horilla's database |
| Redis | 6+ (on Windows: [Memurai](https://www.memurai.com/)) | Horilla's cache (optional, but recommended) |
| Git | any | Cloning Horilla |
| Claude Code or Claude Desktop | current | Using the tools |

On Windows the installs are:

```powershell
winget install Python.Python.3.13
winget install PostgreSQL.PostgreSQL.18
winget install Memurai.MemuraiDeveloper     # run from an admin terminal; installs a Windows service
```

## 1. Set up Horilla (without Docker)

```powershell
cd C:\Users\<you>\projects
git clone https://github.com/horilla-opensource/horilla.git
cd horilla
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Create a database user and database (psql asks for your `postgres` password):

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost `
  -c "CREATE USER horilla_user WITH PASSWORD '<db-password>';" `
  -c "CREATE DATABASE horilla_db OWNER horilla_user ENCODING 'UTF8';"
```

Create `horilla\.env` (git ignores it):

```ini
DJANGO_SETTINGS_MODULE=horilla.settings
SECRET_KEY=<run: python -c "from django.core.management.utils import get_random_secret_key as g; print(g())">
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000
TIME_ZONE=Asia/Kolkata
DB_INIT_PASSWORD=<any random string>
DATABASE_URL=postgres://horilla_user:<db-password>@localhost:5432/horilla_db
REDIS_URL=redis://localhost:6379/0
```

Create the tables, load Horilla's demo company (about 276 employees with leave and attendance history),
and start the server:

```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py collectstatic --no-input
.venv\Scripts\python manage.py load_demo_data --no-input
.venv\Scripts\python manage.py runserver 127.0.0.1:8000
```

### Test users

Horilla's demo data doesn't publish passwords, so give these five accounts, one per kind of
user, the shared demo password `Horilla@Demo1`:

```powershell
.venv\Scripts\python manage.py shell -c "from django.contrib.auth import get_user_model as g; [ (u.set_password('Horilla@Demo1'), u.save()) for u in g().objects.filter(username__in=['admin','michael.brown@horilla.com','emily.clark@horilla.com','christopher.miller@horilla.com','emma.lee@horilla.com']) ]"
```

| Kind of user | Username | Password | Who they are | What they can do through the tools |
|---|---|---|---|---|
| Admin | `admin` | `Horilla@Demo1` | Adam Admin, superuser | Everything: sees all 276 employees and every leave request, approves or rejects any of them |
| HR manager | `michael.brown@horilla.com` | `Horilla@Demo1` | Michael Brown, "HR Manager" group; reports to Christopher Miller | Sees and decides company leave requests; also applies for his own leave |
| Leave manager | `emily.clark@horilla.com` | `Horilla@Demo1` | Emily Clark, "Leave Manager" group; reports to Christopher Miller | Approves or rejects leave requests; applies for her own |
| Reporting manager | `christopher.miller@horilla.com` | `Horilla@Demo1` | Christopher Miller, manages 7 people (Michael, Emily, Emma and others), no admin groups | Sees his team's leave requests (22 in the demo data) and approves or rejects them |
| Employee | `emma.lee@horilla.com` | `Horilla@Demo1` | Emma Lee, no groups; reports to Christopher Miller | Her own profile, balance, requests, holidays and attendance; `list_leave_requests` returns 403 |

These are demo-data accounts on a local Horilla. Don't reuse the password anywhere real, and
change it before exposing Horilla to a network.

Good pairs for the apply-and-approve flow: Emma (or Michael) applies, Christopher approves.
Five wrong passwords in a row lock an account for 30 minutes (django-axes); unlock it sooner with
`.venv\Scripts\python manage.py axes_reset`.

Check it: http://localhost:8000 shows the login page, and http://localhost:8000/api/swagger/
shows the API docs.

> Prefer Docker? Horilla's repo includes `docker-compose.yml`: `docker compose up -d` runs Horilla,
> PostgreSQL and Redis on port 8000, and the rest of this guide is the same.

## 2. Set up the MCP server

```powershell
cd C:\Users\<you>\projects\horilla-mcp
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env
```

Edit `.env` with the Horilla user the assistant should act as:

```ini
HORILLA_URL=http://localhost:8000
HORILLA_USERNAME=admin
HORILLA_PASSWORD=Horilla@Demo1
```

Use any of the [test users](#test-users) to act as a different kind of user. The server reads
`.env` from the project folder; real environment variables take precedence.

An app that has already signed the user in can start the server with that user's Horilla
tokens instead of a password: set `HORILLA_ACCESS_TOKEN` and `HORILLA_REFRESH_TOKEN` (from
Horilla's `/api/auth/login/`) and optionally `HORILLA_EMPLOYEE` (the login response's `employee`
object, as JSON). The server then never reads `.env` and never logs in with a password; when
the refresh token stops working, tools return "The Horilla session has expired. Sign in again."
The [HR Assistant](https://github.com/ByTusharGarg/hr-assistant) uses this to run one server
per signed-in user.

## 3. Connect it to Claude

**Claude Code:**

```powershell
claude mcp add horilla --scope user -- "C:\Users\<you>\projects\horilla-mcp\.venv\Scripts\horilla-mcp.exe"
claude mcp list        # horilla should show as connected
```

Then ask Claude things like "how many casual leave days do I have left?" or "show leave requests
waiting for my approval". Inside Claude Code, `/mcp` shows the server and its tools.

**Claude Desktop:** add this to `claude_desktop_config.json` (Settings > Developer > Edit Config)
and restart the app:

```json
{
  "mcpServers": {
    "horilla": {
      "command": "C:\\Users\\<you>\\projects\\horilla-mcp\\.venv\\Scripts\\horilla-mcp.exe"
    }
  }
}
```

## 4. Run everything locally (after setup)

Once setup is done, this is the everyday routine. Each time, you need:
1. **Horilla running in its own terminal.**
2. **The MCP server**, which needs no terminal of its own: Claude Code, Claude Desktop and MCP
   Inspector each start it when they connect.

PostgreSQL and Memurai (Redis) are Windows services that start automatically.

**Terminal 1: start Horilla** and leave it running.

```powershell
cd C:\Users\<you>\projects\horilla
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python manage.py runserver 127.0.0.1:8000
```

Wait for `Starting development server at http://127.0.0.1:8000/`. To confirm it's up, open
http://localhost:8000/health/ (should show `{"status": "ok", ...}`) or log in at http://localhost:8000.

**Terminal 2: use the MCP server.** Pick one:

```powershell
cd C:\Users\<you>\projects\horilla-mcp

# a) chat with it in Claude Code (after the one-time `claude mcp add` in step 3)
claude

# b) test the tools by hand in MCP Inspector (see the next section)
npx @modelcontextprotocol/inspector@2.13.0 .venv\Scripts\horilla-mcp.exe

# c) run the automated tests
.venv\Scripts\python -m pytest -q
.venv\Scripts\python scripts\smoke_test.py
```

**After pulling new code.** In `horilla`, run `git pull`, then
`.venv\Scripts\python -m pip install -r requirements.txt` and `.venv\Scripts\python manage.py migrate`.
In `horilla-mcp`, run `git pull`, then `.venv\Scripts\python -m pip install -e ".[dev]"`.

**To stop**, press `Ctrl+C` in Terminal 1. The MCP server exits by itself when its client closes.

| Service | Address | Started by |
|---|---|---|
| Horilla web app and API | http://localhost:8000 (API docs at `/api/swagger/`) | You (Terminal 1) |
| PostgreSQL | localhost:5432 | Windows service, automatic |
| Redis (Memurai) | localhost:6379 | Windows service, automatic |
| MCP server (stdio) | none (runs as a child process of the client) | Claude Code, Claude Desktop or Inspector |
| MCP server (HTTP mode, optional) | http://127.0.0.1:9000/mcp | You: `horilla-mcp --http` |
| MCP Inspector web UI | http://127.0.0.1:6274 | You: `npx @modelcontextprotocol/inspector@2.13.0 ...` |

## 5. Test with MCP Inspector

[MCP Inspector](https://github.com/modelcontextprotocol/inspector) is the official tool for calling
an MCP server's tools by hand, without an AI in the loop. Use it to check that a tool works and to
see exactly what it returns. It needs Node.js and runs through `npx`, so there's nothing to install.

> Version 2.13.0 is pinned because 2.14 and later need Node.js 24.22.1 or newer. With a newer
> Node.js you can drop the `@2.13.0`.

Horilla must be running (Terminal 1 above). The server logs in as the user in `.env`.

### Web UI

```powershell
cd C:\Users\<you>\projects\horilla-mcp
npx @modelcontextprotocol/inspector@2.13.0 .venv\Scripts\horilla-mcp.exe
```

It prints a URL like `http://127.0.0.1:6274?MCP_INSPECTOR_API_TOKEN=...`. Open that full URL, since
the token in it is required. Then:

1. Click **Connect**. The status turns green. If it fails, check that Horilla is running and that
   `.env` has the right password.
2. Open the **Tools** tab and click **List Tools** to see all 14 tools with their descriptions and inputs.
3. Click a tool, fill in its arguments, and click **Run Tool**. Good first calls:
   - `list_holidays`: no arguments.
   - `search_employees`: `search` = `michael`.
   - `get_my_leave_balance`: shows each leave type's `id`, which you need for `apply_leave`.
   - `apply_leave`: `leave_type_id` = `2` (Casual Leave), `start_date` = a future weekday such as
     `2026-12-15`, and `start_date_breakdown` = `first_half`. This creates a real request.
4. Errors appear in the result in red, for example `start_date` = `15-12-2026` returns
   "start_date must be a date in YYYY-MM-DD format".

Press `Ctrl+C` in the terminal to stop Inspector.

To test as a different user, override the login just for this run:

```powershell
npx @modelcontextprotocol/inspector@2.13.0 -e HORILLA_USERNAME=christopher.miller@horilla.com .venv\Scripts\horilla-mcp.exe
```

Christopher is Michael's manager, so `list_leave_requests` with `status` = `requested` shows
Michael's pending request, which you can then approve with `approve_leave_request`. Swap in
any other [test user](#test-users) the same way, e.g. `HORILLA_USERNAME=emma.lee@horilla.com` to
see what a plain employee is allowed to do.

### CLI mode (no browser, good for quick checks and scripts)

```powershell
$I = "npx @modelcontextprotocol/inspector@2.13.0 --cli"

# list all tools
iex "$I .venv\Scripts\horilla-mcp.exe --method tools/list"

# call a tool without arguments
iex "$I .venv\Scripts\horilla-mcp.exe --method tools/call --tool-name list_holidays"

# call a tool with arguments
iex "$I .venv\Scripts\horilla-mcp.exe --method tools/call --tool-name search_employees --tool-arg search=michael"
```

Each command prints the JSON response and exits.

### Testing the HTTP mode with Inspector

```powershell
# terminal A: run the server over HTTP
$env:MCP_AUTH_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:MCP_AUTH_TOKEN                     # copy this value
.venv\Scripts\horilla-mcp.exe --http --port 9000

# terminal B: point Inspector at the URL, with the token
npx @modelcontextprotocol/inspector@2.13.0 --cli http://127.0.0.1:9000/mcp `
  --header "Authorization: Bearer <token>" --method tools/list
```

In the web UI, choose transport **Streamable HTTP**, enter `http://127.0.0.1:9000/mcp`, and add an
`Authorization` header with the value `Bearer <token>`. Without the header the server answers 401.

## 6. Automated tests

**Unit tests** run the MCP server in memory against a fake Horilla API, so they need no running
Horilla and are safe for CI:

```powershell
.venv\Scripts\python -m pytest -q
```

They cover login and bearer tokens, token refresh after a 401, sessions started from tokens
(no password login, a clear error when the session expires), delegated mode (each call runs as
the token in its `_meta`, calls without one are refused), pagination, date validation,
leave-application defaults, the attachment-required error, reject reasons, API error handling
and the HTTP auth middleware.

**Live smoke test** calls every tool against your running Horilla, through a real MCP client session:

```powershell
# read-only tools, as the user in .env
.venv\Scripts\python scripts\smoke_test.py

# plus the full flow: Michael applies for two half days, clocks in and out;
# Christopher approves one request and rejects the other; Michael sees both outcomes
.venv\Scripts\python scripts\smoke_test.py --write `
  --employee michael.brown@horilla.com --manager christopher.miller@horilla.com
```

`--write` creates real leave and attendance records, so run it only against demo data.
The employee and manager use `HORILLA_PASSWORD` unless `SMOKE_PASSWORD` is set.
The last run passed 21/21 checks.

## 7. Deploy

Locally, Claude starts the server itself over stdio and nothing needs deploying. To share one
server with a team, or to use it from Claude on the web, run it over **Streamable HTTP**:

```powershell
$env:MCP_AUTH_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
horilla-mcp --http --host 127.0.0.1 --port 9000      # serves http://127.0.0.1:9000/mcp
```

Every HTTP request must send `Authorization: Bearer <MCP_AUTH_TOKEN>`; others get a 401. The
server won't start in HTTP mode without a token of at least 24 characters. Connect Claude Code
to it with:

```powershell
claude mcp add horilla-remote --transport http https://<your-host>/mcp --header "Authorization: Bearer <token>"
```

### Many users, many copies: delegated mode

By default the server acts as the one Horilla user in its `.env`. An app that signs its own
users in (like the [HR Assistant](https://github.com/ByTusharGarg/hr-assistant)) can instead run
one shared server in **delegated mode** and send each tool call with the calling user's Horilla
access token, so every call runs with that user's own Horilla permissions:

```powershell
horilla-mcp --http --delegated --port 9000    # or set HORILLA_MCP_DELEGATED=1
```

- The server never logs in itself and needs no `HORILLA_USERNAME` / `HORILLA_PASSWORD`, only
  `HORILLA_URL` and `MCP_AUTH_TOKEN` (which authenticates the calling app).
- Each `tools/call` carries the user in its `_meta`:
  `"horilla-mcp/access-token": "<the user's Horilla access JWT>"` and, optionally,
  `"horilla-mcp/employee": {...}` (the login response's `employee` object, which
  `get_my_profile` returns). A call without a token is refused.
- The calling app keeps the user's refresh token and renews the access token before it expires;
  an expired token gets "The Horilla session has expired. Sign in again."
- HTTP mode is stateless (`stateless_http`, plain JSON responses: no MCP session id and no SSE
  stream to pin), and delegated mode holds no login, so any copy can answer any call: run as many
  as you need behind an ordinary load balancer, no sticky sessions.

With the Python SDK:

```python
await client.call_tool("get_my_leave_balance", {}, meta={"horilla-mcp/access-token": access_jwt})
```

A token in `_meta` also works in the normal modes, for that one call.

**Hosting options**

- **A VM or server next to Horilla** (simplest): install as in step 2, run `horilla-mcp --http`
  as a service (systemd on Linux; NSSM or Task Scheduler on Windows), and put it behind a reverse
  proxy that terminates HTTPS (Caddy or nginx).
- **A container platform** (Railway, Render, Fly.io): the included `Dockerfile` builds the
  server. Set `HORILLA_URL`, `HORILLA_USERNAME`, `HORILLA_PASSWORD` and `MCP_AUTH_TOKEN` as
  secrets. The platform's `PORT` variable is respected. Horilla must be reachable from the
  platform, so deploy Horilla too, or expose your local instance through a tunnel such as
  Cloudflare Tunnel for demos.

**Before production**

- Always use HTTPS: the bearer token and HR data travel in every request.
- Give the MCP its own Horilla account with only the permissions it needs, not `admin`.
- Without `--delegated`, one shared token means every caller acts as the same Horilla user. For
  per-employee identity, use delegated mode from a trusted backend that signs users in (above);
  for MCP clients that connect directly (e.g. Claude), the next step is MCP's OAuth support,
  mapping each signed-in user to their own Horilla account.
- Set `DEBUG=False` and a strong `SECRET_KEY` in Horilla, and keep `.env` files out of git
  (`.gitignore` already covers this project's).

## Troubleshooting

| Problem | Fix |
|---|---|
| `Login to Horilla failed (401)` | Check `HORILLA_USERNAME`/`HORILLA_PASSWORD` in `.env`. Repeated failures trigger Horilla's lockout (django-axes); wait, or run `manage.py axes_reset`. |
| `This leave type requires a supporting document` | Sick and maternity leave need an attachment, which the API tool can't send. Apply in the Horilla web app, or use another leave type. |
| `pip install` of Horilla fails on `spacy` | You're on Python 3.15. Use 3.13: `py -3.13 -m venv .venv`. |
| `UnicodeEncodeError` from `manage.py` on Windows | Set `$env:PYTHONIOENCODING = "utf-8"` first. |
| Tools don't appear in Claude | Run `claude mcp list`; in Claude Desktop, restart the app after editing the config. |
| `/api/swagger.json` returns a redirect | Use `/api/swagger/?format=openapi` for the raw schema. |
| Inspector shows `EBADENGINE` or won't start | That Inspector version needs a newer Node.js. Use `@2.13.0`, or update Node.js. |
| Inspector web UI says unauthorized | Open the full URL it printed, including `?MCP_INSPECTOR_API_TOKEN=...`. |
| Inspector can't connect | Start Horilla first (http://localhost:8000/health/ should answer), and check `.env`. |

## Project layout

```
horilla-mcp/
├── src/horilla_mcp/
│   ├── client.py        Horilla REST client (JWT login, refresh, errors)
│   └── server.py        MCP tools, stdio and HTTP entry point, bearer auth
├── tests/test_server.py offline unit tests (fake Horilla API)
├── scripts/smoke_test.py live end-to-end test
├── Dockerfile           for HTTP deployment
├── .env.example
└── pyproject.toml
```
