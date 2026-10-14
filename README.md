# Team 1 Table and Plot Agent

Extract captioned Markdown tables and plot CSV/TSV or table artifacts through Prometheus. Parsing preserves source cells deterministically; semantic matching selects only from actual captions and headers. Team 1 includes contributors from ORNL, INL and Sandia.

## Skills

| Skill ID | Input | Output |
|---|---|---|
| `team1_extract_table` | Markdown artifact ID and a caption/subject description | A CSV table artifact, exact headers/values, caption and row count |
| `team1_search_deeplynx` | DeepLynx text query | Matching DeepLynx records and a cataloged JSON result artifact |
| `team1_import_deeplynx_file` | DeepLynx file/record ID from search results | A conversation-scoped artifact usable by extraction and plotting |
| `team1_plot_table` | Extracted table artifact or uploaded CSV/TSV; requested axes | A PNG artifact stored in DeepLynx |
| `team1_list_tables` | Markdown artifact ID | Actual captions, columns, row counts and source lines |

The Docker container is `team1-table-plot-agent`. It joins `prometheus_platform_agent-network` and registers its card and workflow, `team1.extract_then_plot`, through the SDK.

## Organization

| Module               | Responsibility                                             |
| -------------------- | ---------------------------------------------------------- |
| `markdown_parser.py` | Captioned GFM Markdown → `TableData`                       |
| `table_data.py`      | Validated table structure, CSV serialization and numbers   |
| `plotting.py`        | `TableData` → PNG with labels, units and missing-data gaps |
| `matching.py`        | Lexical matching and bounded LLM selection                 |
| `capabilities.py`    | Extract, list and plot skill coordination                  |
| `artifact_io.py`     | SDK artifact storage, file reads and follow-up prompts     |
| `agent.py`           | A2A dispatch and conversation resume                       |
| `tests/`             | Offline pytest regression tests                            |
| `scripts/`           | Manual platform/DeepLynx smoke checks                      |

The parser ignores fenced code, supports wrapped bold captions and escaped pipes, and rejects malformed rows. `TableData` retains headers and original cell strings; its CSV reader handles quoting, BOMs and comma/semicolon/tab delimiters.

Matching first checks exact headers and generic lexical overlap. It preserves case-sensitive symbols, without dictionaries that assign topics or meanings to them. Paraphrases and symbols with units use one bounded LLM tool call over real candidates; row values are never sent to the model. Ambiguous requests produce actual-caption or actual-column choices. Extraction parses all tables before deciding whether there is a single candidate.

Plots support line, scatter and grouped bars. Time-like axis metadata informs automatic style; explicit `chart_type` overrides it. Numeric x values are sorted only for plotting. Missing readings remain gaps and incompatible y units use separate panels. Rendering uses isolated Matplotlib Agg figures in a worker thread.

`team1_search_deeplynx` searches the configured project through the Prometheus SDK's authenticated storage client. It returns record metadata and publishes the complete result set as a JSON artifact, which can be used as the input to a later table or file-processing step.

`team1_import_deeplynx_file` converts a `file_id` from those results into a conversation-scoped `deeplynx://` artifact reference. Pass its returned `artifact_id` to `team1_list_tables` or `team1_extract_table`; do not pass the numeric DeepLynx ID as the conversation artifact ID.

## Setup and run

Requires Python 3.12+, uv, and a compatible Prometheus SDK. With `prometheus_core` checked out alongside this repository:

```sh
make dev-setup
uv run pytest
uv run ruff check .
```

If the SDK is elsewhere, use `make dev-setup SDK_PATH=/path/to/prometheus_core/sdk`. The Docker base image already supplies the SDK; it is deliberately not duplicated in `pyproject.toml`.

Create the ignored `.env` from the platform using its standard agent setup helper:

```sh
uv run python ../prometheus_core/agent-template/setup_agent.py --env-only .
```

The helper selects agent credentials from the platform rather than copying its private signing key. Review the local configuration; it must match the platform's storage, identity and inference setup. Never commit `.env` or print resolved LLM settings.

Required configuration includes:

- SDK base image repository/tag compatible with the platform.
- Matching `AGENT_SECRET`, `LITELLM_MASTER_KEY`, `REDIS_PASSWORD` and service URLs.
- `DEEPLYNX_*` storage connection settings for uploads and generated artifacts.
- `LLM_CONFIG_PROFILE` and its provider key, where applicable.
- The platform's identity verifier settings (`JWKS_URL` or public key, issuer/audience).

Start the platform first, then run:

```sh
make up
```

This rebuilds and starts this agent only. `make down` stops it; `make logs` tails its logs. External mode requires both `EXTERNAL_AGENT_HOST` and `EXTERNAL_AGENT_PORT`. Run one checkout at a time because all copies use the same container name.

For standalone development, use the same SDK entry point:

```sh
uv run python agent.py
```

When running directly on the host, configure registry, Redis, identity and storage URLs reachable from the host rather than Docker-only service names. Requests use the standard A2A interface and configured authentication.

## Verification

Offline checks are in `tests/`. Manual system checks are documented in [scripts/README.md](scripts/README.md); they exercise the orchestrator and real DeepLynx artifact downloads.

The demo uploads are bundled in `tests/fixtures/`. Use a fresh Prometheus conversation:

1. Upload `hollowmere_almanac.md`: **Extract the population table and plot each village over time.**
2. Upload `demo_sightings.csv`: **Plot this file.** Choose `night` and `mice_sighted` from the actual-column dropdowns.
3. Upload `symbolic_report.md`: **Extract the heating measurements and plot temperature against time.**
4. Ask to list tables or choose a nonexistent caption; expect real choices or a clear error.

These are fixtures, not runtime matching rules. New documents need no code changes when they follow the same table contract.

## Limits and attribution

Accepts UTF-8 Markdown/CSV/TSV and compatible table JSON up to 8 MB. Headers must be unique and nonempty; row widths must agree. Y columns must contain numbers or documented missing markers. Excel, PDFs, scans, currency/unit conversion and approximate extraction need separate capabilities.

The template and SDK come from Prometheus, licensed under MIT; see LICENSE and NOTICE.txt. Plotting uses Matplotlib. The challenge supplied the practice datasets. The team repository is [nshoman/prometheus-hackathon-agent](https://github.com/nshoman/prometheus-hackathon-agent), with the implementation branch in [ajfurlong/prometheus-hackathon-agent](https://github.com/ajfurlong/prometheus-hackathon-agent). See [SUBMISSION.md](SUBMISSION.md) for validation and remaining judging requirements.
