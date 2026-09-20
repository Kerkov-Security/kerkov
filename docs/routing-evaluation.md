# Routing evaluation

This opt-in harness compares auto routing with the same tasks forced to the
configured LARGE model. It runs the production `RoutingPolicy` and `AgentRunner`
with the built-in analysis profile. Both arms receive identical instructions,
success criteria, model parameters, and tools: only the standard `report` tool.
All input data is supplied in the fixtures. No target access, file-reading tools,
shell commands, delegation, or network tools are available to either arm.

The initial corpus contains 24 frozen cases:

| Category | Cases | Checks |
| --- | ---: | --- |
| Clerical preprocessing of collected artifacts | 8 | Exact JSON |
| Bounded analysis of inventories, logs, and timing data | 8 | Exact JSON |
| Security decisions, sensitive text, complex or general work | 8 | 4 exact JSON, 4 human rubrics |

The current policy selects eight SMALL, eight MEDIUM, and eight LARGE routes.
This distribution comes from an intentionally balanced fixture set; it does not
estimate the distribution of real engagements. The corpus is a first check of
bounded, supplied-data tasks, not a benchmark of tool use or full assessments.

## Run

List cases without credentials or model calls:

```sh
python -m kerkov.routing.evaluate --list
```

For a live evaluation, select a provider using the existing Kerkov configuration
and credentials, and configure three distinct model IDs supported by that
provider:

```dotenv
AGENT_ROUTING_SMALL_MODEL=your-small-model-id
AGENT_ROUTING_MEDIUM_MODEL=your-medium-model-id
AGENT_ROUTING_LARGE_MODEL=your-large-model-id
```

The harness explicitly enables routing for the evaluation. It does not require
the interactive auto-mode setting to already be enabled. These commands make
real model calls and may incur charges:

```sh
# Start with one pair of runs.
python -m kerkov.routing.evaluate --case inventory-hosts --output evaluation-one.json

# Run the full corpus (48 agent runs), optionally supplying price estimates.
python -m kerkov.routing.evaluate --output evaluation.json --prices model-prices.json
```

`--case` can be repeated. Each arm uses a fresh provider fork and isolated
temporary transcript/workspace. Arm order alternates by case. Parameters are
fixed at temperature 0, up to 2,048 output tokens per call, four iterations, and
180 seconds per arm. Models/providers can interpret generation parameters
differently. There is no retry, escalation, or model-based grading.

The optional price file uses the same format as engagement usage summaries:

```json
{
  "version": 1,
  "models": {
    "ProviderClass/your-small-model-id": {
      "input": 1.0,
      "output": 2.0,
      "cache_read": 0.1,
      "cache_write": 1.25
    }
  }
}
```

These are illustrative rates, in USD per million tokens. Use the exact provider
identity from the report and add entries for all three models with your actual
rates. Pricing accounts separately for uncached input, cache reads, cache writes,
and output. Omitted prices or usage remain unknown; estimates are not invoices.

## Read results

The command prints a readable summary and writes the complete JSON report to
`--output`. It includes the selected cases and their frozen answers/rubrics,
corpus and system-prompt hashes, models, settings, price inputs, routing reasons,
per-arm findings/errors, elapsed time, measured token/cache usage, and estimated
cost. Temporary transcripts are discarded after their measurements are copied
into the report.

An exact-answer case passes only when the runner completed and the entire
`report.findings` value parses as the expected JSON. Extra fields, wrong values,
Markdown wrappers, partial runs, stuck runs, and provider errors fail. The
runner's `completed` status by itself is never an accuracy result.

The four prose cases stay `pending_review`; they are never counted as passes.
Review both arms' saved findings against each case's frozen rubric. Every rubric
criterion must be met for a manual pass. Record the reviewer and decision with
the report when sharing results; keep pending counts separate from pass/fail.

Token fields are known subtotals. `unknown_usage_calls`, `unmeasured_runs`, and
`token_usage_complete` describe their coverage; the printed summary explicitly
marks partial/unknown totals. Total estimated cost is null if any run lacks the
necessary measurements or prices. Elapsed time is the sum of per-arm wall time,
including runner overhead; per-run usage also retains model-call time.

Compare correctness first, then cost and elapsed time for comparable successful
runs. LARGE-routed auto cases are useful same-model controls. A single run cannot
establish general savings or answer quality: model nondeterminism, provider
caching, load, and this small corpus limit the conclusions. Re-run with the same
case/settings hashes when investigating a difference. The command finishing
successfully means it wrote a report, not that all cases passed.

## Offline checks

```sh
python -m pytest tests/routing/evaluation_test.py --no-cov
```

These tests use a local fake provider through real agent loops. They validate
route coverage, identical inputs/tools, correctness gates, unknown usage/cost,
cache-aware estimates, and handling of failed runs. They make no model API calls
and provide no empirical claim about the configured models.
