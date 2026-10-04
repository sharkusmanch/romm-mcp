# RomM MCP implementation plan

Goal: researched, reviewed server published and running in the cluster, usable by both games clients.
Spec: design.md. Execution: autonomous native implementation with independent subagent review.

1. Add failing tests for API filtering, pagination, response bounds and sanitized errors;
   implement config, typed projections and async client. Verify with pytest.
2. Add failing write tests for atomic membership, partial props, independent readback and
   uncertainty; implement opt-in writes. Verify preserved unrelated state and read-only mode.
3. Add failing transport tests for stdio/HTTP discovery and HTTP auth/Host/Origin rejection;
   implement MCP registration, CLI and liveness. Verify real SDK clients on both transports.
4. Add Dockerfile, locked dependencies, CI/release with signed image provenance, README and
   research sources. Review source and deployment manifests independently; fix findings.
5. Create GitHub repo, publish v0.1.0 and verify image attestations. Deploy Flux manifests with
   ESO credentials, network policy and monitoring. Verify actual calls from HTTP and clients.

Review focus: empty/out-of-range pages; giant metadata; expired credentials/HTML upstream;
ambiguous write outcomes; hostile HTTP host/origin and caller/upstream credential separation.
