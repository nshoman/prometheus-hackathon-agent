"""Team 1's generic Table & Plot agent, built from the Prometheus agent template."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from prometheus_api.agent import EventQueue, MissingInput, PrometheusAgent, RequestContext
from prometheus_api.storage_client import ConnectionConfig

from matching import (
    expand_columns,
    is_vague,
    match_column,
    match_table,
    select_axes,
    select_table,
    series_presets,
)
from plotting import render_plot
from tables import Table, parse_csv, parse_markdown

logger = logging.getLogger(__name__)


class TablePlotAgent(PrometheusAgent):
    """Parse values deterministically; ask rather than guess ambiguous selections."""

    async def _execute(
        self, params: dict[str, Any], context: RequestContext, event_queue: EventQueue
    ) -> dict:
        skill = params.get("skill_id")
        handlers = {
            "team1_extract_table": self._extract,
            "team1_plot_table": self._plot,
            "team1_list_tables": self._list,
        }
        handler = handlers.get(skill)
        if handler is None:
            return {"status": "error", "message": "Unknown table/plot skill."}
        pending = self.load_context_state(str(context.context_id)).get("pending", {})
        merged = dict(pending) if pending.get("skill_id") == skill else {}
        merged.update(params)
        merged.update(self.get_provided_inputs(context))
        try:
            return await handler(merged, context, event_queue)
        except (ValueError, FileNotFoundError, UnicodeError) as exc:
            return {"status": "error", "message": str(exc)}

    async def _ask(
        self,
        params: dict,
        context: RequestContext,
        events: EventQueue,
        fields: list[MissingInput],
        hint: str,
    ) -> dict:
        # Persist only selectors; reload source artifacts after the follow-up.
        pending = {
            key: value for key, value in params.items() if key not in {"artifacts", "userToken"}
        }
        self.save_context_state(str(context.context_id), {"pending": pending})
        await self.send_input_required(context, events, missing_inputs=fields, hint=hint)
        return {}

    async def _text(
        self, artifact_id: str, params: dict, context: RequestContext
    ) -> tuple[str, str]:
        if not artifact_id:
            raise ValueError("Upload a file in this conversation and choose its artifact.")
        path = await self.get_artifact_file_path(
            artifact_id, context=context, artifacts=params.get("artifacts")
        )
        if path.stat().st_size > 8_000_000:
            raise ValueError("Please provide a document smaller than 8 MB.")
        reference = self.resolve_artifact_reference(str(context.context_id), artifact_id)
        name = reference.name if reference else path.name
        return await asyncio.to_thread(path.read_text, encoding="utf-8-sig"), name

    async def _markdown(self, params: dict, context: RequestContext) -> list[Table]:
        text, name = await self._text(
            str(params.get("document_artifact_id") or ""), params, context
        )
        if Path(name).suffix.lower() not in {".md", ".markdown"}:
            raise ValueError(
                "Extract/list tables expects a Markdown (.md) upload; use the plot skill for CSV."
            )
        tables = parse_markdown(text)
        if not tables:
            raise ValueError("No GitHub-flavoured Markdown tables were found in this document.")
        return tables

    async def _list(self, params: dict, context: RequestContext, events: EventQueue) -> dict:
        tables = await self._markdown(params, context)
        listing = [
            {
                "caption": table.caption,
                "columns": table.columns,
                "rows": len(table.rows),
                "source_line": table.source_line,
            }
            for table in tables
        ]
        artifact_id = await self.send_json_artifact(
            context, events, listing, name="available_tables", catalog_artifact=True
        )
        return {"status": "success", "tables": listing, "artifact_id": artifact_id}

    async def _extract(self, params: dict, context: RequestContext, events: EventQueue) -> dict:
        tables = await self._markdown(params, context)
        await self.send_status_update(
            context=context,
            event_queue=events,
            message=f"Found {len(tables)} tables; matching captions.",
        )
        choices = [f"{index + 1}: {table.caption}" for index, table in enumerate(tables)]
        chosen = params.get("table_choice")
        query = str(params.get("table_query") or "")
        index = choices.index(chosen) if chosen in choices else match_table(tables, query)
        if index is None and query.strip():
            async with self.llm_client(context) as llm:
                index = await select_table(llm, tables, query)
        if index is None:
            return await self._ask(
                params,
                context,
                events,
                [
                    MissingInput(
                        name="table_choice",
                        type="enum",
                        options=choices,
                        description="The captions actually present in your document.",
                        user_prompt="Which table should I extract?",
                    )
                ],
                "I could not select a unique matching table. Choose an actual caption.",
            )
        table = tables[index]
        await self.send_status_update(
            context=context,
            event_queue=events,
            message=f"Extracting {len(table.rows)} rows and {len(table.columns)} columns without an LLM parser.",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"table_{index + 1}.csv"
            path.write_text(table.to_csv(), encoding="utf-8")
            reference = await self._publish(
                path,
                table.caption,
                "text/csv",
                context,
                events,
                {
                    "caption": table.caption,
                    "columns": table.columns,
                    "source_line": table.source_line,
                },
            )
        self.save_context_state(str(context.context_id), {})
        return {
            "status": "success",
            "artifact_id": reference.artifact_id,
            "table_artifact_id": reference.artifact_id,
            "caption": table.caption,
            "columns": table.columns,
            "numeric_columns": table.numeric_columns(),
            "row_count": len(table.rows),
        }

    async def _load_table(self, params: dict, context: RequestContext) -> Table:
        artifact_id = str(params.get("table_artifact_id") or "")
        reference = self.resolve_artifact_reference(str(context.context_id), artifact_id)
        if reference and isinstance(reference.json_body, dict):
            return Table.from_dict(reference.json_body)
        text, name = await self._text(artifact_id, params, context)
        reference = self.resolve_artifact_reference(str(context.context_id), artifact_id)
        suffix = Path(name).suffix.lower()
        if suffix == ".json":
            return Table.from_dict(json.loads(text))
        if suffix not in {".csv", ".tsv"}:
            raise ValueError(
                "Plotting expects a CSV/TSV upload or an extracted table artifact. Extract Markdown first."
            )
        caption = (
            reference.metadata.get("caption") or reference.description
            if reference
            else Path(name).stem
        )
        return parse_csv(
            text, str(caption or Path(name).stem), delimiter="\t" if suffix == ".tsv" else None
        )

    async def _plot(self, params: dict, context: RequestContext, events: EventQueue) -> dict:
        table = await self._load_table(params, context)
        numeric_columns = table.numeric_columns()
        if not numeric_columns:
            raise ValueError("This table has no numeric columns to plot.")
        x_request = str(params.get("x_column") or "")
        raw_y_columns = params.get("y_columns") or []
        if isinstance(raw_y_columns, str):
            raw_y_columns = [raw_y_columns]
        y_requests = list(raw_y_columns)
        if params.get("y_column"):
            y_requests.append(str(params["y_column"]))
        x = match_column(table.columns, x_request) if x_request else None
        presets = dict(series_presets(numeric_columns, x))
        ys: list[str] = []
        unresolved: list[str] = []
        for request in y_requests:
            request = str(request)
            if request.startswith("Keep just "):
                continue  # The user chose not to add another series on a follow-up ask.
            if request in presets:
                ys.extend(presets[request])
                continue
            column = match_column(table.columns, request)
            if column and column in numeric_columns and column != x:
                ys.append(column)
                continue
            expanded = expand_columns(table.columns, numeric_columns, request, x)
            if expanded:
                ys.extend(expanded)
                continue
            unresolved.append(request)
        ys = list(dict.fromkeys(ys))
        query = str(params.get("plot_query") or "")
        if (not x or not ys) and not is_vague(query):
            async with self.llm_client(context) as llm:
                selected = await select_axes(llm, table, query)
            if selected:
                x = x or selected[0]
                ys = ys or [y for y in selected[1] if y != x]
        fields = []
        if not x:
            fields.append(
                MissingInput(
                    name="x_column",
                    type="enum",
                    options=table.columns,
                    description="Actual columns in the selected table.",
                    user_prompt="Which column goes on the x axis?",
                )
            )
        pending_params = dict(params)
        if x:
            pending_params["x_column"] = x
        hint = "Choose the columns to plot; I will not guess unspecified axes."
        if not ys:
            candidates = [column for column in numeric_columns if column != x]
            if not candidates:
                raise ValueError(
                    "There is no numeric y column distinct from the selected x column."
                )
            preset_options = [label for label, _ in series_presets(numeric_columns, x)]
            fields.append(
                MissingInput(
                    name="y_column",
                    type="enum",
                    options=[*preset_options, *candidates],
                    description=(
                        "Numeric columns available for the y axis; a group option plots "
                        "several series at once."
                    ),
                    user_prompt="Which column(s) should I plot on the y axis?",
                )
            )
            # Drop the requests that failed to resolve; don't replay them against the
            # dropdown answer on resume.
            pending_params["y_columns"] = []
            pending_params.pop("y_column", None)
        elif unresolved:
            # Some requested series matched real columns; do not silently drop the rest.
            # Keep the matches, ask only about what did not resolve.
            candidates = [column for column in numeric_columns if column != x]
            preset_options = [label for label, _ in series_presets(numeric_columns, x)]
            skip_label = f"Keep just {', '.join(ys)}"
            fields.append(
                MissingInput(
                    name="y_column",
                    type="enum",
                    options=[skip_label, *preset_options, *candidates],
                    description=(
                        "Numeric columns available for the y axis; pick a group, add one "
                        "more series, or keep the ones already matched."
                    ),
                    user_prompt=(
                        f"I could not match {', '.join(unresolved)} to a column. "
                        "Add another series or keep what matched?"
                    ),
                )
            )
            pending_params["y_columns"] = ys
            pending_params.pop("y_column", None)
            hint = f"{', '.join(unresolved)} did not match a column. {hint}"
        if fields:
            return await self._ask(pending_params, context, events, fields, hint)
        await self.send_status_update(
            context=context, event_queue=events, message=f"Plotting {', '.join(ys)} versus {x}."
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plot.png"
            details = await asyncio.to_thread(
                render_plot,
                table,
                x,
                ys,
                path,
                str(params.get("chart_type") or "auto"),
                params.get("title"),
            )
            reference = await self._publish(
                path,
                f"{', '.join(ys)} versus {x}. {table.caption}",
                "image/png",
                context,
                events,
                {"x_column": x, "y_columns": ys, **details},
            )
        self.save_context_state(str(context.context_id), {})
        return {
            "status": "success",
            "artifact_id": reference.artifact_id,
            "x_column": x,
            "y_columns": ys,
            "caption": table.caption,
            **details,
        }

    async def _publish(
        self,
        path: Path,
        caption: str,
        mime_type: str,
        context: RequestContext,
        events: EventQueue,
        metadata: dict,
    ):
        try:
            config = ConnectionConfig.from_env()
        except ValueError:
            raise ValueError(
                "Configure the agent's local DEEPLYNX_* variables before publishing artifacts."
            ) from None
        return await self.send_deeplynx_artifact(
            context=context,
            event_queue=events,
            file_path=path,
            name=path.name,
            description=caption,
            storage_config=config,
            mime_type=mime_type,
            tags=["team1", "table" if mime_type == "text/csv" else "plot"],
            metadata=metadata,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    TablePlotAgent(agent_name="Team 1 Table and Plot Agent").run()
