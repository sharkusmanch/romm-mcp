# Full HTTP and realtime API coverage

This design supersedes the eight-tool scope in [design.md](design.md) and
[plan.md](plan.md). Those documents remain historical records of v0.1.0.
[README.md](../README.md) is the current operator and client guide.

Extend the compact tools with lazy discovery and schema-guided invocation of all
246 HTTP operations in the configured RomM 5.3.1 OpenAPI document. Fetch the schema
from the configured origin and cache it for the process. Expose operation IDs,
required scopes, additive switches, and request schemas on demand; accept neither
arbitrary target URLs nor caller-chosen URL paths. Preserve the compact tools.
The MCP surface has ten tools with writes disabled and fourteen with writes enabled.

All environment gates default false. `ROMM_ALLOW_WRITES` controls state changes;
`ROMM_ALLOW_DESTRUCTIVE` controls deletes, replacement-capable uploads, regeneration,
and destructive task paths; `ROMM_ALLOW_ADMIN` controls administrative data and
operations; `ROMM_ALLOW_AUTH` controls identity/token workflows and alternate auth;
`ROMM_ALLOW_FILES` controls file transfers; `ROMM_ALLOW_TASKS` controls task endpoints,
exports, streaming mutations, and socket sends; `ROMM_ALLOW_REALTIME` enables socket
connections. Gates are additive and checked on every call. Upstream scopes and roles
remain independent requirements. Discovery includes disabled operations and switches.

Separate generic read/write tools provide appropriate MCP annotations. HTTP methods
alone do not determine safety: OpenID GETs, memory-card version listing, task status,
and conditional save-content sync updates require the write interface. Support JSON,
form, multipart and raw byte bodies, repeated query values, declared headers,
operator-configured Basic auth, and isolated opaque HTTP cookie sessions. Report
text, JSON, binary downloads, empty responses, and redirects without following them.
Generic writes return `verification=not_performed`; compact writes retain independent
readback. Never automatically retry an ambiguous mutation.

Responses support JSON Pointer selection and bounded local pages, with explicit
truncation and continuation. Upstream pagination remains an independent query option.
Files stream to private temporary artifacts, then move in bounded base64 chunks.
No caller chooses a filesystem path. Aggregate storage is configurable with
`ROMM_MAX_TRANSFER_BYTES` (default 64 MiB) and the parent directory with
`ROMM_TRANSFER_DIRECTORY`. At most sixteen artifacts are retained; TTL is fixed at
3,600 seconds in the server, not exposed through an environment variable. Active
upload leases retain quota and artifact slots through expiry, block mutation, and
close before deletion. Failed writes retain reserved quota until cleanup succeeds.

The additional realtime surface covers all thirteen callable Socket.IO events:
five main events for scans/activity and eight netplay events for rooms, signaling,
and game data. One `romm_socket` tool provides catalog/open/send/listen/close actions.
Main connections require the configured `ROMM_SESSION_COOKIE`; bearer authentication
alone is unsupported by RomM 5.3.1's main socket. Sending requires realtime, writes,
and tasks; starting a scan also requires files and destructive access. Log capture
additionally requires admin. Four sessions, five-minute
lifetimes, bounded event queues, short listen waits, and binary base64 wrappers keep
resource use bounded. Sending confirms transport acceptance, not application success.

Validation covers route completeness, undocumented upload corrections, serialization,
safety bypasses, bounded responses and artifact lifetimes, both MCP transports, and
realtime behavior. Release v0.2.0 through the existing GHA image/SBOM/provenance flow;
deploy a pinned image through Flux and verify the games-project clients. Keep
upstream token scopes and deployment switches aligned with intended capabilities.
