# RomM MCP design

> Historical v0.1.0 scope, superseded by [full-api-design.md](full-api-design.md).

Build an independent MIT-licensed Python server with the official MCP 2.3 SDK,
httpx and Pydantic. Default local transport is stdio; HTTP is stateless Streamable
HTTP on /mcp, with explicit host/origin validation and a dedicated bearer token.
Upstream credentials are operator configuration, never client-supplied tool inputs.
The initial compatibility target is RomM 5.3.1.

Eight tools cover platform listing, ROM search/detail, collection listing/detail,
private collection creation, atomic membership changes, and personal progress updates.
Read-only is the default; an explicit operator flag registers the three mutation tools.
Search defaults to 20, max 100, offset pagination with total and next_offset. Disable
RomM's character/filter/ROM-ID indexes; project summaries and bounded detail sections.
Do not expose arbitrary API requests, scans, filesystem uploads/downloads/deletions,
or administration. Verify every write via a separate GET. Never retry writes after
an uncertain outcome; return a useful verification warning instead.

HTTP is a single configured RomM identity behind TLS termination, using static bearer
authentication (not a complete OAuth authorization server). Separate upstream and MCP
secrets. Fail closed without authentication. Non-root image, read-only filesystem,
restricted network policy, no Kubernetes API token or persistent storage. Health is
process liveness only, without library information.

Publish reproducible locked builds for amd64/arm64 using GitHub Actions, SHA-pinned
actions, GHCR and signed build provenance. Tag matches package version; CI verifies
both transports and security/error/pagination behavior. Deploy a pinned image through
Flux; configure the games project in Claude and Codex with untracked credentials.
