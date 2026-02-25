# Contributing

Thanks for contributing to the NanoKVM Home Assistant integration.

## Branching and PR Targets

- Open feature/fix PRs **against `dev`**.
- `main` is reserved for release-ready merges only.
- If a PR is opened against `main`, maintainers may retarget it to `dev`.

Recommended flow:

1. Create a branch from `dev`.
2. Open PR into `dev`.
3. After validation and review, changes are merged to `dev`.
4. Release PRs merge `dev` into `main`.

## Development Notes

- Keep changes focused and small when possible.
- AI-assisted coding is welcome, but contributors are responsible for
  understanding, reviewing, and validating generated changes.
- Update docs/translations when behavior or user-facing text changes.
- Add tests for bug fixes and new behavior when practical.

## Validation

Before opening a PR, run:

1. `ruff check custom_components/nanokvm`
2. Local Home Assistant smoke test (if relevant)

CI must pass on the PR branch:

- `.github/workflows/hacs.yaml`
- `.github/workflows/hassfest.yaml`

## Commit and PR Guidance

- Use clear commit messages (e.g., `fix: ...`, `feat: ...`, `docs: ...`).
- In the PR description, include:
  - what changed
  - why it changed
  - how it was validated
