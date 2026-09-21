from __future__ import annotations

from typing import Any

import httpx

from court_search.domain.contracts import (
    CaseDecisionsRequest,
    FilterLookupRequest,
    ReadRequest,
    SearchRequest,
)

TOOL_DISPATCH = {
    "search_decisions": ("POST", "/api/v1/search", SearchRequest),
    "get_decision": ("POST", "/api/v1/decisions/read", ReadRequest),
    "get_case_decisions": ("POST", "/api/v1/cases/decisions", CaseDecisionsRequest),
    "lookup_filters": ("POST", "/api/v1/filters/lookup", FilterLookupRequest),
}


def tool_schemas() -> list[dict[str, Any]]:
    """OpenAI-style function schemas exported from the same Pydantic contracts."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": model.__doc__ or name,
                "parameters": model.model_json_schema(),
            },
        }
        for name, (_method, _path, model) in TOOL_DISPATCH.items()
    ] + [
        {
            "type": "function",
            "function": {
                "name": "get_corpus_info",
                "description": "Inspect installed coverage and capabilities",
                "parameters": {
                    "type": "object",
                    "properties": {"corpus_generation": {"type": ["string", "null"]}},
                    "additionalProperties": False,
                },
            },
        }
    ]


def dispatch(client: httpx.Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "get_corpus_info":
        generation = arguments.get("corpus_generation")
        path = "/api/v1/corpus"
        if generation:
            path += f"?corpus_generation={generation}"
        response = client.get(path)
        response.raise_for_status()
        return response.json()
    if name not in TOOL_DISPATCH:
        raise ValueError(f"Unknown tool: {name}")
    method, path, model = TOOL_DISPATCH[name]
    payload = model.model_validate(arguments).model_dump(mode="json")
    response = client.request(method, path, json=payload)
    response.raise_for_status()
    return response.json()
