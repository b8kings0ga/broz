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

The script can also be run directly:

```bash
bash skills/broz-deploy/scripts/broz-deploy.sh deploy . --domain my-demo
```

It packages locally, uploads an artifact, creates or reuses one isolated guest service, deploys it, downloads the complete public homepage, verifies HTTP 200 and the exact `X-Mim-Deployment` header, then opens the URL. A single successful run is a measurement, not a 10-second SLA.

See [runtime contracts](skills/broz-deploy/references/runtime-contracts.md) for the accepted project layouts.

## Local state and privacy

- `.broz.json` contains only non-secret project and service identifiers.
- The high-entropy guest credential is stored at `~/.config/broz/credentials/<project-id>.json` with mode `0600`.
- Credentials are never passed on the command line or printed.
- Guest previews are free, limited, and remain until explicitly deleted.

## License

MIT
