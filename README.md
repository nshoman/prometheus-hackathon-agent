# Team 1 Table and Plot Agent

A general-purpose table extraction and plotting agent for Team 1, built with `prometheus_core/agent-template/setup_agent.py` and subclassing `PrometheusAgent`. It accepts rectangular tables from different domains: UTF-8 GitHub-flavoured Markdown with captions, CSV/TSV, or compatible table JSON. The supplied Hollowmere file is a demo and test fixture; the parser has no dataset-specific rules.

## Skills

| Skill ID | Input | Output |
|---|---|---|
| `team1_extract_table` | Markdown artifact ID and a caption/subject description | A CSV table artifact, exact headers/values, caption and row count |
| `team1_plot_table` | Extracted table artifact or uploaded CSV/TSV; requested axes | A PNG artifact stored in DeepLynx |
| `team1_list_tables` | Markdown artifact ID | Actual captions, columns, row counts and source lines |

The card is **Team 1 Table and Plot Agent**; the Docker container is `team1-table-plot-agent`. The agent joins the existing `prometheus_platform_agent-network` and registers itself. No platform source changes are required.

## Approach

`tables.py` deterministically scans GitHub-flavoured Markdown tables. It associates the immediately preceding bold caption, including multiline captions and captions followed by prose. It ignores fenced code, recognizes alignment separators, handles escaped pipes, validates row widths and unique headers, and retains original cell strings. CSV/TSV use Python's standard `csv` parser, including quoted commas and quotes.

`matching.py` first matches exact headers, caption words and common semantic synonyms. It distinguishes `t` from `T` and understands units such as `[min]` and `[K]`. When the user's description remains ambiguous, one forced LLM tool call may choose among actual captions/headers; row values are never sent to the model. Invalid names or indices are rejected. If the model is unavailable or cannot match, the agent asks rather than fabricating data.

`plotting.py` uses Matplotlib's Agg backend and isolated figures. It supports line, scatter and grouped-bar plots, original axis labels/units, legends for multiple series, and separate panels for incompatible y units. Units come from the column headers; caption words do not imply units. Thousands separators and Unicode minus signs are recognized only for plotting. Missing values remain intact in CSV output and become gaps in lines; no interpolation or imputation occurs. Numeric x values are sorted for plotting, without altering extracted rows. Rendering runs in a worker thread.

For **plot this**, axes stay optional in the card so the skill can read the file and offer dropdowns of its real columns. The follow-up stores selectors in conversation state and reloads the source on resume. `y_columns` supports multiple explicitly requested series by name, and also a single collective phrase such as `all villages` or `each village`, which is expanded deterministically against the table's real headers; total/average columns are excluded from an expansion unless the request names them. If some requested series match and others do not, the matches are kept and only the unmatched names are asked about. When the dropdown is needed, it offers a one-click "all series" group option ahead of the individual columns, so a multi-series plot does not require typing every name.

CSV output uses `text/csv`, which the current platform renders as a table. PNG output uses `image/png`, which renders as an image. Both are cataloged DeepLynx artifacts and pass between skills by artifact ID, never by copying table contents into prompts. A published workflow, `team1.extract_then_plot`, also chains the two skills.

## Setup and run

From this repository's root, with `prometheus_core` checked out alongside it:

```sh
make dev-setup
uv run pytest
uv run ruff check .
make up
```

`make dev-setup` installs the agent dependencies and the local SDK in its isolated environment. If the SDK is elsewhere, supply its path: `make dev-setup SDK_PATH=/path/to/prometheus_core/sdk`. The SDK must match the platform image; it is intentionally not a runtime dependency because the template's base image already provides it. Python 3.12+ and uv are required for local development.

For Docker, `.env` selects the SDK base image, inference profile and shared platform credentials. This workspace's ignored `.env` was copied from the existing platform setup and uses the local SDK and Azure profile. Never commit this file. A fresh checkout can copy `.env.example` to `.env`, then use the template's `setup_agent.py --env-only /path/to/agent` or configure the variables below directly.

Required settings:

