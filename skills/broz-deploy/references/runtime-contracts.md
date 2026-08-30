# Broz runtime contracts

All services must listen on `0.0.0.0:$PORT`, run in the foreground, provide `GET /` and `GET /healthz`, and handle `SIGTERM`. The platform injects `MIM_SERVICE_ID`, `MIM_DEPLOYMENT_ID`, and `PORT`.

## Bun

The project must contain `package.json`, `bun.lock` or `bun.lockb`, and `scripts.start`. Source is uploaded without `node_modules`; the lockfile is not modified.

## Python

The project must contain `pyproject.toml`, `uv.lock`, and a `mimir-service = "module:function"` entry point. Fast preparation uploads deterministic source files plus a generated launcher and prepares an immutable dependency layer from the locked environment. Pure Python and compatible Linux wheels use the hot path; unavailable system build requirements make the revision fast-path ineligible and preserve cold fallback. The legacy cold path invokes the existing `uv` executable to build exactly one wheel. Neither path modifies the lockfile.

## Binary

Pass `--binary FILE --arch amd64`. The file must already be an amd64 Linux ELF executable. The watcher zstd-compresses it, records compressed transport and expanded content SHA-256 digests, and uploads it as `/service` with mode `0755`; the node verifies the ELF and both digests before marking the revision prepared. Version 1 does not build binaries and does not support arm64. Static linking is recommended.

## Packaging safety

`.git`, `.broz.json`, `.env*`, `node_modules`, caches, sockets, devices, pipes, private keys, SSH/AWS/npm credentials, and known credential directories are never uploaded. `.brozignore` adds exclusions; it cannot re-include a protected path.
