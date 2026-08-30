# Broz Deploy

Deploy a small Bun, Python, or prebuilt Linux amd64 service to a public `*.run.broz.uk` URL without copying an API token into your project or shell history.

## Install the Codex Skill

```bash
python3 "$CODEX_HOME/skills/.system/skill-installer/scripts/install-skill-from-github.py" \
  --repo b8kings0ga/broz --path skills/broz-deploy
python3 "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" \
  "$CODEX_HOME/skills/broz-deploy"
```

Restart Codex after installation. You can then ask: “Deploy this project to Broz with the domain `my-demo`.”

The script can also be run directly. Guest deployments retain the compatible
cold path:

```bash
bash skills/broz-deploy/scripts/broz-deploy.sh deploy . --domain my-demo
```

For an enabled Membership profile, start asynchronous preparation while coding:

```bash
bash skills/broz-deploy/scripts/broz-deploy.sh prepare . --profile PROFILE --watch --no-open
bash skills/broz-deploy/scripts/broz-deploy.sh deploy . --profile PROFILE
```

The watcher uses latest-wins revisions and prepares the exact source manifest,
dependency layer, node CAS, persistent slot, route and public connections ahead
of `deploy`. Bun and Python upload deterministic file manifests; an amd64 Linux
binary is zstd-compressed, uploaded with transport/content digests, and verified
after node-side decompression. The deploy command freezes the source again and
uses the hot path only when its receipt exactly matches the manifest, dependency
digest and slot incarnation. Otherwise the remaining preparation is included in
the command time, or the existing cold path is used when the hot path explicitly
fails before switching.

Success always requires the complete public homepage to return HTTP 200 with
`X-Mim-Deployment` exactly equal to the new deployment. A successful sub-second
sample is a measurement under prepared conditions, not an SLA. The JSON reports
`mode`, preparation/activation stage timings, `command_total_ms`, and
`within_1s`. The older guest path continues to report its upload/deploy timings
and `within_10s`.

See [runtime contracts](skills/broz-deploy/references/runtime-contracts.md) for the accepted project layouts.

## Local state and privacy

- `.broz.json` contains only non-secret project and service identifiers.
- Named account credentials live only in `~/.config/broz/profiles/`; prepared
  receipts, worker metadata and logs live under `~/.cache/broz/`, all private.
- The high-entropy guest credential is stored at `~/.config/broz/credentials/<project-id>.json` with mode `0600`.
- Credentials are never passed on the command line or printed.
- Guest previews are free, limited, and remain until explicitly deleted.
- Fast mode keeps a slot running and is billed for its running minutes. `stop` is
  reversible; `delete --yes` removes the service and project-local worker state.

## License

MIT
