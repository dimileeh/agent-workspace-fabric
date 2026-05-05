def run_test():
    import asyncio
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test")
    return asyncio.run(mcp.list_tools())

print(run_test())
