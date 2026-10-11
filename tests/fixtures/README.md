# Upstream route inventories

`romm-5.3.1-operations.json` records OpenAPI operation metadata from RomM 5.3.1.

`romm-5.4.0-routes.json` records 280 HTTP routes derived from official RomM tag
5.4.0, commit `68f71259f7c6de39fbeedff9cc9ac114c18ee250`. Router prefixes and includes,
route decorators, tags and Scope enum values were extracted from the Python AST,
without starting upstream services. Operation IDs follow FastAPI's generated name
format. This is an inventory, not a complete schema: parameter/request/response
validation uses the live OpenAPI document. The ten hidden RetroArch WebDAV methods
are excluded upstream and covered separately by the MCP's explicit supplement.

Source: https://github.com/rommapp/romm/tree/5.4.0/backend/endpoints
