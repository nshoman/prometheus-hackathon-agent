from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import TablePlotAgent


@pytest.fixture
def agent(monkeypatch):
    instance = object.__new__(TablePlotAgent)
    state = {}
    instance.load_context_state = lambda _: state
    instance.save_context_state = lambda _, value: (state.clear(), state.update(value))
    instance.get_provided_inputs = lambda _: {}
    instance.send_status_update = AsyncMock()
    instance.send_input_required = AsyncMock()
    instance.send_json_artifact = AsyncMock(return_value="table-list-artifact")
    instance.get_artifact_file_path = AsyncMock()
    instance.resolve_artifact_reference = lambda *_: None
    instance.llm_client = MagicMock()
    instance.llm_client.return_value.__aenter__ = AsyncMock(
        return_value=SimpleNamespace(medium=AsyncMock(return_value=SimpleNamespace(tool_calls=[])))
    )
    instance.llm_client.return_value.__aexit__ = AsyncMock(return_value=False)
    instance._published = []
    instance.send_json_artifact = AsyncMock(return_value="json-artifact")
    instance.send_artifact_reference = AsyncMock(
        return_value=SimpleNamespace(artifact_id="conversation-file-artifact")
    )

    from artifact_io import ConnectionConfig

    monkeypatch.setattr(ConnectionConfig, "from_env", lambda: object())

    async def publish(*, file_path, mime_type, metadata, **kwargs):
        instance._published.append((mime_type, file_path.read_bytes(), metadata))
        return SimpleNamespace(artifact_id="output-artifact")

    instance.send_deeplynx_artifact = publish
    return instance


