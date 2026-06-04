# Contributing to Tmux Workers

## Branch Model

```
main          ← production releases, tagged
develop       ← default, all PRs target here
release/*     ← milestone cuts from develop
hotfix/*      ← emergency fixes → main + develop
feature/*     ← card-level work from develop
```

## Standards

- Commits: [Conventional Commits](https://www.conventionalcommits.org/)
- No secrets or credentials in any file
- Tmux sessions: named `tw_<card-id>_<n>`

## Before Opening a PR

1. Update `CHANGELOG.md` under `[Unreleased]`
2. Run `python -m tw.health` if available