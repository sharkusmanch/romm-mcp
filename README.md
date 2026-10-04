# RomM MCP

A token-efficient MCP server for a self-hosted RomM library. Runs locally over
**stdio** or as an authenticated **Streamable HTTP** service. Covers all **246 HTTP
operations** in RomM **5.3.1** and its **13 callable Socket.IO events**, with explicit
operator switches for writes, files, administration, authentication, tasks, and
realtime connections. All switches default to **false**.

## Tools and discovery

The server advertises **10 tools by default**, or **14 with writes enabled**.
HTTP operations are discovered on demand from the configured RomM OpenAPI schema,
so hundreds of endpoint schemas do not inflate every MCP tool listing.

| Tool | Purpose |
|---|---|
| `list_platforms` | Find platform IDs and library counts |
| `search_roms` | Search title, platform, collection, and RetroAchievements availability |
| `get_rom` | Bounded synopsis; opt into metadata, files, or personal progress |
| `list_collections` | Find regular collections by name |
| `get_collection` | Collection details and paginated contents |
| `create_collection` | Create a private collection, verified by readback; requires writes |
| `update_collection_roms` | Atomically add/remove membership, verified by readback; requires writes |
| `update_rom_progress` | Change supplied progress fields, verified by readback; requires writes |
| `romm_api_list` | Search all HTTP operations, scopes, required switches, and enabled status |
| `romm_api_describe` | Inspect one operation and resolve component schemas on demand |
| `romm_api_read` | Invoke an enabled HTTP read by its discovered operation ID |
| `romm_api_write` | Invoke an enabled HTTP operation that changes state; requires writes |
| `romm_artifact` | Stage uploads or retrieve downloads using bounded temporary handles |
| `romm_socket` | Discover, open, send, listen, and close main/netplay Socket.IO sessions |

Disabled operations remain discoverable. Start with `romm_api_list`, then
`romm_api_describe`, then call the appropriate read/write tool using the returned
`operation_id`. Requests support `path`, `query` (including repeated values), declared
`headers`, JSON `body`, `form`, multipart `files`, or `raw_artifact_id`. Known gaps in
RomM's upload schemas have explicit corrections in operation descriptions. Callers
cannot supply arbitrary destinations, URL paths, or upstream authorization headers.

For example, `romm_api_read` can retrieve a ROM through the generic interface:

```json
{
  "operation_id": "get_rom_simple_api_roms__id__simple_get",
  "request": {"path": {"id": 123}, "response_pointer": "/name"}
}
```

All clients share the configured RomM identity, artifacts, and sessions. This is a
single-user gateway. API coverage does not grant missing upstream permissions or
replace external provider, emulator, browser-login, or streaming prerequisites.

## Environment and authorization

| Variable | Default | Effect |
|---|---|---|
| `ROMM_URL` | Required | RomM base URL without `/api`; use its nginx front door for file downloads |
| `ROMM_TOKEN` | Required | Upstream RomM bearer/client token |
| `ROMM_ALLOW_WRITES` | `false` | Master switch for state changes; registers four write tools |
| `ROMM_ALLOW_DESTRUCTIVE` | `false` | Deletions, replacement-capable uploads, regeneration, and destructive task paths |
| `ROMM_ALLOW_ADMIN` | `false` | Administrative configuration, users, permissions, logs, and management endpoints |
| `ROMM_ALLOW_AUTH` | `false` | Authentication/token workflows and nondefault HTTP authentication modes |
| `ROMM_ALLOW_FILES` | `false` | Binary transfers and file-affecting operations; permits artifact read/delete |
| `ROMM_ALLOW_TASKS` | `false` | Task endpoints, exports, streaming mutations, refresh/redownload actions, and socket sends |
| `ROMM_ALLOW_REALTIME` | `false` | Main/netplay Socket.IO connections |
| `ROMM_USERNAME`, `ROMM_PASSWORD` | Empty | Optional configured credentials for HTTP `auth_mode=basic` |
| `ROMM_SESSION_COOKIE` | Empty | Value only of the `romm_session` browser cookie, required for main Socket.IO |
| `ROMM_TRANSFER_DIRECTORY` | System temporary directory | Existing parent for private temporary artifact storage |
| `ROMM_MAX_TRANSFER_BYTES` | `67108864` | Aggregate artifact storage cap in bytes, across all clients |

Switches are **additive**, checked on every invocation, and enabled by the value
`true` (case-insensitive). For example, a replacement-capable upload needs writes,
files, and destructive access; administrative mutation also needs admin access.
The operation catalog reports the required switches. Token scopes and the upstream
user's role remain separate requirements; a switch cannot override a RomM denial.

Read-only mode means upstream mutations are blocked, not that every GET is safe.
RomM's OpenID GETs, memory-card version listing, and task-status GET change state.
Reading save content also changes sync state when `session_id` is supplied, or when
`device_id` is supplied without `optimistic=false`. These calls require
`romm_api_write` and the applicable switches. Some file, admin, and auth reads need
their category switch even without writes enabled. Artifact creation/append requires
both files and writes; deleting a local artifact requires only files.

