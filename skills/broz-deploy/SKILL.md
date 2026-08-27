---
name: broz-deploy
description: Deploy, publish, update, inspect, stop, or delete Bun, Python, and prebuilt Linux amd64 services on Broz Membership/Nomad. Use when a user asks to put local code online at a Broz public URL, redeploy a Broz project, choose a `NAME-run.broz.uk` hostname, check its state, stop it, or remove it.
---

# Broz Deploy

Run the installed script as `bash scripts/broz-deploy.sh`; installers may not preserve its executable bit. Do not reimplement the API flow or request a token from the user.

For deployment:

1. Read `references/runtime-contracts.md` for the selected runtime.
2. Run `bash scripts/broz-deploy.sh deploy PATH` with `--domain NAME` when the user requests a hostname. Runtime auto-detection is preferred.
3. Report the final JSON and public URL. Success means the complete homepage returned HTTP 200 and `X-Mim-Deployment` exactly matched this deployment.
4. Do not claim a 10-second SLA. You may state whether this measured run was within 10 seconds.

For `status` and `stop`, run `bash scripts/broz-deploy.sh` with the corresponding command against the project path. `stop` is reversible.

Deletion is destructive. Obtain explicit confirmation immediately before running `bash scripts/broz-deploy.sh delete PATH --yes`. Never add `--yes` without that confirmation. Successful deletion removes the project's `.broz.json` and its local guest credential.

Never display credential files, authorization headers, raw environment variables, or debug traces containing secrets. Do not add GitHub, Docker builds, dependency installation, database migrations, production-service cleanup, or unrelated infrastructure operations to a deployment.
