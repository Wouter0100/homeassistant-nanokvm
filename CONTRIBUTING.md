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
- Update README, SERVICES, strings, and translations together when behavior,
  services, entities, or user-facing text changes.
- Add tests for bug fixes and new behavior when practical.

## Validation

Before opening a PR, run:

1. `python -m ruff check custom_components/nanokvm`
2. `python -m py_compile custom_components/nanokvm/*.py`
3. `python -m json.tool hacs.json`
4. `python -m json.tool custom_components/nanokvm/manifest.json`
5. `python -m json.tool custom_components/nanokvm/strings.json`
6. `python -m json.tool custom_components/nanokvm/translations/en.json`
7. `python -m json.tool custom_components/nanokvm/translations/fr.json`
8. `python -m json.tool custom_components/nanokvm/translations/pt-BR.json`

When behavior changes, also test the integration on a Home Assistant instance.

CI must pass on the PR branch:

- `.github/workflows/hacs.yaml`
- `.github/workflows/hassfest.yaml`

## Commit and PR Guidance

- Use clear commit messages (e.g., `fix: ...`, `feat: ...`, `docs: ...`).
- In the PR description, include:
  - what changed
  - why it changed
  - how it was validated
