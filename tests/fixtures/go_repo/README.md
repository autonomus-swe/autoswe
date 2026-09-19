# gofixture

A tiny dependency-free Go module used by autoswe's Go harness tests.
`ops/ops.go` has `Add`, `Subtract` and `Slugify`; `ops/ops_test.go` covers all three
and passes.

Install (network phase): `go mod download`
Test (offline):          `gotestsum --junitfile .autoswe/report.xml -- ./...`
With a selector:         `gotestsum --junitfile .autoswe/report.xml -- ./ops/...`
