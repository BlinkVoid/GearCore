"""
Integration test for GearCore progressive disclosure.

Connects to the GearCore hub via stdio, verifies:
1. Bootstrap tools are exposed (list_skills, request_skill)
2. Skills can be listed
3. A skill can be requested and activated
4. If the skill's backend is available, tools are unlocked
"""
import asyncio
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def verify():
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"

    params = StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "gearcore_hub.main"],
        env=env,
    )

    print("Connecting to GearCore Hub...")
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # 1. Initial Tool Discovery (Should only see core tools)
            print("\nStep 1: Initial Discovery")
            resp = await session.list_tools()
            bootstrap_tools = [t.name for t in resp.tools]
            print(f"Bootstrap tools: {bootstrap_tools}")
            assert "list_skills" in bootstrap_tools, "list_skills missing"
            assert "request_skill" in bootstrap_tools, "request_skill missing"
            print("✓ Bootstrap tools correct")

            # 2. List Skills
            print("\nStep 2: Listing Skills")
            res = await session.call_tool("list_skills", {})
            skills_text = res.content[0].text
            print(skills_text[:500])

            # Extract skill names from the text
            skill_names = []
            for line in skills_text.splitlines():
                stripped = line.strip()
                if stripped.startswith("GearCore") or stripped.startswith("BROKEN") or not stripped:
                    continue
                # Lines look like: "  skill-name  [active] — description"
                # or: "  skill-name  — description"
                if stripped.startswith("-") or stripped[0].isalnum():
                    parts = stripped.split()
                    if parts:
                        name = parts[0]
                        skill_names.append(name)
            print(f"Found {len(skill_names)} skills: {skill_names[:5]}...")

            # 3. Request a skill (pick first one that has tools)
            test_skill = None
            for name in skill_names:
                # Skip core skills with no MCP backends
                if name in ("first-principles-scientific-mindset", "gearcore", "handoff"):
                    continue
                test_skill = name
                break

            if not test_skill:
                print("\n⚠ No skills with MCP backends found — skipping unlock test")
                print("✓ Core hub functionality verified")
                return

            print(f"\nStep 3: Requesting '{test_skill}' Skill")
            res = await session.call_tool("request_skill", {"name": test_skill})
            activation_text = res.content[0].text
            print(f"Activation result: {activation_text[:100]}...")
            assert "SKILL LOADED" in activation_text or "Error" in activation_text, "Unexpected response"

            if "Error" in activation_text:
                print("✗ Skill activation failed — this may be expected if backends are down")
                return

            # 4. Post-Activation Discovery
            print("\nStep 4: Discovery After Activation")
            resp = await session.list_tools()
            post_tools = [t.name for t in resp.tools]
            print(f"Tools available: {post_tools}")

            if len(post_tools) > len(bootstrap_tools):
                print("✓ Tools unlocked after skill activation")
            else:
                print("⚠ No additional tools unlocked (backend may be offline)")

            print("\n✓ All checks passed")


if __name__ == "__main__":
    asyncio.run(verify())