- `PROMETHEUS_API_BASE_IMAGE_REPO` and `PROMETHEUS_API_BASE_IMAGE_HASH`: an SDK image compatible with the platform; this workspace uses `prometheus_api:local`.
- `AGENT_SECRET`, `LITELLM_MASTER_KEY`, `REDIS_PASSWORD`: match the running platform.
- `REGISTRY_URL`, `REDIS_URL`: the existing platform network addresses.
- `DEEPLYNX_BASE_URL`, `DEEPLYNX_KEY`, `DEEPLYNX_SECRET`, `DEEPLYNX_ORG_ID`, `DEEPLYNX_PROJECT_ID`: the same storage configuration as the platform.
- `LLM_CONFIG_PROFILE` and its provider key: this demo uses `azure` and `OPENAI_API_KEY`. Deterministic local parsing/plotting needs no LLM key.
- `JWKS_URL` or the platform's public identity key, issuer/audience and `USER_IDENTITY_REQUIRED`: match the platform's identity configuration. Never copy its private signing key.

The template Makefile supports internal mode (default) or external mode with both `EXTERNAL_AGENT_HOST` and `EXTERNAL_AGENT_PORT`. `make down` stops only this agent. After adding/changing its card or workflow, refresh orchestrator discovery or wait for automatic refresh. `/agents` should show the Team 1 card.

If the same agent is already running from another checkout, stop that copy with `make down` in its directory before starting this one. Both use the container name `team1-table-plot-agent`.

## Demo in chat

The demo uploads are included under `tests/fixtures/`. Use a fresh conversation for each test:

1. Upload `hollowmere_almanac.md`. Ask: **Extract the table with population data and plot the population of each village over time.** Expect a 15-row table and a line plot of Thistledown, Mossbury and Pebblewick from 2010 to 2024, excluding the aggregate Total column.
2. Upload `demo_sightings.csv`. Ask: **Plot this file.** Choose `night` for x and `mice_sighted` for y in the actual-column dropdowns.
3. Upload `symbolic_report.md`. Ask: **Extract the heating measurements table and plot temperature against time.** Expect `t [min]` vs `T [K]`, with a gap for the missing reading.
4. Ask to list tables, or request a nonexistent table. Expect real caption choices or a clear message.

Offline, run `make demo` to write the population CSV, structured JSON and PNG under the ignored `demo-output/` directory. The same parser/plotter also have generic commands:

```sh
uv run python cli.py list tests/fixtures/hollowmere_almanac.md
uv run python cli.py extract report.md --query population --output output/population
uv run python cli.py plot output/population.json --x Year --y Village_A Village_B --output output/population.png
uv run python cli.py plot measurements.csv --x time --y temperature --output output/temperature.png
```

`live_verify.py` exercises uploaded files through the normal platform orchestrator using its existing system-test A2A helper. It does not call the Team 1 skills directly or prescribe an execution plan. It runs in an ephemeral test container with `/tests/system`, `/dataset`, and `/results` mounts. It prints only sanitized progress/results and saves a verification report.

## Tests and limits

Tests cover the actual almanac, wrapped captions, escaped pipes, fenced examples, CSV quoting/BOM/delimiters, thousands separators, missing data, malformed rows, symbol matching, mocked LLM decisions, PNG generation, honest labels and input-required resume. Files are limited to 8 MB; only UTF-8 Markdown/CSV/TSV or compatible inline table JSON are accepted. Headers must be unique and nonempty, rows must have consistent widths, and y columns must contain numeric or recognized missing values. Excel workbooks, PDFs and scanned tables need a separate reader. Arbitrary text/currency/unit conversion and approximate value extraction are not attempted.

The team repository is [nshoman/prometheus-hackathon-agent](https://github.com/nshoman/prometheus-hackathon-agent), with your fork at [ajfurlong/prometheus-hackathon-agent](https://github.com/ajfurlong/prometheus-hackathon-agent). The table-and-plot implementation is maintained on branch `table-plot-agent`; `origin` points to your fork and `upstream` points to the team repository. Validation and the remaining submission requirements are tracked in [SUBMISSION.md](SUBMISSION.md).

## Attribution

Agent plumbing comes from the Prometheus agent template and SDK (Battelle Energy Alliance, MIT license; see the upstream LICENSE/NOTICE). Deterministic table matching and plotting logic were implemented for this challenge. Plotting uses Matplotlib; CSV parsing uses Python's standard library. The Hollowmere and lunar-cheese practice datasets come from the provided challenge materials.
