from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from prometheus_api.llm_client import ToolCall

from matching import (
    expand_columns,
    is_vague,
    match_column,
    match_table,
    select_axes,
    select_table,
    series_presets,
)
from tables import Table


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("time", "t [min]"),
        ("temperature", "T [K]"),
        ("t", "t [min]"),
        ("T", "T [K]"),
        ("missing", None),
    ],
)
def test_symbols_and_units_do_not_confuse_time_and_temperature(query, expected):
    assert match_column(["t [min]", "T [K]", "Pressure (Pa)"], query) == expected


def test_ambiguous_column_requires_selection():
    assert match_column(["Core temp [°C]", "Rind temp [°C]"], "temperature") is None
    assert (
        match_column(["Core temp [°C]", "Rind temp [°C]"], "core temperature") == "Core temp [°C]"
    )


def test_caption_paraphrase_and_no_match():
    tables = [
        Table("Midsummer census population", ["Year"], [["1"]]),
        Table("Wild mushroom harvest", ["Month"], [["May"]]),
    ]
    assert match_table(tables, "the residents table") == 0
    assert match_table(tables, "the mushroom harvest") == 1
    assert match_table(tables, "submarine fuel consumption") is None
    assert is_vague("plot this")


async def test_llm_selection_is_bounded_to_actual_columns():
    table = Table("Heating", ["t [s]", "T [K]"], [["0", "300"]])
    llm = SimpleNamespace(
        medium=AsyncMock(
            return_value=SimpleNamespace(
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="choose_plot_columns",
                        arguments={"matched": True, "x_column": "t [s]", "y_columns": ["T [K]"]},
                    )
                ]
            )
        )
    )
    assert await select_axes(llm, table, "temperature over time") == ("t [s]", ["T [K]"])
    options = llm.medium.call_args.kwargs["tools"][0].parameters["properties"]
    assert options["x_column"]["enum"] == ["", "t [s]", "T [K]"]
    assert "rows" not in llm.medium.call_args.args[0][1]["content"]


async def test_hallucinated_columns_and_table_indices_are_rejected():
    llm = SimpleNamespace(
        medium=AsyncMock(
            return_value=SimpleNamespace(
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="choose_plot_columns",
                        arguments={"matched": True, "x_column": "imaginary", "y_columns": ["y"]},
                    )
                ]
            )
        )
    )
    table = Table("Numbers", ["x", "y"], [["1", "2"]])
    assert await select_axes(llm, table, "numbers") is None
    llm.medium.return_value = SimpleNamespace(
        tool_calls=[ToolCall(id="2", name="choose_table", arguments={"table_index": 99})]
    )
    assert await select_table(llm, [table], "numbers") is None


POPULATION = Table(
    "Registered population of the three gnome villages",
    ["Year", "Thistledown", "Mossbury", "Pebblewick", "Total"],
    [["2024", "475", "391", "418", "1300"]],
)
HARVEST = Table(
    "Monthly rainfall and wild mushroom harvest",
    ["Month", "Rainfall (mm)", "Chanterelle yield (kg)", "Porcini yield (kg)", "Average price (copper per kg)"],
    [["January", "89.4", "9.7", "7.5", "34.6"]],
)
DRAGONS = Table(
    "Vital statistics of the dragons",
    ["Dragon", "Age (years)", "Wingspan (m)", "Top speed (km/h)"],
    [["Ember", "14", "3.9", "92"]],
)


@pytest.mark.parametrize(
    ("table", "query", "x", "expected"),
    [
        (POPULATION, "all villages", "Year", ["Thistledown", "Mossbury", "Pebblewick"]),
        (
            POPULATION,
            "populations of all villages",
            "Year",
            ["Thistledown", "Mossbury", "Pebblewick"],
        ),
        (POPULATION, "each village", "Year", ["Thistledown", "Mossbury", "Pebblewick"]),
        (
            POPULATION,
            "every village and the total",
            "Year",
            ["Thistledown", "Mossbury", "Pebblewick", "Total"],
        ),
        (HARVEST, "all yields", "Month", ["Chanterelle yield (kg)", "Porcini yield (kg)"]),
        (
            HARVEST,
            "both mushroom yields",
            "Month",
            ["Chanterelle yield (kg)", "Porcini yield (kg)"],
        ),
        (
            HARVEST,
            "all columns",
            "Month",
            ["Rainfall (mm)", "Chanterelle yield (kg)", "Porcini yield (kg)"],
        ),
        (
            DRAGONS,
            "wingspan and speed of each dragon",
            "Dragon",
            ["Wingspan (m)", "Top speed (km/h)"],
        ),
    ],
)
def test_expand_columns_resolves_collective_requests(table, query, x, expected):
    assert expand_columns(table.columns, table.numeric_columns(), query, x) == expected


@pytest.mark.parametrize(
    "query", ["Thistledown", "Total", "Riverbend", "population", "plot this"]
)
def test_expand_columns_refuses_non_collective_requests(query):
    assert expand_columns(POPULATION.columns, POPULATION.numeric_columns(), query, "Year") == []


def test_expand_columns_falls_back_when_every_candidate_is_an_aggregate():
    table = Table("Avg only", ["Day", "Average A", "Mean B"], [["1", "2", "3"]])
    assert expand_columns(table.columns, table.numeric_columns(), "all series", "Day") == [
        "Average A",
        "Mean B",
    ]


def test_expand_columns_never_includes_x():
    expanded = expand_columns(
        POPULATION.columns, POPULATION.numeric_columns(), "all villages", "Year"
    )
    assert "Year" not in expanded


def test_series_presets_are_deterministic_and_exclude_x():
    presets = series_presets(POPULATION.numeric_columns(), "Year")
    assert presets == [
        (
            "All 3 series (Thistledown, Mossbury, Pebblewick)",
            ["Thistledown", "Mossbury", "Pebblewick"],
        ),
        (
            "All 4 series including Total (Thistledown, Mossbury, Pebblewick, Total)",
            ["Thistledown", "Mossbury", "Pebblewick", "Total"],
        ),
    ]
    for _, columns in presets:
        assert "Year" not in columns
        assert all(column in POPULATION.numeric_columns() for column in columns)


def test_series_presets_omit_group_option_with_a_single_candidate():
    table = Table("Single", ["Year", "Pop"], [["2020", "5"]])
    assert series_presets(table.numeric_columns(), "Year") == []
