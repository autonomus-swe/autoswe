# Fixture repository

`fixture_repo/` is the template for the end-to-end target repository. The tests use it in
two ways:

- **Local end-to-end** (`tests/e2e/test_m1_local.py`): copied into a throwaway git repo on
  disk, so the whole loop runs with no GitHub account and no network.
- **GitHub end-to-end** (`tests/e2e/test_m1_github.py`): push this content to a repository
  of your own and set `AUTOSWE_FIXTURE_REPO` to its URL.

The goal used in both cases:

> Implement subtract(a, b) and slugify(text) in fixture/ops.py so that tests/test_ops.py
> passes. Do not change the tests.