async def test_search_deeplynx_returns_records_and_publishes_json(
    agent, context, event_queue, monkeypatch
):
    item = SimpleNamespace(
        id=42,
        name="hollowmere_almanac.md",
        description="Annual rainfall and population tables.",
        properties={"source": "challenge"},
        tags=[SimpleNamespace(name="almanac")],
        data_source_id=7,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    page = SimpleNamespace(
        items=[item],
        page_info=SimpleNamespace(total_count=1),
    )
    storage = MagicMock()
    storage.__aenter__ = AsyncMock(return_value=storage)
    storage.__aexit__ = AsyncMock(return_value=False)
    storage.search.query = AsyncMock(return_value=page)
    monkeypatch.setattr("agent.ConnectionConfig.from_env", lambda: object())
    monkeypatch.setattr("agent.AsyncStorageClient", lambda config: storage)

    result = await agent._execute(
        {"skill_id": "team1_search_deeplynx", "query": "the almanac"},
        context,
        event_queue,
    )

    assert result["status"] == "success"
    assert result["result_count"] == 1
    assert result["results"][0]["name"] == "hollowmere_almanac.md"
    assert result["artifact_id"] == "json-artifact"
    storage.search.query.assert_awaited_once_with("the almanac", page=1, page_size=20)
    agent.send_json_artifact.assert_awaited_once()


async def test_import_deeplynx_file_registers_conversation_artifact(
    agent, context, event_queue, monkeypatch
):
    item = SimpleNamespace(
        id=16,
        name="hollowmere_almanac.md",
        description="Markdown almanac.",
        properties={"mime_type": "text/markdown", "original_filename": "hollowmere_almanac.md"},
    )
    file_meta = SimpleNamespace(name="hollowmere_almanac.md", size=7775)
    storage = MagicMock()
    storage.__aenter__ = AsyncMock(return_value=storage)
    storage.__aexit__ = AsyncMock(return_value=False)
    storage.items.get = AsyncMock(return_value=item)
    storage.files.get = AsyncMock(return_value=file_meta)
    config = SimpleNamespace(organization_id=1, project_id=1)
    monkeypatch.setattr("agent.ConnectionConfig.from_env", lambda: config)
    monkeypatch.setattr("agent.AsyncStorageClient", lambda _: storage)

    result = await agent._execute(
        {"skill_id": "team1_import_deeplynx_file", "file_id": 16},
        context,
        event_queue,
    )

    assert result["status"] == "success"
    assert result["artifact_id"] == "conversation-file-artifact"
    assert result["mime_type"] == "text/markdown"
    kwargs = agent.send_artifact_reference.await_args.kwargs
    assert kwargs["uri"] == "deeplynx://organizations/1/projects/1/items/16"
    assert kwargs["metadata"]["deeplynx_file_id"] == 16


async def test_extract_publishes_renderable_csv_without_an_llm(agent, context, event_queue):
    path = Path(__file__).parent / "fixtures/hollowmere_almanac.md"
    agent.get_artifact_file_path.return_value = path
    result = await agent._execute(
        {
            "skill_id": "team1_extract_table",
            "document_artifact_id": "doc",
            "table_query": "population",
        },
        context,
        event_queue,
    )
    assert result["row_count"] == 15
    assert result["columns"] == ["Year", "Thistledown", "Mossbury", "Pebblewick", "Total"]
    assert result["table_artifact_id"] == "output-artifact"
    assert agent._published[0][0] == "text/csv"
    assert b"2010,416,382,172,974" in agent._published[0][1]
    agent.llm_client.assert_not_called()


async def test_missing_table_offers_actual_captions(agent, context, event_queue):
    agent.get_artifact_file_path.return_value = (
        Path(__file__).parent / "fixtures/hollowmere_almanac.md"
    )
    assert (
        await agent._execute(
            {
                "skill_id": "team1_extract_table",
                "document_artifact_id": "doc",
                "table_query": "submarine fuel",
            },
            context,
            event_queue,
        )
        == {}
    )
    field = agent.send_input_required.call_args.kwargs["missing_inputs"][0]
    assert field.name == "table_choice"
    assert len(field.options) == 3
    assert "population" in field.options[0]


async def test_vague_csv_plot_asks_real_columns_then_resumes(agent, context, event_queue, tmp_path):
    path = tmp_path / "measurements.csv"
    path.write_text("t [s],T [K],note\n0,300,start\n1,310,end\n")
    agent.get_artifact_file_path.return_value = path
    result = await agent._execute(
        {
            "skill_id": "team1_plot_table",
            "table_artifact_id": "csv",
            "plot_query": "plot this",
        },
        context,
        event_queue,
    )
    assert result == {}
    fields = agent.send_input_required.call_args.kwargs["missing_inputs"]
    assert fields[0].options == ["t [s]", "T [K]", "note"]
    assert fields[1].options == ["t [s]", "T [K]"]
    agent.llm_client.assert_not_called()
    agent.get_provided_inputs = lambda _: {"x_column": "t [s]", "y_column": "T [K]"}
    result = await agent._execute({"skill_id": "team1_plot_table"}, context, event_queue)
    assert result["status"] == "success"
    assert result["y_columns"] == ["T [K]"]
    assert agent._published[0][0] == "image/png"
    assert agent._published[0][1].startswith(b"\x89PNG")


async def test_wrong_file_type_returns_clear_error(agent, context, event_queue, tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("not a markdown upload")
    agent.get_artifact_file_path.return_value = path
    result = await agent._execute(
        {"skill_id": "team1_extract_table", "document_artifact_id": "doc"},
        context,
        event_queue,
    )
    assert result["status"] == "error"
    assert "Markdown" in result["message"]


async def test_unknown_skill_is_rejected(agent, context, event_queue):
    assert (await agent._execute({"skill_id": "unknown"}, context, event_queue))[
        "status"
    ] == "error"


async def test_list_skill_uses_the_parser_without_inference(agent, context, event_queue):
    agent.get_artifact_file_path.return_value = (
        Path(__file__).parent / "fixtures/hollowmere_almanac.md"
    )
    result = await agent._execute(
        {"skill_id": "team1_list_tables", "document_artifact_id": "doc"},
        context,
        event_queue,
    )
    assert result["artifact_id"] == "table-list-artifact"
    assert [table["rows"] for table in result["tables"]] == [15, 12, 8]
    assert result["tables"][0]["columns"][-1] == "Total"
    agent.llm_client.assert_not_called()


async def test_workflow_plot_request_can_select_symbolic_axes(
    agent, context, event_queue, tmp_path
):
    from prometheus_api.llm_client import ToolCall

    path = tmp_path / "measurements.csv"
    path.write_text("t [min],T [K],Status\n0,290,starting\n1,302,heating\n")
    agent.get_artifact_file_path.return_value = path
    llm = agent.llm_client.return_value.__aenter__.return_value
    llm.medium.return_value = SimpleNamespace(
        tool_calls=[
            ToolCall(
                id="selection",
                name="choose_plot_columns",
                arguments={"matched": True, "x_column": "t [min]", "y_columns": ["T [K]"]},
            )
        ]
    )
    result = await agent._execute(
        {
            "skill_id": "team1_plot_table",
            "table_artifact_id": "table",
            "plot_query": "temperature against time",
        },
        context,
        event_queue,
    )
    assert result["status"] == "success"
    assert result["x_column"] == "t [min]"
    assert result["y_columns"] == ["T [K]"]
    agent.send_input_required.assert_not_called()
