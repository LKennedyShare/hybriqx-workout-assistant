"""Locally invoke a real framework tool, offline only; no model key needed."""

import argparse
import asyncio
import json
import os

from hybriqx_workout_assistant.tools import HybriqxWorkoutTools


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", choices=("langchain", "openai"), required=True)
    args = parser.parse_args()
    # This executable is deliberately offline even if the host enables tracing.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    factory = HybriqxWorkoutTools()
    if args.framework == "langchain":
        tool = factory.langchain_tools()[0]
        result = await tool.ainvoke({})
    else:
        from agents.tool_context import ToolContext

        tool = factory.openai_tools()[0]
        context = ToolContext(context=None, tool_name=tool.name,
                              tool_call_id="offline-demo", tool_arguments="{}")
        result = await tool.on_invoke_tool(context, "{}")
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
