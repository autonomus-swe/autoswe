# node-fixture

A tiny zero-dependency Node package used by autoswe's sandbox tests — the Node
counterpart of `fixture_repo/`. `src/ops.js` exports `add`, `subtract` and
`slugify`; `test/` holds four passing tests split across the two shapes a real
repository uses: bare `test()` calls (`ops.test.js`) and a `describe()/it()`
block (`slugify.test.js`), because the JUnit reporter emits them differently.

Everything below was verified in `agent-sandbox:node-20` (Node v20.20.2, npm
10.8.2) with a read-only rootfs, `--user 1234:1234`, `--network none`.

## Install

Nothing to install — the test runner and the JUnit reporter both ship with
Node 20 — but the command still has to exit 0 offline. With the committed
lockfile:

    npm ci --no-audit --no-fund

`npm install --no-audit --no-fund --prefer-offline` also exits 0 and leaves the
lockfile untouched. Without a committed lockfile `npm install` *writes* one into
the workspace, so the lockfile is committed on purpose. Neither command creates
`node_modules` for a zero-dependency project.

## Test

From the repo root:

    node --test --test-reporter=junit --test-reporter-destination=.autoswe/report.xml test/

A selector is a path `node --test` accepts and replaces `test/`:

    node --test --test-reporter=junit --test-reporter-destination=.autoswe/report.xml test/slugify.test.js

Exit code is 0 when everything passes and 1 when any test fails; the report is
written in both cases.

## What the harness has to know

- **`.autoswe/` is not created by the reporter.** If the directory is missing,
  the process dies with `Error: ENOENT ... open '.autoswe/report.xml'`, exit
  code **7**, and no report at all. If it exists but is not writable by the
  container uid the same thing happens with `EACCES`. Create it before the run.
- **The file and line of a failure live in the `<failure>` element body**, never
  in an attribute: `at TestContext.<anonymous> (file:///workspace/test/ops.test.js:11:10)`.
  `classname` is the constant `"test"` for every test in every file, and a
  passing `<testcase>` carries no file information at all.
- **Prefer the body over the `message` attribute.** The attribute has every
  newline deleted (`"...strictly equal:2 !== 3"`) and double-escapes quotes
  (`"` becomes the literal text `&quot;`). The body is escaped correctly.
- **A module that fails to load reports nothing useful.** The `<testcase name>`
  becomes the absolute file path and the whole failure is
  `message="test failed"` with no stack — and with the JUnit reporter alone the
  real `ERR_MODULE_NOT_FOUND` appears on neither stdout nor stderr. Adding
  `--test-reporter=spec --test-reporter-destination=stdout` alongside the JUnit
  pair is what puts it back in the captured output.
