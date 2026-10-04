# Research and implementation decisions

Researched 2026-10-03. Sources are upstream specifications, SDK documentation,
registry metadata, and RomM source. These are implementation decisions, not claims
that every possible MCP or RomM endpoint is supported.

## MCP and token efficiency

The [MCP tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
defines input/output schemas, structured results, annotations and tool-list
pagination. Tools use typed outputs and accurate read/write annotations. Stable
tool ordering supports clients that cache tool definitions. Search-result
pagination is implemented separately from MCP's tool-list pagination.

[Anthropic's tool-design guidance](https://www.anthropic.com/engineering/writing-tools-for-agents)
recommends task-oriented tools, useful descriptions, meaningful identifiers,
filtered results, pagination, bounded responses and actionable errors. This server
therefore offers a small set of game-library workflows rather than mirroring the
whole REST API. Search defaults to 20 records, with a maximum of 100, and returns
the total and next offset. Compact records retain IDs needed for follow-up calls,
names and platforms. Detailed descriptions and lists have explicit bounds; raw
provider payloads and unrelated metadata are excluded. Efficiency is a design
property tested with representative oversized data; no percentage token saving
is claimed without a measured baseline.

The [official Python SDK v2](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md)
supports current and legacy protocol revisions. Version 2.3.0 was confirmed from
[PyPI metadata](https://pypi.org/pypi/mcp/json) and pinned. Its high-level class is
`MCPServer`; old `FastMCP` imports no longer apply. Transport settings belong on
`run()` or the HTTP app builder. Python was selected for typed, concise handlers
and a shared async HTTP client; TypeScript is also an official supported option.

## Transport and security

[Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
uses one MCP endpoint. Current protocol requests are stateless; the server also
sets `stateless_http=True` to avoid sessions for legacy clients. The CLI defaults
to stdio; HTTP is selected explicitly and uses JSON responses. Local HTTP binds
to loopback by default. Containers explicitly bind to all interfaces. Host and
Origin allowlists are configured rather than disabling rebinding protection.
See the [SDK deployment guidance](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/deploy.md)
for proxy host matching and modern/legacy behavior.

The [MCP security guidance](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)
forbids unvalidated upstream-token passthrough. The incoming MCP secret is separate
from the RomM API credential and is never forwarded. HTTP requires a dedicated
static bearer token and deployment behind TLS. This is a single configured RomM
identity, not a multi-user OAuth authorization service. Upstream URL and credentials
come from operator configuration, not tool arguments. Read-only is the default;
an explicit operator setting exposes mutation tools. Errors must not leak secrets
or upstream response bodies. Liveness returns no game-library data.

## RomM API compatibility

The initial compatibility target is RomM **5.3.1**, source commit
`95599dadbe93c8f7b8a8647148fafbe293df3167`. The deployed API schema was compared
with these upstream handlers during implementation. Authentication documentation
can lag source changes, so the pinned handlers define the tested contract.

- [ROM handlers](https://github.com/rommapp/romm/blob/5.3.1/backend/endpoints/roms/__init__.py):
  filtered, offset-based searches and ROM details. Disable `with_char_index`,
  `with_filter_values`, `with_rom_id_index` and `with_files` on searches while
  retaining totals. This avoids generating unrelated indexes for agent queries.
- [ROM filter implementation](https://github.com/rommapp/romm/blob/5.3.1/backend/handler/database/rom_filters.py)
  and [response definitions](https://github.com/rommapp/romm/blob/5.3.1/backend/endpoints/responses/rom.py):
  preserve supported filter semantics and project useful fields rather than
  exposing every nested metadata provider object.
- [Collection handlers](https://github.com/rommapp/romm/blob/5.3.1/backend/endpoints/collections.py):
  membership changes use atomic POST/DELETE requests to `/collections/{id}/roms`
  with `rom_ids`. Do not replace the complete collection to add one game.
- [ROM handlers](https://github.com/rommapp/romm/blob/5.3.1/backend/endpoints/roms/__init__.py):
  personal property updates use partial PUT payloads, preserving properties
  absent from the request. Independently read back changes. Do not automatically
  retry ambiguous mutations, which could duplicate an operation.
- [Client token handlers](https://github.com/rommapp/romm/blob/5.3.1/backend/endpoints/client_tokens.py),
  [scope definitions](https://github.com/rommapp/romm/blob/5.3.1/backend/handler/auth/constants.py),
  and [authentication guide](https://docs.romm.app/5.0.0/developers/api-authentication/):
  use a dedicated scoped RomM credential. Its permissions constrain the shared
  server identity in addition to the server's read-only setting.

## Container and release provenance

[uv's Docker guidance](https://docs.astral.sh/uv/guides/integration/docker/)
supports a non-editable locked installation in an intermediate build stage,
copying only the virtual environment to a matching runtime image. This image pins
the Python 3.14 slim and uv 0.12.23 multi-platform digests. The final image runs as
UID/GID 1000, contains no uv build tool, and supports a read-only root filesystem
with writable temporary storage. CI checks Python 3.12 and 3.14 and exercises a
restricted container. Dependency locking improves repeatability; byte-for-byte
reproducible images are not claimed.

[Docker's attestation documentation](https://docs.docker.com/build/ci/github-actions/attestations/)
defines BuildKit provenance and SBOM generation. Release builds explicitly request
`provenance: mode=max` and `sbom: true`, pushed directly to GHCR for amd64 and
arm64. Each platform receives its own SBOM; the workflow does not mislabel a scan
of one platform as covering the whole index. Secrets are runtime configuration,
never build arguments.

[GitHub artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)
bind an artifact digest to its workflow identity. Tag-only releases check the tag
against the package version, then sign build provenance for the image index and
push it as an OCI referrer with `push-to-registry: true`. The wheel and source
archive also receive build attestations and become GitHub release assets. Actions
are pinned to commit SHAs resolved from their official release tags. BuildKit's
SBOM records and GitHub's signed provenance are distinct artifacts.

Verify the published image, using its immutable digest:

```sh
gh attestation verify oci://ghcr.io/sharkusmanch/romm-mcp@sha256:<digest> \
  --repo sharkusmanch/romm-mcp
```

Verification must validate the signer and repository, not merely the existence
of an attestation. Production admission additionally restricts the release
workflow/tag identity.
