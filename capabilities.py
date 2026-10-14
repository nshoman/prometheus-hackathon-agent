"""Coordinate table selection and plotting using SDK artifact adapters."""

import asyncio
import tempfile
from pathlib import Path

from prometheus_api.agent import EventQueue, MissingInput, RequestContext

from artifact_io import AgentArtifacts
from matching import is_vague, match_column, match_table, select_axes, select_table
from plotting import render_plot


class TableCapabilities:
    """Implement the three skills while the agent handles A2A dispatch only."""

    def __init__(self, agent):
        """Bind the agent and its artifact adapter for one skill execution."""
        self.agent = agent
        self.io = AgentArtifacts(agent)

    async def _list(self, params: dict, context: RequestContext, events: EventQueue) -> dict:
        """Publish actual table captions, headers, row counts and source locations."""
        tables = await self.io._markdown(params, context)
        listing = [
            {
                "caption": table.caption,
                "columns": table.columns,
                "rows": len(table.rows),
                "source_line": table.source_line,
            }
            for table in tables
        ]
        artifact_id = await self.agent.send_json_artifact(
            context, events, listing, name="available_tables", catalog_artifact=True
        )
        return {"status": "success", "tables": listing, "artifact_id": artifact_id}

    async def _extract(self, params: dict, context: RequestContext, events: EventQueue) -> dict:
        """Select an actual caption and publish the unchanged source cells as CSV."""
        tables = await self.io._markdown(params, context)
        await self.agent.send_status_update(
            context=context,
            event_queue=events,
            message=f"Found {len(tables)} tables; matching captions.",
        )
        choices = [f"{index + 1}: {table.caption}" for index, table in enumerate(tables)]
        chosen = params.get("table_choice")
        query = str(params.get("table_query") or "")
        index = choices.index(chosen) if chosen in choices else match_table(tables, query)
        if index is None and query.strip():
            async with self.agent.llm_client(context, config=self.agent._llm_config()) as llm:
                index = await select_table(llm, tables, query)
        if index is None:
            return await self.io._ask(
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
        await self.agent.send_status_update(
            context=context,
            event_queue=events,
            message=f"Extracting {len(table.rows)} rows and {len(table.columns)} columns without an LLM parser.",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"table_{index + 1}.csv"
            path.write_text(table.to_csv(), encoding="utf-8")
            reference = await self.io._publish(
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
        self.agent.save_context_state(str(context.context_id), {})
        return {
            "status": "success",
            "artifact_id": reference.artifact_id,
            "table_artifact_id": reference.artifact_id,
            "caption": table.caption,
            "columns": table.columns,
            "numeric_columns": table.numeric_columns(),
            "row_count": len(table.rows),
        }

    async def _plot(self, params: dict, context: RequestContext, events: EventQueue) -> dict:
        """Resolve explicit or semantic axes, ask when ambiguous, then publish a PNG."""
        table = await self.io._load_table(params, context)
        numeric_columns = table.numeric_columns()
        if not numeric_columns:
            raise ValueError("This table has no numeric columns to plot.")
        x_request = str(params.get("x_column") or "")
        y_requests = params.get("y_columns") or (
            [params["y_column"]] if params.get("y_column") else []
        )
        if isinstance(y_requests, str):
            y_requests = [y_requests]
        x = match_column(table.columns, x_request) if x_request else None
        ys = [match_column(table.columns, str(y)) for y in y_requests]
        ys = (
            list(dict.fromkeys(ys))
            if ys and all(y in numeric_columns and y != x for y in ys)
            else []
        )
        query = str(params.get("plot_query") or "")
        if (not x or not ys) and not is_vague(query):
            async with self.agent.llm_client(context, config=self.agent._llm_config()) as llm:
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
        if not ys:
            candidates = [column for column in numeric_columns if column != x]
            if not candidates:
                raise ValueError(
                    "There is no numeric y column distinct from the selected x column."
                )
            fields.append(
                MissingInput(
                    name="y_column",
                    type="enum",
                    options=candidates,
                    description="Numeric columns available for the y axis.",
                    user_prompt="Which column should I plot on the y axis?",
                )
            )
        if fields:
            return await self.io._ask(
                params,
                context,
                events,
                fields,
                "Choose the columns to plot; I will not guess unspecified axes.",
            )
        await self.agent.send_status_update(
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
            reference = await self.io._publish(
                path,
                f"{', '.join(ys)} versus {x}. {table.caption}",
                "image/png",
                context,
                events,
                {"x_column": x, "y_columns": ys, **details},
            )
        self.agent.save_context_state(str(context.context_id), {})
        return {
            "status": "success",
            "artifact_id": reference.artifact_id,
            "x_column": x,
            "y_columns": ys,
            "caption": table.caption,
            **details,
        }
