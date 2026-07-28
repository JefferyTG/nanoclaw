"""Control-plane policy for trusted, high-capability scene agents.

Scene Profiles may opt into any ordinary execution/data tool.  Only APIs that
directly mutate Agent definitions or private capability assets are reserved for
the main Agent/user control plane.
"""


SCENE_FORBIDDEN_TOOLS = frozenset(
    {
        "create_agent",
        "update_agent",
        "delete_agent",
        "create_agent_skill",
        "update_agent_skill",
        "delete_agent_skill",
        "create_agent_tool",
        "update_agent_tool",
        "delete_agent_tool",
    }
)
