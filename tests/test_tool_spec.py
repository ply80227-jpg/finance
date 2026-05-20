from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_market.tool_spec import (
    get_anthropic_tools,
    get_mcp_tools,
    get_openai_tools,
    get_output_schemas,
    render,
)

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"

EXPECTED_NAMES = {"quote", "batch_quote", "history", "news", "search"}


class TestToolSpec:
    def test_openai_shape(self) -> None:
        tools = get_openai_tools()
        assert {t["function"]["name"] for t in tools} == EXPECTED_NAMES
        for t in tools:
            assert t["type"] == "function"
            fn = t["function"]
            assert "description" in fn and fn["description"]
            params = fn["parameters"]
            assert params["type"] == "object"
            assert params["additionalProperties"] is False
            # required keys must reference actual property names
            for req in params.get("required", []):
                assert req in params["properties"]

    def test_anthropic_shape(self) -> None:
        tools = get_anthropic_tools()
        assert {t["name"] for t in tools} == EXPECTED_NAMES
        for t in tools:
            assert "description" in t and t["description"]
            schema = t["input_schema"]
            assert schema["type"] == "object"
            assert schema["additionalProperties"] is False

    def test_mcp_shape(self) -> None:
        tools = get_mcp_tools()
        assert {t["name"] for t in tools} == EXPECTED_NAMES
        for t in tools:
            # MCP uses camelCase ``inputSchema`` to match the spec.
            assert "inputSchema" in t and t["inputSchema"]["type"] == "object"

    def test_quote_requires_symbol(self) -> None:
        tools = get_anthropic_tools()
        quote = next(t for t in tools if t["name"] == "quote")
        assert quote["input_schema"]["required"] == ["symbol"]
        market = quote["input_schema"]["properties"]["market"]
        assert market["enum"] == ["cn", "hk"]

    def test_batch_quote_has_bounded_list(self) -> None:
        tools = get_anthropic_tools()
        batch = next(t for t in tools if t["name"] == "batch_quote")
        symbols = batch["input_schema"]["properties"]["symbols"]
        assert symbols["type"] == "array"
        assert symbols["minItems"] == 1
        assert symbols["maxItems"] == 50

    def test_output_schemas_have_required_envelopes(self) -> None:
        schemas = get_output_schemas()
        assert set(schemas) >= EXPECTED_NAMES
        # Every envelope must require ok + schema_version, no exception.
        for name, sch in schemas.items():
            assert "ok" in sch["required"], name
            assert "schema_version" in sch["required"], name

    def test_render_unknown_format_raises(self) -> None:
        with pytest.raises(ValueError):
            render("notARealFramework")

    def test_render_payload_shape(self) -> None:
        for fmt in ("openai", "anthropic", "mcp"):
            payload = render(fmt)
            assert payload["format"] == fmt
            assert isinstance(payload["tools"], list)
            assert payload["output_schemas"]

    @pytest.mark.parametrize(
        ("fmt", "filename"),
        [
            ("openai", "hermes_tools.openai.json"),
            ("anthropic", "hermes_tools.anthropic.json"),
            ("mcp", "hermes_tools.mcp.json"),
        ],
    )
    def test_static_file_matches_render(self, fmt: str, filename: str) -> None:
        path = TOOLS_DIR / filename
        on_disk = json.loads(path.read_text("utf-8"))
        msg = f"{filename} stale; rerun `hermes-market tools --format {fmt}`."
        assert on_disk == render(fmt), msg

    def test_static_output_schemas_match(self) -> None:
        on_disk = json.loads((TOOLS_DIR / "output_schemas.json").read_text("utf-8"))
        assert on_disk == get_output_schemas()
