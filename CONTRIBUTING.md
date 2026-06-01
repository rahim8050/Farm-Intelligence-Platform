# Contributing

Thank you for taking the time to improve Weather APIs.

## Scope

This repository is a Django + DRF API. Contributions should keep the existing
API versioning, auth model, and response envelopes stable unless a change is
explicitly requested.

## Before you start

- Read the root [README.md](README.md).
- Read the app README for the area you want to change.
- Check [TESTING.md](TESTING.md) for the local test commands.
- Review the repository [docs/README.md](docs/README.md) for the docs layout.

## Local workflow

1. Create a branch.
2. Make the smallest change that solves the problem.
3. Add or update tests when behavior changes.
4. Run the quality checks listed in [README.md](README.md).
5. Update docs when the public API, env vars, or setup steps change.

## Documentation expectations

- Keep endpoint docs aligned with the actual response envelope.
- Document new env vars in the root README or the relevant app README.
- Add new public docs to [docs/README.md](docs/README.md).

## Pull requests

- Prefer focused PRs with one purpose.
- Include a short summary of behavior changes.
- Call out any backward-incompatible API or configuration changes clearly.

## Questions

If something in the repo is unclear, open an issue or start with the smallest
possible docs-only change so the gap is visible.