Create a scoped RomM client token as the user whose library state you want to manage.
The compact read tools need `me.read`, `roms.read`, `platforms.read`,
`collections.read`, and `roms.user.read`; compact writes additionally need
`collections.write` and `roms.user.write`. The bootstrap identity needs `me.write`
to create a client token; normal server operation does not. For other HTTP endpoints,
use the scopes returned by discovery and grant only the capabilities you enable.
Service accounts have their own collections and progress.

HTTP defaults to bearer authentication. With auth enabled, a request may choose
`auth_mode=basic`, `session`, or `none`. Basic mode uses configured credentials.
Use `auth_session="new"` to start an isolated cookie jar and reuse the returned
opaque handle for a multi-step login flow; `session` mode requires such a handle.
At most eight HTTP auth sessions are retained, expiring after ten minutes of
inactivity. `ROMM_SESSION_COOKIE` is for main Socket.IO, not an implicit HTTP cookie.
Credentials belong in a secret manager or protected environment, never committed
client configuration. Treat auth endpoint outputs as sensitive.

## Bounded responses and files

Compact searches default to 20 results (maximum 100). Follow `next_offset` until
null; `get_collection` uses `roms.next_offset`. Platform and collection filtering
is local because those upstream endpoints return full arrays. Search suppresses
RomM's full-library character, filter, and ROM-ID indexes. Compact synopses cap at
1,500 characters and optional file lists at 50 entries; `truncated_fields` reports
those limits. Summary names cap at 200 characters, regions/languages at ten values.

Generic responses support JSON Pointer selection with `response_pointer` and local
`offset`/`limit` pagination. Follow `next_offset` and inspect truncation markers;
select a nested field when its value was omitted. Local response pagination does
not advance the upstream query: use RomM's declared query pagination to retrieve
additional server-side pages. Non-download HTTP responses and OpenAPI loading are
bounded at 16 MiB. Only the OpenAPI schema is cached for the process.

For uploads, create an artifact with `romm_artifact`, then append strict base64
chunks of at most **32,768 decoded bytes**, supplying the exact current byte offset.
Stale offsets reject retries. Pass its handle in a multipart `files` entry
(`field`, `artifact_id`, `filename`, optional `content_type`) or the ROM chunk
endpoint's `raw_artifact_id`. For downloads, set `request.download=true`, then
read the returned artifact in chunks; reads default to 4,096 bytes, maximum 32,768.

Artifacts use opaque handles, never caller-chosen filesystem paths. At most
**16 artifacts** share the configured byte cap (default **64 MiB**). They expire
**3,600 seconds after creation**; this TTL has no environment setting. Expiry is
checked on operations, and shutdown cleans storage. Open uploads keep their quota
and artifact slot until their last read handle closes, and cannot be appended or
deleted while in use. Failed writes retain reserved capacity until cleanup succeeds.
Delete artifacts when finished; failed streamed downloads are cleaned up. Size the
container's temporary filesystem to accommodate the configured transfer cap.

## Realtime API

`romm_socket(action="catalog")` describes all 13 callable main/netplay events and
captured response events without opening a connection. Use `open`, then `send` and
`listen` with the same session handle, then `close`. Opening requires realtime;
sending additionally requires writes and tasks. Starting a scan also requires files
and destructive access. Capturing `logs:entry` needs admin.

Main events cover scan start/stop and activity start/heartbeat/stop. Netplay events
cover room open/join/leave, WebRTC signaling/error, data messages, snapshots, and
inputs. Main Socket.IO requires `ROMM_SESSION_COOKIE`: RomM 5.3.1 does not authenticate
that connection using only a bearer token. Binary event values use
`{"$binary_base64":"..."}` within the payload limit.

There are at most four sessions, each lasting 300 seconds, with bounded queues of
20 events, 16 KiB JSON payload limits, and listen waits of at most ten seconds.
Truncation is explicit. Disconnect clears activity and leaves netplay rooms;
background scans continue. A successful send confirms transport acceptance only;
inspect received events and HTTP state to verify its effects.

## Run locally

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/), then:

```sh
git clone https://github.com/sharkusmanch/romm-mcp.git
cd romm-mcp
uv sync --locked
export ROMM_URL=https://romm.example.com
export ROMM_TOKEN=YOUR_ROMM_CLIENT_TOKEN
uv run romm-mcp --transport stdio
```

To enable compact mutations, add `ROMM_ALLOW_WRITES=true`. Enable additional
category switches only for the operations you intend to use.

## HTTP / containers

