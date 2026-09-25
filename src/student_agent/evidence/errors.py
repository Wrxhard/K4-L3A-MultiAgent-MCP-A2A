class EvidenceToolError(RuntimeError):
    """An MCP tool failure classified without inventing business facts."""

    def __init__(self, tool_name: str, message: str, *, not_found: bool = False) -> None:
        super().__init__(f"MCP tool {tool_name} failed: {message}")
        self.tool_name = tool_name
        self.not_found = not_found
