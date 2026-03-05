import asyncio
import sys
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def verify():
    # Parameters to start the hub as a stdio process
    import os
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    
    params = StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "gearcore_hub.main"],
        env=env
    )
    
    print("Connecting to GearCore Hub...")
    async with stdio_client(params) as (read_stream, write_stream):
        # We can't easily capture stderr from stdio_client in this simple script 
        # but we can see if it initializes.
        async with ClientSession(read_stream, write_stream) as session:
            # Initialize the session
            await session.initialize()
            
            # List all tools discovered by the Hub
            print("\nListing Tools from Hub:")
            resp = await session.list_tools()
            for tool in resp.tools:
                print(f"- {tool.name}: {tool.description[:50]}...")
            
            if not resp.tools:
                print("No tools found! Ensure 'npx' is in your path and 'gearcore.yaml' is correct.")
            else:
                print(f"\nSuccess! Hub aggregated {len(resp.tools)} tools from backends.")

if __name__ == "__main__":
    asyncio.run(verify())
