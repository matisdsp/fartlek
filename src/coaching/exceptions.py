class CoachingError(Exception):
    """Base for coaching context errors."""


class LLMError(CoachingError):
    """LLM provider returned an error."""


class ToolExecutionError(CoachingError):
    """A tool dispatcher failed unexpectedly (programming error, not a tool returning is_error)."""

    def __init__(self, tool_name: str, message: str):
        super().__init__(f"{tool_name}: {message}")
        self.tool_name = tool_name
