import asyncio

from mcp import Client

from p4_history.server import create_server


def test_mcp_tools_round_trip_and_errors(history):
    history.p4.add("example.txt", b"original")

    async def exercise():
        async with Client(create_server(history)) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert "p4_history_begin" in names
            assert "p4_history_apply_restore" in names
            assert {
                "p4_history_prepare",
                "p4_history_plan_merge",
                "p4_history_plan_switch",
                "p4_history_continue_task",
                "p4_history_label",
            } <= names
            result = await client.call_tool("p4_history_begin", {"change": "123"})
            assert not result.is_error
            assert result.structured_content["created"]
            milestone = await client.call_tool(
                "p4_history_commit", {"change": "123", "message": "Milestone validated by tests"}
            )
            assert milestone.structured_content["created"]
            assert milestone.structured_content["commit"] != result.structured_content["commit"]
            deduplicated = await client.call_tool(
                "p4_history_commit",
                {
                    "change": "123",
                    "message": "Routine checkpoint",
                    "allow_empty": False,
                },
            )
            assert not deduplicated.structured_content["created"]
            invalid = await client.call_tool(
                "p4_history_commit", {"change": "-bad", "message": "x"}
            )
            assert invalid.is_error

    asyncio.run(exercise())
