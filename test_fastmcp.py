from mcp.server.fastmcp import FastMCP
mcp = FastMCP("test")

@mcp.tool()
def sample_tool() -> str:
    return "test"

print("Dir:", dir(mcp))

