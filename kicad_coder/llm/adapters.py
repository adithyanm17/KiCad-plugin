"""Convert the neutral tool definitions into each provider's wire format.

No vendor SDK is imported here -- these functions return plain dicts you pass
straight to whichever client you already use. Supporting a new provider means
adding one function.

Coverage:

* :func:`to_openai` -- OpenAI, and every OpenAI-compatible endpoint. That
  includes DeepSeek, Moonshot/Kimi, Mistral, Groq, Together, vLLM, Ollama's
  ``/v1`` and OpenRouter.
* :func:`to_anthropic` -- Claude (Opus, Sonnet, Haiku, Fable).
* :func:`to_gemini` -- Google Gemini function declarations.
* :func:`to_bedrock` -- Amazon Bedrock Converse tool config.
* :func:`to_ollama` -- Ollama's native ``/api/chat`` tool format.

Each also has a matching ``parse_*_calls`` helper that pulls tool calls out of
a response, so a driver loop can stay provider-neutral end to end.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Sequence, Tuple

__all__ = [
    "to_openai", "to_anthropic", "to_gemini", "to_bedrock", "to_ollama",
    "parse_openai_calls", "parse_anthropic_calls", "parse_gemini_calls",
    "for_provider", "PROVIDERS",
]

PROVIDERS = ("openai", "anthropic", "gemini", "bedrock", "ollama")

#: OpenAI-compatible endpoints, for documentation and the CLI's help text.
OPENAI_COMPATIBLE = (
    "openai", "deepseek", "kimi", "moonshot", "mistral", "groq",
    "together", "openrouter", "vllm", "xai", "fireworks",
)


def _schemas(tools) -> List[Dict[str, Any]]:
    """Accept either Tool objects or already-plain schema dicts."""
    out = []
    for t in tools:
        out.append(t.schema() if hasattr(t, "schema") else dict(t))
    return out


# -- OpenAI (and every OpenAI-compatible endpoint) ------------------------


def to_openai(tools) -> List[Dict[str, Any]]:
    """``tools=`` payload for chat completions.

    Works unchanged against DeepSeek, Kimi/Moonshot, Mistral, Groq, Together,
    OpenRouter and any vLLM or Ollama ``/v1`` deployment.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["parameters"],
            },
        }
        for s in _schemas(tools)
    ]


def parse_openai_calls(message) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Return ``[(call_id, tool_name, arguments)]`` from a response message."""
    if hasattr(message, "model_dump"):
        message = message.model_dump()
    elif hasattr(message, "to_dict"):
        message = message.to_dict()

    out: List[Tuple[str, str, Dict[str, Any]]] = []
    for call in (message or {}).get("tool_calls") or []:
        fn = call.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except ValueError:
            args = {}
        out.append((str(call.get("id", "")), str(fn.get("name", "")), args))
    return out


def openai_tool_result(call_id: str, result) -> Dict[str, Any]:
    """Build the ``role: tool`` message that answers one call."""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": result.content if hasattr(result, "content") else str(result),
    }


# -- Anthropic ------------------------------------------------------------


def to_anthropic(tools) -> List[Dict[str, Any]]:
    """``tools=`` payload for the Messages API (Opus, Sonnet, Haiku, Fable)."""
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "input_schema": s["parameters"],
        }
        for s in _schemas(tools)
    ]


def parse_anthropic_calls(response) -> List[Tuple[str, str, Dict[str, Any]]]:
    if hasattr(response, "model_dump"):
        response = response.model_dump()
    blocks = (response or {}).get("content") or []
    out: List[Tuple[str, str, Dict[str, Any]]] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append((str(block.get("id", "")), str(block.get("name", "")),
                        dict(block.get("input") or {})))
    return out


def anthropic_tool_result(call_id: str, result) -> Dict[str, Any]:
    ok = getattr(result, "ok", True)
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": result.content if hasattr(result, "content") else str(result),
        "is_error": not ok,
    }


# -- Google Gemini --------------------------------------------------------

_GEMINI_STRIP = ("additionalProperties", "$schema", "default", "examples",
                 "title", "const")


def _gemini_clean(schema: Any) -> Any:
    """Gemini accepts an OpenAPI 3.0 subset and rejects unknown keywords."""
    if isinstance(schema, dict):
        out = {}
        for k, v in schema.items():
            if k in _GEMINI_STRIP:
                continue
            out[k] = _gemini_clean(v)
        # An object with no declared properties must not carry an empty
        # "properties" map; Gemini rejects it.
        if out.get("type") == "object" and not out.get("properties"):
            out.pop("properties", None)
            out.pop("required", None)
        return out
    if isinstance(schema, list):
        return [_gemini_clean(v) for v in schema]
    return schema


def to_gemini(tools) -> List[Dict[str, Any]]:
    """``tools=`` payload: a single entry holding all function declarations."""
    decls = []
    for s in _schemas(tools):
        params = _gemini_clean(s["parameters"])
        decl = {"name": s["name"], "description": s["description"]}
        # Gemini rejects a parameterless function that declares empty params.
        if params.get("properties"):
            decl["parameters"] = params
        decls.append(decl)
    return [{"function_declarations": decls}]


def parse_gemini_calls(response) -> List[Tuple[str, str, Dict[str, Any]]]:
    if hasattr(response, "to_dict"):
        response = response.to_dict()
    out: List[Tuple[str, str, Dict[str, Any]]] = []
    for cand in (response or {}).get("candidates") or []:
        parts = ((cand.get("content") or {}).get("parts")) or []
        for part in parts:
            fc = part.get("functionCall") or part.get("function_call")
            if fc:
                name = str(fc.get("name", ""))
                out.append((name, name, dict(fc.get("args") or {})))
    return out


def gemini_tool_result(name: str, result) -> Dict[str, Any]:
    return {
        "functionResponse": {
            "name": name,
            "response": {
                "content": result.content if hasattr(result, "content")
                else str(result)
            },
        }
    }


# -- Amazon Bedrock (Converse) -------------------------------------------


def to_bedrock(tools) -> Dict[str, Any]:
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": s["name"],
                    "description": s["description"],
                    "inputSchema": {"json": s["parameters"]},
                }
            }
            for s in _schemas(tools)
        ]
    }


# -- Ollama native --------------------------------------------------------


def to_ollama(tools) -> List[Dict[str, Any]]:
    """Ollama's native ``/api/chat`` format (its ``/v1`` uses :func:`to_openai`)."""
    return to_openai(tools)


# -- dispatch -------------------------------------------------------------


def for_provider(provider: str, tools):
    """Return the tool payload for a named provider.

    Any OpenAI-compatible vendor name maps to the OpenAI shape.
    """
    p = (provider or "").strip().lower()
    if p in OPENAI_COMPATIBLE:
        return to_openai(tools)
    if p in ("anthropic", "claude"):
        return to_anthropic(tools)
    if p in ("gemini", "google", "vertex"):
        return to_gemini(tools)
    if p == "bedrock":
        return to_bedrock(tools)
    if p == "ollama":
        return to_ollama(tools)
    raise ValueError(
        "unknown provider %r -- known: %s (plus any OpenAI-compatible "
        "endpoint: %s)"
        % (provider, ", ".join(PROVIDERS), ", ".join(OPENAI_COMPATIBLE))
    )
