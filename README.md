# RomM MCP

A small MCP server for a self-hosted RomM library. Runs locally over **stdio** or
as an authenticated **Streamable HTTP** service. Tested with RomM **5.3.1**.

## Tools

| Tool | Purpose |
|---|---|
| `list_platforms` | Find platform IDs and library counts |
| `search_roms` | Search by title, platform, collection, and RetroAchievements availability |
| `get_rom` | Bounded synopsis; opt into metadata, files, or personal progress |
| `list_collections` | Find regular collections by name |
| `get_collection` | Collection details and paginated contents |
| `create_collection` | Create a private collection, verified by readback |
| `update_collection_roms` | Atomically add/remove membership, verified by readback |
| `update_rom_progress` | Change only supplied progress fields, verified by readback |

The last three tools are registered only with `ROMM_ALLOW_WRITES=true`. No ROM
file deletion, upload, download, scans, administration, or arbitrary API requests.
All clients share the configured RomM identity; this is a single-user gateway.

Searches default to 20 results (maximum 100). Follow `next_offset` until null;
`get_collection` uses `roms.next_offset`. Lists of platforms and collections are
filtered/paged locally because their upstream endpoints return full arrays.
Searches suppress RomM's full-library character, filter, and ROM-ID indexes.
Responses project useful fields instead of returning provider blobs. Detail
synopses are capped at 1,500 characters and optional file lists at 50 entries,
with `truncated_fields` identifying those truncations. Summary names cap at 200
characters, regions/languages at ten values each. No entire library is cached.

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

Supply credentials through your local secret manager or protected environment,
not a committed configuration. `ROMM_URL` is the base URL **without `/api`**.
To enable the mutation tools, set `ROMM_ALLOW_WRITES=true` explicitly.

Create a RomM client token as the user whose collections/progress you intend to
manage, using RomM's UI or `POST /api/client-tokens`. Read-only scopes:
`me.read`, `roms.read`, `platforms.read`, `collections.read`, `roms.user.read`.
For writes, additionally grant `collections.write` and `roms.user.write`.
The bootstrap identity needs `me.write` to create the token; the server does not.
Service accounts have their own collections and progress.

## HTTP / containers

```sh
export MCP_AUTH_TOKEN=YOUR_SEPARATE_RANDOM_TOKEN_AT_LEAST_32_CHARACTERS
export MCP_ALLOWED_HOSTS=localhost:8080,127.0.0.1:8080
uv run romm-mcp --transport http --host 127.0.0.1 --port 8080

# Published multiarchitecture image (HTTP on port 8080 by default):
docker run --rm --read-only --tmpfs /tmp -p 127.0.0.1:8080:8080 \
  -e ROMM_URL -e ROMM_TOKEN -e MCP_AUTH_TOKEN -e MCP_ALLOWED_HOSTS \
  ghcr.io/sharkusmanch/romm-mcp:v0.1.0

# Container as a local stdio server (no port publication or HTTP token needed):
docker run --rm -i --read-only --tmpfs /tmp -e ROMM_URL -e ROMM_TOKEN \
  ghcr.io/sharkusmanch/romm-mcp:v0.1.0 --transport stdio
```

Connect to `/mcp` with `Authorization: Bearer <MCP_AUTH_TOKEN>`. Static bearer
access is **not** a complete OAuth authorization server. TLS termination belongs
at your reverse proxy. HTTP refuses to start without a token of at least 32
non-whitespace ASCII characters. The incoming token is never forwarded to RomM.
`MCP_ALLOWED_HOSTS` is a comma-separated explicit Host allowlist; include ports
when used. Loopback defaults accept any port. Requests with an Origin header are
rejected unless that origin appears in `MCP_ALLOWED_ORIGINS`; native MCP clients
normally send no Origin. `/healthz` is unauthenticated process liveness only.

Environment also supports `MCP_TRANSPORT`, `MCP_HOST`, and `MCP_PORT`; CLI flags
win. Native execution defaults to stdio, HTTP binds loopback unless overridden.
The image runs as UID/GID 1000. It needs no database, volume, or Kubernetes access.

## Client configuration

Claude Code supports a project-local HTTP server with a `headersHelper` returning
an Authorization JSON object; Codex supports project `.codex/config.toml` with
`http_headers_helper`. Use a helper that reads a protected token file, or use the
clients' environment-backed header options. See [Claude MCP configuration](https://code.claude.com/docs/en/mcp)
and [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
For stdio, use command `uv`, args `run --directory /path/to/romm-mcp romm-mcp
--transport stdio`, and provide `ROMM_URL`/`ROMM_TOKEN` through the client environment.

## Writes and errors

Membership changes use RomM's atomic endpoints and do not replace other members.
Progress statuses are `incomplete`, `finished`, `completed_100`, `retired`, or
`never_playing`; `backlogged` and `now_playing` are separate flags. `status: null`
clears status. Ratings/difficulty range 0–10; completion ranges 0–100. Unknown
fields, empty changes, and invalid values are rejected.

The server independently reads writes back. It never automatically retries a
write. If a write or verification fails after dispatch, the outcome is reported
as uncertain: inspect the target before retrying. Concurrent outside edits can
still race the readback; RomM does not offer an ETag precondition for these calls.
Private collection names are checked per owner to reduce accidental duplicates.
RomM content is untrusted data and must not be treated as instructions.

## Development and releases

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build --no-sources
```

Tests include real SDK clients over stdio and TCP HTTP, bounded results,
authentication, Host/Origin enforcement, partial/atomic mutations, and uncertainty.
CI tests Python 3.12 and 3.14 and builds/runs the restricted container. Tags must
match the package version. Tag releases publish amd64/arm64 images, per-platform
BuildKit SBOMs, GitHub-signed build provenance as OCI referrers, and attested wheel
and source archives. Actions and build images are pinned to immutable digests.

```sh
gh attestation verify oci://ghcr.io/sharkusmanch/romm-mcp:v0.1.0 \
  --repo sharkusmanch/romm-mcp
```

See [research and sources](docs/research.md), [design](docs/design.md), and
[implementation plan](docs/plan.md). Licensed under MIT.
