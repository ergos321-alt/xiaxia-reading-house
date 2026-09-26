from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import yaml

import mcp_app


def _tools():
    return asyncio.run(mcp_app.reading_mcp.list_tools())


def test_mcp_discovery_matches_all_openapi_operations():
    spec = yaml.safe_load((Path(__file__).parents[1] / "openapi.yaml").read_text(encoding="utf-8"))
    expected = {
        operation["operationId"]
        for path in spec["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    tools = _tools()
    assert {tool.name for tool in tools} == expected
    assert len(tools) == len(expected) == 19


def test_mcp_argument_names_and_requiredness_match_openapi():
    spec = yaml.safe_load((Path(__file__).parents[1] / "openapi.yaml").read_text(encoding="utf-8"))
    tools = {tool.name: tool for tool in _tools()}
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            operation_id = operation.get("operationId")
            if not operation_id:
                continue
            expected_properties = {parameter["name"] for parameter in operation.get("parameters", [])}
            expected_required = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter.get("required")
            }
            request_body = operation.get("requestBody", {})
            body_schema = request_body.get("content", {}).get("application/json", {}).get("schema", {})
            body_ref = body_schema.get("$ref")
            if body_ref:
                body_schema = spec["components"]["schemas"][body_ref.rsplit("/", 1)[-1]]
            body_properties = set(body_schema.get("properties", {}))
            expected_properties |= body_properties
            if request_body.get("required"):
                expected_required |= set(body_schema.get("required", []))
            schema = tools[operation_id].input_schema
            assert set(schema["properties"]) == expected_properties, operation_id
            assert set(schema.get("required", [])) == expected_required, operation_id


def test_mcp_schemas_keep_required_fields_defaults_and_bounds():
    tools = {tool.name: tool for tool in _tools()}
    assert tools["listXiaxiaThoughts"].input_schema["required"] == ["book_id"]
    assert tools["replyToAnnotation"].input_schema["required"] == ["annotation_id", "response"]
    assert tools["createXiaxiaThought"].input_schema["required"] == [
        "book_id", "chapter_id", "scope", "content"
    ]
    assert tools["listBooks"].input_schema["properties"]["limit"]["default"] == 10
    assert tools["listBooks"].input_schema["properties"]["limit"]["maximum"] == 50
    assert tools["previewXiaxiaThoughts"].input_schema["properties"]["candidates"]["minItems"] == 1
    assert tools["previewXiaxiaThoughts"].input_schema["properties"]["candidates"]["maxItems"] == 50
    assert tools["createXiaxiaThought"].input_schema["properties"]["content"]["maxLength"] == 50_000


def test_read_tool_forwards_query_to_existing_route():
    book_id = uuid4()
    result_body = {"filter": "all", "annotations": [], "xiaxia_thoughts": [], "counts": {}}
    with patch.object(mcp_app, "_request", return_value=result_body) as request:
        result = mcp_app.listBookAnnotations(book_id, "all")
    assert result == result_body
    request.assert_called_once_with(
        "GET", f"/api/books/{book_id}/annotations", params={"filter": "all"}
    )


def test_write_tool_forwards_existing_http_method_path_and_json():
    annotation_id = uuid4()
    response_body = {"reply": {"response": "A response"}}
    with patch.object(mcp_app, "_request", return_value=response_body) as request:
        result = mcp_app.updateAnnotationReply(annotation_id, "A response")
    assert result == response_body
    request.assert_called_once_with(
        "PATCH", f"/api/annotations/{annotation_id}/reply", body={"response": "A response"}
    )


def test_ai_progress_omits_unset_optional_fields():
    book_id = uuid4()
    with patch.object(mcp_app, "_request", return_value={"ok": True}) as request:
        mcp_app.saveAiProgress(book_id)
    request.assert_called_once_with(
        "POST", "/api/ai/progress", body={"book_id": str(book_id)}
    )


def test_api_adapter_reuses_action_bearer_and_returns_existing_json(monkeypatch):
    monkeypatch.setenv("PORT", "19999")
    monkeypatch.setenv("ACTION_API_TOKEN", "unit-test-token")
    response_body = {"current": None, "message": "empty"}
    response = type("Response", (), {"json": lambda self: response_body})()
    with patch.object(mcp_app.httpx, "Client") as client_type:
        client_type.return_value.__enter__.return_value.request.return_value = response
        result = mcp_app._request("GET", "/api/reading/state")
    assert result is response_body
    client_type.return_value.__enter__.return_value.request.assert_called_once_with(
        "GET",
        "/api/reading/state",
        params={},
        json=None,
        headers={"Authorization": "Bearer unit-test-token"},
    )
