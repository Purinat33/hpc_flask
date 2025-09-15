# Software Metrics (LOC, SLOC, Complexity, Halstead, Coverage)

> What each metric means, how we measure it in this repo, and the guardrails (thresholds) we use.

---

## 1) What these metrics mean

| Metric                             | Plain-English meaning                                                                                     | Canon / formula (when applicable)                                                                                                                                                                              |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Lines**                          | All physical lines in files (including blanks & comments).                                                | —                                                                                                                                                                                                              |
| **LOC**                            | Non-blank, non-comment lines.                                                                             | From tools like `cloc` (a.k.a. _code_ lines).                                                                                                                                                                  |
| **SLOC**                           | Logical “statements” (close to executable statements).                                                    | Tool-dependent; `lizard`/`radon raw` approximate.                                                                                                                                                              |
| **Cyclomatic Complexity (CC)**     | Number of independent paths through a function → how much branching to test.                              | McCabe: **`M = E − N + 2P`** (edges, nodes, connected components). Practically: +1 per `if/for/while/and/or/except/case`.                                                                                      |
| **MaxNest**                        | Maximum nesting depth of blocks in a function.                                                            | Count of nested `if/for/while/try` etc.                                                                                                                                                                        |
| **Halstead (H)**                   | Measures _operators/operands_ usage to estimate cognitive effort.                                         | Let `n1, n2` be unique operators/operands; `N1, N2` their totals. Vocabulary `n = n1 + n2`; length `N = N1 + N2`; **Volume `V = N * log2(n)`**; **Difficulty `D = (n1/2) * (N2/n2)`**; **Effort `E = D * V`**. |
| **Maintainability Index (MI)**     | Composite health score (higher is better).                                                                | One common form: `MI = 171 − 5.2*ln(V) − 0.23*CC − 16.2*ln(LOC)` scaled to 0–100.                                                                                                                              |
| **Branch coverage**                | % of decision branches taken at least once by tests.                                                      | `pytest-cov --cov-branch`.                                                                                                                                                                                     |
| **Line coverage**                  | % of executed lines.                                                                                      | `pytest-cov`.                                                                                                                                                                                                  |
| **MCDC**                           | Modified Condition/Decision Coverage (each boolean sub-condition independently affects decision outcome). | Stronger than branch coverage; requires targeted test design or specialized tooling.                                                                                                                           |
| **CBO (Coupling Between Objects)** | How many other classes/modules a class depends on.                                                        | For Python we approximate via import/call coupling (see §4 optional tools).                                                                                                                                    |

> Note: Pure OO metrics like _AvgMethod/ChildNumber/MaxInheritanceDepth_ are less informative in our Python/Flask codebase but can be approximated with `pylint` or `lizard` if needed.

---

## 2) How we measure them (commands)

Run from repo root (use a venv):

```bash
pip install -U pytest pytest-cov coverage[toml] radon xenon lizard cloc diff-cover genbadge[coverage]
mkdir -p .reports
```

**Size (Lines/LOC/SLOC)**

```bash
cloc --exclude-dir=.git,env,venv,dist,build,docs,instance --include-lang=Python . \
  | tee .reports/cloc.txt
```

**Complexity & Maintainability**

```bash
# Cyclomatic complexity per function/class, with average
radon cc -s -a -n --json . > .reports/radon-cc.json

# Maintainability Index per module
radon mi -s --json . > .reports/radon-mi.json

# Halstead metrics per module
radon hal -s --json . > .reports/radon-halstead.json

# Secondary lens: SLOC, parameters, MaxNest (nesting depth), CCN
lizard -l python -x "*/env/*" -x "*/venv/*" -x "*/tests/*" -x "*/docs/*" \
  -o .reports/lizard.csv .
```

**Coverage (line + branch)**

```bash
pytest -q --cov=. --cov-branch \
  --cov-report=term-missing:skip-covered \
  --cov-report=xml:.reports/coverage.xml

# Optional pretty badge
genbadge coverage -i .reports/coverage.xml -o .reports/coverage-badge.svg
```

**Diff coverage (PRs only)**

```bash
# Compare against main to ensure new/changed lines are tested
diff-cover .reports/coverage.xml --compare-branch=origin/main --fail-under=60
```

---

## 3) Thresholds / gates we enforce

Use these as CI gates (tweak later as the codebase grows):

| Area                      | Gate (fail if worse)               | Tool                    |
| ------------------------- | ---------------------------------- | ----------------------- |
| **Line coverage**         | ≥ **70%** overall                  | `coverage`              |
| **Branch coverage**       | ≥ **50%** overall                  | `coverage --cov-branch` |
| **CC (per function)**     | no function > **15** (≈ Xenon “C”) | `xenon`/`radon`         |
| **CC average**            | project average ≤ **7**            | `xenon`                 |
| **Maintainability Index** | module MI **≥ 65**                 | `radon mi`              |
| **Diff coverage**         | **≥ 60%** of changed lines         | `diff-cover`            |

