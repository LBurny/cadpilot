from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters

# Agent configuration
AGENT_NAME = "cad_design_agent"
MODEL_NAME = "gemini-2.5-flash-lite"

# Basic instruction
BASIC_PROMPT = "You are a CAD designer."

# Initialize agent
root_agent = LlmAgent(
    model=MODEL_NAME,
    name=AGENT_NAME,
    instruction=BASIC_PROMPT,
    tools=[
        MCPToolset(
            connection_params=StdioServerParameters(command="uvx", args=["cadpilot"])
            # Dev mode (run from a local clone instead of PyPI):
            # StdioServerParameters(
            #     command="uv", args=["--directory", "/path/to/cadpilot", "run", "cadpilot"]
            # )
        )
    ],
)
