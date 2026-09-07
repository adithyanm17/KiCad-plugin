"""Drive the tool set with a real language model, on any provider.

The loop is identical for every vendor; only the client call and the two
adapter functions change. Run with no arguments to see a dry run that uses a
scripted "model" and needs no API key:

    python examples/agent_loop.py                  # scripted, no network
    python examples/agent_loop.py anthropic        # needs ANTHROPIC_API_KEY
    python examples/agent_loop.py openai           # needs OPENAI_API_KEY
    python examples/agent_loop.py deepseek         # needs DEEPSEEK_API_KEY
    python examples/agent_loop.py gemini           # needs GEMINI_API_KEY

Each provider branch is a dozen lines. Copy the one you need into your own
code; there is nothing else to it.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_coder.llm.adapters import (parse_anthropic_calls, parse_gemini_calls,
                                      parse_openai_calls, to_anthropic,
                                      to_gemini, to_openai)
from kicad_coder.llm.prompts import system_prompt
from kicad_coder.llm.tools import DesignSession

TASK = (
    "Design a small I2C temperature sensor breakout: a TMP102 in SOIC-8, "
    "proper decoupling, 4.7k pull-ups on SDA and SCL, and a 4-pin 2.54mm "
    "header carrying 3V3, GND, SDA and SCL. Board 30x20mm. "
    "Validate, place, generate the board and write the BOM, then summarise "
    "what you built and what a human should check."
)

MAX_TURNS = 24


# -- Anthropic (Claude: Opus, Sonnet, Haiku, Fable) -----------------------


def run_anthropic(session: DesignSession, task: str) -> None:
    import anthropic

    client = anthropic.Anthropic()
    tools = to_anthropic(session.tools)
    messages = [{"role": "user", "content": task}]

    for turn in range(MAX_TURNS):
        resp = client.messages.create(
            model=os.environ.get("MODEL", "claude-opus-5"),
            max_tokens=4096,
            system=system_prompt("design"),
            tools=tools,
            messages=messages,
        ).model_dump()

        messages.append({"role": "assistant", "content": resp["content"]})
        for block in resp["content"]:
            if block.get("type") == "text" and block.get("text", "").strip():
                print("\n[model] %s" % block["text"].strip())

        calls = parse_anthropic_calls(resp)
        if not calls:
            return

        results = []
        for call_id, name, args in calls:
            result = session.call(name, args)
            print("  -> %-18s %s" % (name, "ok" if result.ok else "FAILED"))
            results.append({"type": "tool_result", "tool_use_id": call_id,
                            "content": result.content, "is_error": not result.ok})
        messages.append({"role": "user", "content": results})


# -- OpenAI-compatible: OpenAI, DeepSeek, Kimi, Mistral, Groq, ... --------

_OPENAI_ENDPOINTS = {
    "openai": (None, "gpt-4o", "OPENAI_API_KEY"),
    "deepseek": ("https://api.deepseek.com", "deepseek-chat", "DEEPSEEK_API_KEY"),
    "kimi": ("https://api.moonshot.cn/v1", "moonshot-v1-32k", "MOONSHOT_API_KEY"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-32k", "MOONSHOT_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-large-latest", "MISTRAL_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", "qwen2.5:14b", ""),
}


def run_openai_compatible(session: DesignSession, task: str, vendor: str) -> None:
    from openai import OpenAI

    base_url, default_model, key_env = _OPENAI_ENDPOINTS[vendor]
    api_key = os.environ.get(key_env, "") if key_env else "ollama"
    client = OpenAI(base_url=base_url, api_key=api_key or "unset")

    tools = to_openai(session.tools)
    messages = [
        {"role": "system", "content": system_prompt("design")},
        {"role": "user", "content": task},
    ]

    for turn in range(MAX_TURNS):
        resp = client.chat.completions.create(
            model=os.environ.get("MODEL", default_model),
            messages=messages,
            tools=tools,
        )
        message = resp.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if message.content:
            print("\n[model] %s" % message.content.strip())

        calls = parse_openai_calls(message)
        if not calls:
            return

        for call_id, name, args in calls:
            result = session.call(name, args)
            print("  -> %-18s %s" % (name, "ok" if result.ok else "FAILED"))
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": result.content})


# -- Google Gemini --------------------------------------------------------


def run_gemini(session: DesignSession, task: str) -> None:
    import google.generativeai as genai

    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel(
        os.environ.get("MODEL", "gemini-2.0-flash"),
        tools=to_gemini(session.tools),
        system_instruction=system_prompt("design"),
    )
    chat = model.start_chat()
    message = task

    for turn in range(MAX_TURNS):
        resp = chat.send_message(message)
        calls = parse_gemini_calls(resp.to_dict())

        text = "".join(
            p.get("text", "")
            for c in resp.to_dict().get("candidates", [])
            for p in (c.get("content") or {}).get("parts", [])
        ).strip()
        if text:
            print("\n[model] %s" % text)

        if not calls:
            return

        replies = []
        for _, name, args in calls:
            result = session.call(name, args)
            print("  -> %-18s %s" % (name, "ok" if result.ok else "FAILED"))
            replies.append(genai.protos.Part(
                function_response=genai.protos.FunctionResponse(
                    name=name, response={"content": result.content})))
        message = replies


# -- scripted dry run (no network, no key) --------------------------------


def run_scripted(session: DesignSession) -> None:
    """The same call sequence a competent model produces, hard-coded."""
    script = [
        ("create_design", {"name": "tmp102-breakout",
                           "description": "I2C temperature sensor breakout",
                           "width_mm": 30, "height_mm": 20}),
        ("search_footprints", {"query": "SOIC-8", "limit": 3}),
        ("add_net_class", {"name": "Power", "track_width_mm": 0.5,
                           "clearance_mm": 0.25}),
        ("add_components", {"components": [
            {"ref": "U1", "value": "TMP102", "mpn": "TMP102AIDRLR",
             "manufacturer": "Texas Instruments",
             "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
             "description": "I2C temperature sensor"},
            {"ref": "C1", "value": "100n", "mpn": "CL10B104KB8NNNC",
             "manufacturer": "Samsung", "near": ["U1"],
             "footprint": "Capacitor_SMD:C_0603_1608Metric",
             "description": "Decoupling capacitor"},
            {"ref": "C2", "value": "10u", "mpn": "CL21A106KAYNNNE",
             "manufacturer": "Samsung",
             "footprint": "Capacitor_SMD:C_0805_2012Metric",
             "description": "Bulk capacitor"},
            {"ref": "R1", "value": "4k7", "mpn": "RC0603FR-074K7L",
             "manufacturer": "Yageo",
             "footprint": "Resistor_SMD:R_0603_1608Metric",
             "description": "SDA pull-up"},
            {"ref": "R2", "value": "4k7", "mpn": "RC0603FR-074K7L",
             "manufacturer": "Yageo",
             "footprint": "Resistor_SMD:R_0603_1608Metric",
             "description": "SCL pull-up"},
            {"ref": "J1", "value": "I2C", "mpn": "61300411121",
             "manufacturer": "Wurth",
             "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"},
            {"ref": "TP1", "value": "3V3",
             "footprint": "TestPoint:TestPoint_Pad_D1.5mm",
             "exclude_from_bom": True},
        ]}),
        ("connect", {"nets": [
            {"name": "GND", "net_class": "Power", "connections": [
                {"ref": "U1", "pad": "4"}, {"ref": "C1", "pad": "2"},
                {"ref": "C2", "pad": "2"}, {"ref": "J1", "pad": "2"}]},
            {"name": "+3V3", "net_class": "Power", "connections": [
                {"ref": "U1", "pad": "8"}, {"ref": "C1", "pad": "1"},
                {"ref": "C2", "pad": "1"}, {"ref": "J1", "pad": "1"},
                {"ref": "R1", "pad": "1"}, {"ref": "R2", "pad": "1"},
                {"ref": "TP1", "pad": "1"}]},
            {"name": "SDA", "connections": [
                {"ref": "U1", "pad": "1"}, {"ref": "R1", "pad": "2"},
                {"ref": "J1", "pad": "3"}]},
            {"name": "SCL", "connections": [
                {"ref": "U1", "pad": "2"}, {"ref": "R2", "pad": "2"},
                {"ref": "J1", "pad": "4"}]},
            {"name": "ALERT", "connections": [
                {"ref": "U1", "pad": "3"}, {"ref": "J1", "pad": "4"}]},
        ]}),
        ("validate_design", {}),
        ("remove_net", {"name": "ALERT"}),
        ("validate_design", {}),
        ("place_components", {"seed": 4}),
        ("generate_board", {}),
        ("generate_bom", {"format": "md"}),
        ("review_design", {}),
    ]

    for name, args in script:
        result = session.call(name, args)
        print("\n>>> %s(%s)" % (name, _short(args)))
        status = "ok" if result.ok else "FAILED (%s)" % result.error_code
        print("    [%s]" % status)
        body = result.content
        if name == "review_design":
            body = body.split("\n\nDesign facts")[0]
        for line in body.splitlines()[:14]:
            print("    " + line)


def _short(args) -> str:
    text = json.dumps(args)
    return text if len(text) <= 70 else text[:67] + "..."


def main() -> int:
    provider = (sys.argv[1] if len(sys.argv) > 1 else "scripted").lower()
    session = DesignSession(work_dir="build")

    print("kicad_coder agent loop -- provider: %s" % provider)
    print(session.library.status())

    if provider == "scripted":
        run_scripted(session)
    elif provider in ("anthropic", "claude"):
        run_anthropic(session, TASK)
    elif provider in _OPENAI_ENDPOINTS:
        run_openai_compatible(session, TASK, provider)
    elif provider in ("gemini", "google"):
        run_gemini(session, TASK)
    else:
        print("unknown provider %r; known: scripted, anthropic, gemini, %s"
              % (provider, ", ".join(sorted(_OPENAI_ENDPOINTS))))
        return 2

    print()
    print("=" * 60)
    print(session.design.summary())
    print("%d tool call(s), %d failed"
          % (len(session.history),
             sum(1 for h in session.history if not h["ok"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
