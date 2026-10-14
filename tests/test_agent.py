from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import TablePlotAgent


@pytest.fixture
def agent():
    instance = object.__new__(TablePlotAgent)
    state = {}
    instance.load_context_state = lambda _: state
    instance.save_context_state = lambda _, value: (state.clear(), state.update(value))
    instance.get_provided_inputs = lambda _: {}
    instance.send_status_update = AsyncMock()
    instance.send_input_required = AsyncMock()
    instance.get_artifact_file_path = AsyncMock()
    instance.resolve_artifact_reference = lambda *_: None
    instance.llm_client = MagicMock()
    instance.llm_client.return_value.__aenter__ = AsyncMock(
        return_value=SimpleNamespace(medium=AsyncMock(return_value=SimpleNamespace(tool_calls=[])))
    )
    instance.llm_client.return_value.__aexit__ = AsyncMock(return_value=False)
    instance._published = []

    async def publish(path, caption, mime_type, context, events, metadata):
        instance._published.append((mime_type, path.read_bytes(), metadata))
        return SimpleNamespace(artifact_id="output-artifact")

    instance._publish = publish
    return instance


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
    assert fields[1].options == ["All 2 series (t [s], T [K])", "t [s]", "T [K]"]
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


POPULATION_CSV = (
    "Year,Thistledown,Mossbury,Pebblewick,Total\n"
    "2010,416,382,172,974\n"
    "2011,412,389,180,995\n"
)


async def test_collective_y_request_expands_without_an_llm(agent, context, event_queue, tmp_path):
    path = tmp_path / "population.csv"
    path.write_text(POPULATION_CSV)
    agent.get_artifact_file_path.return_value = path
    result = await agent._execute(
        {
            "skill_id": "team1_plot_table",
            "table_artifact_id": "csv",
            "x_column": "year",
            "y_columns": ["all villages"],
            "plot_query": "plot populations of all villages vs year",
        },
        context,
        event_queue,
    )
    assert result["status"] == "success"
    assert result["x_column"] == "Year"
    assert result["y_columns"] == ["Thistledown", "Mossbury", "Pebblewick"]
    agent.llm_client.assert_not_called()


async def test_partial_y_match_keeps_matches_and_asks_about_the_rest(
    agent, context, event_queue, tmp_path
):
    path = tmp_path / "population.csv"
    path.write_text(POPULATION_CSV)
    agent.get_artifact_file_path.return_value = path
    result = await agent._execute(
        {
            "skill_id": "team1_plot_table",
            "table_artifact_id": "csv",
            "x_column": "Year",
            "y_columns": ["Thistledown", "Mossbury", "Riverbend"],
        },
        context,
        event_queue,
    )
    assert result == {}
    call = agent.send_input_required.call_args.kwargs
    assert "Riverbend" in call["hint"]
    field = call["missing_inputs"][0]
    assert field.name == "y_column"
    assert "Pebblewick" in field.options
    # The two that matched are not thrown away; they are carried on resume.
    pending = agent.load_context_state(str(context.context_id))["pending"]
    assert pending["y_columns"] == ["Thistledown", "Mossbury"]
    agent.get_provided_inputs = lambda _: {"y_column": "Pebblewick"}
    result = await agent._execute({"skill_id": "team1_plot_table"}, context, event_queue)
    assert result["status"] == "success"
    assert result["y_columns"] == ["Thistledown", "Mossbury", "Pebblewick"]


async def test_group_preset_answer_resumes_with_every_series(
    agent, context, event_queue, tmp_path
):
    path = tmp_path / "population.csv"
    path.write_text(POPULATION_CSV)
    agent.get_artifact_file_path.return_value = path
    result = await agent._execute(
        {"skill_id": "team1_plot_table", "table_artifact_id": "csv", "x_column": "Year"},
        context,
        event_queue,
    )
    assert result == {}
    field = agent.send_input_required.call_args.kwargs["missing_inputs"][0]
    assert field.options[0] == "All 3 series (Thistledown, Mossbury, Pebblewick)"
    agent.get_provided_inputs = lambda _: {"y_column": field.options[0]}
    result = await agent._execute({"skill_id": "team1_plot_table"}, context, event_queue)
    assert result["status"] == "success"
    assert result["y_columns"] == ["Thistledown", "Mossbury", "Pebblewick"]
    assert agent._published[0][0] == "image/png"


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
