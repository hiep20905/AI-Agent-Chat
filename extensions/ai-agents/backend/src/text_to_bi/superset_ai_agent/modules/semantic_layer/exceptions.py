from ...common.exceptions import ServiceUnavailableError, ValidationError


class SemanticLayerUnavailableError(ServiceUnavailableError):
    """The Semantic Layer could not be read: unreachable, the user lacks access, or it answered with an error."""

    message = "Không kết nối được tới Semantic Layer."


class SemanticLayerRefusedError(ValidationError):
    """The Semantic Layer refused or failed a request the agent made as the user; the message is its own.

    `status_code` is its HTTP answer, None when it was not reached; `transient` marks a failure the same request may
    not meet again (unreachable, busy or timed out).
    """

    message = "Semantic Layer từ chối yêu cầu."

    def __init__(self, message: str | None = None, status_code: int | None = None, transient: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient
