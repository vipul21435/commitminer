# Classifier sample tree

A tiny, original multi-language tree for `commitminer classify` (`make demo-classify`).
Each file exercises one kind of rule: a Rust module with an in-file `#[cfg(test)]` module,
a Go file with a generated-code header, a minified JavaScript bundle without a `.min.js`
name, a TypeScript test, a vendored Go package, and a fixture that only
[`commitminer.toml`](commitminer.toml) marks as test data. The same file also disables the
built-in `tooling-dir` rule, so `tools/cli/main.go` counts as source here.
