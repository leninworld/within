# Within #426 — retained Docker validation results

Recorded from the [completed Docker job](https://github.com/leninworld/within/actions/runs/41223853691/job/124603800088) on October 31, 2026. This is a summary of recorded results, **not a fresh execution or terminal capture**.

| Validation | Recorded result |
|---|---|
| Full Rust tests, including doctests | **573 passed, 0 failed, 1 existing ignored** |
| Rebuilt Python extension and full Python suite | Passed; numeric pytest total not retained |
| Line coverage | **97.28%** (94% required) |
| Default, 9 positive-wire cases and zero-deserialization regression | Passed |
| Existing v21 decode-and-solve; v20/v19/v12 rejection controls | Passed |
| Fmt, Clippy, MSRV 1.85, deny, semver, release wheel/sdist, installed API demo, benchmark smoke | Passed |

## API behavior recorded inside Docker

The public Rust example constructs `ApproxSchurConfig` with `NonZeroU32::new(2).unwrap()`; its compiling example and otherwise-identical zero-literal compile-fail example both passed. The old base fails the exact zero-deserialization assertion; the candidate rejects zero.

The real Python witness constructed `ApproxSchurConfig(seed=17, split=2)`, built and applied its Schur preconditioner, then pickled and reused it in a solver. `ApproxSchurConfig(split=0)` raised the existing `ValueError: split must be >= 1`.

```json
{
  "zero_rejected": true,
  "positive_getters": [
    1,
    2,
    4294967295
  ],
  "read_only": true,
  "tuned_schur": "Schur.approximate(seed=17, split=2)",
  "pickle_and_solver_reuse": "PASS",
  "old_positive_pickle": "COMPATIBLE"
}
```

Native Linux ARM64, Python 3.12.16, Rust 1.97.0. This does not claim Windows, macOS, free-threaded Python or the full wheel matrix.

Source base: `7d9e6e28686df0007489d74edd653364e8f7e912`.
Full runtime-tested patch SHA256: `2a7dab1405093ae26b018f42a9d60d93f671a987271c412eb8d2785af162617d`.

A single assertion-wrap change in the publishable Python test is AST-identical to the tested file. Those formatted bytes are not described as fully runtime-tested. Ruff 0.9.10 checks are a separate supplemental lane; their result is not included in the full-job totals above.
