"""Shared Anthropic client. Handles API key loading, model defaults, and prompt caching."""
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

DEFAULT_MODEL = "claude-sonnet-4-6"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def get_client() -> anthropic.Anthropic:
    load_dotenv(dotenv_path=ENV_PATH)
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "Error: ANTHROPIC_API_KEY not set.\n"
            "  1. Copy .env.example to .env\n"
            "  2. Add your key from https://console.anthropic.com/",
            file=sys.stderr,
        )
        sys.exit(1)
    return anthropic.Anthropic(api_key=api_key)


def cached_system_block(text: str) -> list[dict]:
    """Wrap a system prompt as a cacheable block.

    Used for stable content (profile, strategy docs) that's reused across calls.
    """
    return [
        {
            "type": "text",
            "text": text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def run_tool_loop(
    client: anthropic.Anthropic,
    *,
    system: list[dict] | str,
    messages: list[dict],
    tools: list[dict],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 8096,
    on_tool_use=None,
):
    """Run an agentic loop with Claude: repeatedly call the API, feed server-side tool
    results back, until stop_reason is end_turn.

    `on_tool_use(tool_use_blocks)` is called each iteration for progress reporting.
    Returns the final assistant content blocks.
    """
    final_content = None

    while True:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system if isinstance(system, list) else system,
            tools=tools,
            messages=messages,
        )

        # Server-side tools (web_search, code_execution) complete within the same
        # API call — their block type is `server_tool_use`, not `tool_use`. We only
        # need to pass tool_result blocks back for client-side tool_use blocks.
        client_tool_uses = [b for b in response.content if b.type == "tool_use"]
        server_tool_uses = [b for b in response.content if b.type == "server_tool_use"]
        all_tool_uses = client_tool_uses + server_tool_uses

        if all_tool_uses and on_tool_use is not None:
            on_tool_use(all_tool_uses)

        if response.stop_reason == "end_turn":
            final_content = response.content
            break

        if response.stop_reason == "tool_use" and client_tool_uses:
            messages.append({"role": "assistant", "content": response.content})
            tool_results = [
                {"type": "tool_result", "tool_use_id": tu.id, "content": ""}
                for tu in client_tool_uses
            ]
            messages.append({"role": "user", "content": tool_results})
            continue

        # No client-side tool uses remaining, or unexpected stop_reason — return
        final_content = response.content
        break

    return final_content


def extract_text(content_blocks) -> str:
    """Pull text out of assistant content blocks. Returns concatenated text."""
    return "\n".join(b.text for b in content_blocks if getattr(b, "type", None) == "text")
