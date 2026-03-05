import asyncio
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def verify():
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
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            
            # 1. Initial Tool Discovery (Should only see core tools)
            print("\nStep 1: Initial Discovery")
            resp = await session.list_tools()
            print(f"Tools available: {[t.name for t in resp.tools]}")
            
            # 2. List Skills
            print("\nStep 2: Listing Skills")
            res = await session.call_tool("list_skills", {})
            print(res.content[0].text)
            
            # 3. Request Skill (Unlocking)
            print("\nStep 3: Requesting 'fs-ops' Skill")
            res = await session.call_tool("request_skill", {"name": "fs-ops"})
            print(f"Activation Result:\n{res.content[0].text[:100]}...")
            
            # 4. Post-Activation Discovery (Should see local-fs tools)
            print("\nStep 4: Discovery After Activation")
            resp = await session.list_tools()
            print(f"Tools available: {[t.name for t in resp.tools]}")
            
            # 5. Call an Unlocked Tool
            if any(t.name == "local-fs_list_allowed_directories" for t in resp.tools):
                print("\nStep 5: Calling Unlocked Tool 'local-fs_list_allowed_directories'")
                res = await session.call_tool("local-fs_list_allowed_directories", {})
                print(f"Result: {res.content[0].text}")
            else:
                print("\nError: Unlocked tools not found in list_tools!")

if __name__ == "__main__":
    asyncio.run(verify())
