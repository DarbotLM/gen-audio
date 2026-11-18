"""Errors for the Agent Yankers show module."""


class AgentYankersError(ValueError):
    """A show document, consent check, or stubbed integration failed.

    ``code`` is the stable token tests assert on.
    """

    def __init__(self, message: str, code: str = "invalid"):
        self.code = code
        text = message if message.startswith(f"{code}:") else f"{code}: {message}"
        super().__init__(text)


class SchemaError(AgentYankersError):
    """A document failed the JSON Schema for its kind."""

    def __init__(self, path: str, message: str):
        self.path = path
        super().__init__(f"{path}: {message}", code="schema")


class ConsentRefusal(AgentYankersError):
    """One or more marks lack recorded two-party consent. No call was placed."""

    def __init__(self, refusals: list[tuple[str, list[str]]]):
        self.refusals = [(mark_id, list(reasons)) for mark_id, reasons in refusals]
        parts = [f"{mark_id} ({'; '.join(reasons)})" for mark_id, reasons in self.refusals]
        detail = ", ".join(parts) if parts else "no mark was named"
        super().__init__(
            f"refused marks without recorded consent: {detail}. No call was placed.",
            code="consent_refused",
        )


class IntegrationUnavailable(AgentYankersError):
    """A LiveKit or VibeVoice call cannot run. No audio was written."""

    def __init__(self, integration: str, missing: list[str]):
        self.integration = integration
        self.missing = [item for item in missing if item]
        if not self.missing:
            self.missing = [f"{integration} is not runnable"]
        detail = "; ".join(self.missing)
        super().__init__(
            f"{integration} is not runnable: {detail}. No audio was written.",
            code="unavailable",
        )
