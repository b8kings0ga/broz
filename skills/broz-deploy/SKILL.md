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
4. Treat a revision as prewarmed only after the worker has emitted the exact revision's `prepared` event. Preparation also reserves one short-lived, allocation-bound activation ticket and warms the service's protected SlotSupervisor endpoint. The deploy command must still freeze and compare the current manifest. Never activate a stale or superseded receipt.
5. A prepared worker may use `activation_transport=node_direct`: it sends the pre-authorized ticket through the service's public hostname, then durably queues the slower Membership state reconciliation. If the ticket is absent, expired, rejected, or its response is uncertain, the helper continues the exact same deployment ID through Membership; it must not create a second deployment. Direct fallback without a prepared worker retains the central path.
6. Treat node or Membership `activated` as slot activation only. Independently fetch the public homepage; success means the complete page returned HTTP 200 and `X-Mim-Deployment` exactly matched this deployment. A public verification failure after activation is a deployment failure report, but must never trigger a cold fallback or a second deployment. Report the final JSON and public URL, identify `hot`, `cold`, or `cold_fallback`, preserve all residual preparation in `command_total_ms`, and expose reconciliation only as `queued` or `committed` without printing tickets or receipts.
7. Do not claim a 1-second or 10-second SLA. You may report measured percentiles only with sample count and prepared/cold conditions, and must preserve slower attempts when evaluating reliability.

If the host has an active system VPN and the user explicitly wants a physical-network benchmark, discover the intended interface and pass `--direct-interface INTERFACE` to both `prepare --watch` and later commands. Do not silently bypass a VPN for ordinary deployments.

For `status` and `stop`, run `bash scripts/broz-deploy.sh` with the corresponding command against the project path. `stop` is reversible.

Deletion is destructive. Obtain explicit confirmation immediately before running `bash scripts/broz-deploy.sh delete PATH --yes`. Never add `--yes` without that confirmation. Successful deletion shuts down the exact project worker, removes project/cache state, and removes only a project-scoped guest credential; named account profiles remain.

If hot activation times out or its response is lost, inspect the deployment/slot state before any cold fallback. Never start a second deployment when the slot may already have switched. Never display credential files, authorization headers, raw environment variables, capabilities, prepared receipts, or debug traces containing secrets. Do not add GitHub, Docker builds, production-service cleanup, or unrelated infrastructure operations to a deployment.
