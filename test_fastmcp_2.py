from mcp.server.fastmcp import FastMCP
mcp = FastMCP("test")

@mcp.tool()
def sample_tool() -> str:
    return "test"

tools = mcp.list_tools()
print("Tools type:", type(tools))
if tools:
    print("Tool type:", type(tools[0]))
    print("Tool:", tools[0])