```sh
export MCP_AUTH_TOKEN=YOUR_SEPARATE_RANDOM_TOKEN_AT_LEAST_32_CHARACTERS
export MCP_ALLOWED_HOSTS=localhost:8080,127.0.0.1:8080
uv run romm-mcp --transport http --host 127.0.0.1 --port 8080

# Published multiarchitecture image (HTTP on port 8080 by default):
docker run --rm --read-only --tmpfs /tmp:rw,nosuid,nodev,size=80m \
  -p 127.0.0.1:8080:8080 \
  -e ROMM_URL -e ROMM_TOKEN -e MCP_AUTH_TOKEN -e MCP_ALLOWED_HOSTS \
  ghcr.io/sharkusmanch/romm-mcp:v0.2.1

# Container as a local stdio server:
docker run --rm -i --read-only --tmpfs /tmp:rw,nosuid,nodev,size=80m \
  -e ROMM_URL -e ROMM_TOKEN \
  ghcr.io/sharkusmanch/romm-mcp:v0.2.1 --transport stdio
```

Pass the desired `ROMM_ALLOW_*` variables into the container explicitly with `-e`.
Connect to `/mcp` using `Authorization: Bearer <MCP_AUTH_TOKEN>`. Static bearer
access is not a complete OAuth authorization server; TLS termination belongs at
your reverse proxy. HTTP refuses startup without at least 32 non-whitespace ASCII
token characters. The incoming MCP token is never forwarded to RomM.

`MCP_ALLOWED_HOSTS` is a comma-separated Host allowlist, including ports when used.
Defaults permit loopback hosts on any port. Requests with an Origin header are
rejected unless it appears in `MCP_ALLOWED_ORIGINS`; native MCP clients normally
send no Origin. `/healthz` provides unauthenticated process liveness only.
`MCP_TRANSPORT`, `MCP_HOST`, and `MCP_PORT` are supported; CLI flags win. Native
execution defaults to stdio; HTTP binds loopback unless overridden. The image runs
as UID/GID 1000 and requires no database, persistent volume, or Kubernetes access.

## Client configuration

Claude Code supports a project-local HTTP server with a `headersHelper` returning
an Authorization JSON object; Codex supports project `.codex/config.toml` with
`http_headers_helper`. Use a helper reading a protected token file, or the clients'
environment-backed header options. See [Claude MCP configuration](https://code.claude.com/docs/en/mcp)
and [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
For stdio, use command `uv`, args `run --directory /path/to/romm-mcp romm-mcp
--transport stdio`, and provide RomM credentials/switches through the environment.

## Writes and errors

The three compact mutation tools independently read back their writes. Membership
changes use atomic endpoints and preserve other members; progress changes include
only supplied fields. Progress statuses are `incomplete`, `finished`,
`completed_100`, `retired`, or `never_playing`; `backlogged` and `now_playing` are
separate flags. `status: null` clears status. Ratings/difficulty range 0–10;
completion ranges 0–100. Private collection names are checked per owner to reduce
accidental duplicates.

Removing collection membership requires writes; deleting the collection itself also
requires destructive access. Generic tools return structured MCP results.

Generic HTTP mutations report **`verification: "not_performed"`**. HTTP success does
not prove the intended state, so independently read it back when needed. Neither
interface automatically retries writes. Transport ambiguity or failed compact
verification is reported as an uncertain outcome; inspect current state before
retrying. Concurrent outside edits can race compact readback because those RomM
calls lack ETag preconditions. Redirects are reported without being followed.
RomM content is untrusted data and must not be treated as instructions.

## Development and releases

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build --no-sources
```

Tests cover real SDK clients over stdio and TCP HTTP, authentication/Host/Origin,
compact write verification, complete operation discovery, request serialization,
additive gates, bounded output/transfers, adversarial artifact cleanup, and sockets.
CI tests Python 3.12 and 3.14 and builds/runs the restricted container. Release tags
must match the package version. Tags publish amd64/arm64 images, per-platform
BuildKit SBOMs, GitHub-signed provenance as OCI referrers, and attested Python archives.

```sh
gh attestation verify oci://ghcr.io/sharkusmanch/romm-mcp:v0.2.1 \
  --repo sharkusmanch/romm-mcp
```

Dependency updates use the hosted [Renovate GitHub App](https://github.com/apps/renovate).
Grant that app access to this repository using its
[installation settings](https://docs.renovatebot.com/getting-started/installing-onboarding/).
The repository does not need a Renovate Actions workflow or PAT secret.

[Renovate configuration](renovate.json) uses recommended defaults, pins GitHub
Actions and Docker digests, refreshes `uv.lock` weekly before 04:00 Monday UTC,
and disables automerge. Native managers cover the Dockerfile's Python `ARG` and uv
`COPY --from`, the setup-uv workflow version, and Python dependencies/lockfile; no
custom regex manager is needed. See official [Dockerfile support](https://docs.renovatebot.com/modules/manager/dockerfile/),
[GitHub Actions inputs](https://docs.renovatebot.com/modules/manager/github-actions/#updating-with-values-in-github-actions),
and [PEP 621 lockfile support](https://docs.renovatebot.com/modules/manager/pep621/).

See [research and sources](docs/research.md) and the current
[full API design](docs/full-api-design.md). The original [design](docs/design.md)
and [implementation plan](docs/plan.md) describe the superseded v0.1.0 scope.
Licensed under MIT.