Example gate step:

```bash
xenon .                  # fails if thresholds exceeded (reads [tool.xenon])
diff-cover .reports/coverage.xml --compare-branch=origin/main --fail-under=60
```

---

## 4) Optional extras (coupling, imports, history)

- **Coupling / architecture rules**:
  `pip install import-linter` → write `importlinter` contracts (e.g., “Flask blueprints must not import data stores directly”).
  `pydeps` or `sourcery` can visualize import graphs and cycles.
- **History tracking**:
  `pip install wily` tracks radon/coverage metrics over time (`wily build . && wily report`).

---

## 5) Interpreting the numbers (pragmatic tips)

- **Cyclomatic Complexity**:

  - 1–5 (A): simple; 6–10 (B): fine; 11–20 (C): refactor candidate; >20: split.
  - Prefer _early returns_ and small helpers over deep nests (MaxNest↑).

- **Halstead**:

  - Very high **Volume** or **Effort** typically means “too much work in one place” → extract helpers; reduce intermediate states.

- **Maintainability Index**:

  - ≥85 (A) excellent, 70–85 (B) healthy, <65 consider refactor/tests/docs.

- **Coverage**:

  - Branch coverage is the better signal for control flow.
  - For security-critical and payment logic, test both success/failure paths and signature mismatches (MCDC-style thinking).

---

## 6) Minimal config (drop in)

`pyproject.toml`

```toml
[tool.pytest.ini_options]
addopts = "-q --cov=. --cov-branch --cov-report=term-missing:skip-covered --cov-report=xml:.reports/coverage.xml"
testpaths = ["tests"]

[tool.coverage.run]
branch = true
source = ["."]
omit = ["tests/*","docs/*","env/*","venv/*","instance/*"]

[tool.coverage.report]
fail_under = 70
skip_covered = true
show_missing = true

[tool.radon]
exclude = "tests|docs|env|venv|instance"
cc_min = "A"
mi_min = "B"
no_assert = true

[tool.xenon]
max-average = "A"
max-modules = "B"
max-absolute = "C"
exclude = ["tests","docs","env","venv","instance"]
```

`Makefile` (optional convenience)

```makefile
REPORTS=.reports
.PHONY: metrics coverage complexity loc gate

metrics: loc complexity coverage

loc:
	mkdir -p $(REPORTS)
	cloc --exclude-dir=.git,venv,env,dist,build,docs,instance --include-lang=Python . | tee $(REPORTS)/cloc.txt

complexity:
	mkdir -p $(REPORTS)
	radon cc -s -a -n --json . > $(REPORTS)/radon-cc.json
	radon mi -s --json . > $(REPORTS)/radon-mi.json
	radon hal -s --json . > $(REPORTS)/radon-halstead.json
	lizard -l python -x "*/env/*" -x "*/venv/*" -x "*/tests/*" -x "*/docs/*" -o $(REPORTS)/lizard.csv .

coverage:
	mkdir -p $(REPORTS)
	pytest
	genbadge coverage -i $(REPORTS)/coverage.xml -o $(REPORTS)/coverage-badge.svg || true

gate:
	xenon .
	diff-cover $(REPORTS)/coverage.xml --compare-branch=origin/main --fail-under=60
```

---

## 7) MCDC in Python (what’s realistic)

True **MCDC** requires showing that each boolean atom within a decision can independently affect the outcome. We don’t have an off-the-shelf Python tool that reports MCDC directly. Practical approach:

- Keep decisions **flat** (avoid `if a and b or c and not d` blobs).
- Write explicit tests toggling each boolean input while holding others constant.
- Use **branch coverage** as a proxy signal and review complex predicates manually (or refactor them into named helper functions and test those).

---

## 8) Where to focus first in this codebase

- **Payments webhook handler**: cover signature bad/good, amount mismatch, replay idempotency.
- **Receipt creation**: cover duplicate `job_key` rejection and total computation.
- **Auth throttling**: cover lock start/end and neutral login errors.
- Any function with **CC > 10** or **MaxNest > 3** from `radon`/`lizard`.

---

## 9) Outputs (artifacts)

After a run you’ll have:

```
.reports/
  cloc.txt
  radon-cc.json
  radon-mi.json
  radon-halstead.json
  lizard.csv
  coverage.xml
  coverage-badge.svg (optional)
```

Upload these in CI for quick inspection or wire them into a dashboard.

---
