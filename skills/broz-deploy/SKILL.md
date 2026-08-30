---
name: broz-deploy
description: Deploy, publish, update, inspect, stop, or delete Bun, Python, and prebuilt Linux amd64 services on Broz Membership/Nomad. Use when a user asks to put local code online at a Broz public URL, redeploy a Broz project, choose a `NAME-run.broz.uk` hostname, check its state, stop it, or remove it.
---

# Broz Deploy

Run the installed script as `bash scripts/broz-deploy.sh`; installers may not preserve its executable bit. Do not reimplement the API flow or request a token from the user.

For deployment:

1. Read `references/runtime-contracts.md` for the selected runtime.
2. When an enabled Membership profile exists and the user begins coding, run `bash scripts/broz-deploy.sh prepare PATH --profile PROFILE --watch --no-open`. Tell the user once that fast mode keeps the service slot running and is billed by running minute. Do not wait until the final deploy command to start preparation.
3. Run `bash scripts/broz-deploy.sh deploy PATH --profile PROFILE` with `--domain NAME` when requested. Without a named profile, retain the compatible guest cold-deploy flow. Runtime auto-detection is preferred.
4. Treat a revision as prewarmed only after the worker has emitted the exact revision's `prepared` event. The deploy command must still freeze and compare the current manifest. Never activate a stale or superseded receipt.
5. Report the final JSON and public URL. Success means the complete homepage returned HTTP 200 and `X-Mim-Deployment` exactly matched this deployment. Report `hot`, `cold`, or `cold_fallback` and preserve all residual preparation in `command_total_ms`.
6. Do not claim a 1-second or 10-second SLA. You may report measured percentiles only with sample count and prepared/cold conditions, and must preserve slower attempts when evaluating reliability.

If the host has an active system VPN and the user explicitly wants a physical-network benchmark, discover the intended interface and pass `--direct-interface INTERFACE` to both `prepare --watch` and later commands. Do not silently bypass a VPN for ordinary deployments.

For `status` and `stop`, run `bash scripts/broz-deploy.sh` with the corresponding command against the project path. `stop` is reversible.

Deletion is destructive. Obtain explicit confirmation immediately before running `bash scripts/broz-deploy.sh delete PATH --yes`. Never add `--yes` without that confirmation. Successful deletion shuts down the exact project worker, removes project/cache state, and removes only a project-scoped guest credential; named account profiles remain.

If hot activation times out or its response is lost, inspect the deployment/slot state before any cold fallback. Never start a second deployment when the slot may already have switched. Never display credential files, authorization headers, raw environment variables, capabilities, prepared receipts, or debug traces containing secrets. Do not add GitHub, Docker builds, production-service cleanup, or unrelated infrastructure operations to a deployment.
