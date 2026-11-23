class LLMError(Exception):
    """Base for all LLM-layer errors."""


class LLMUnavailable(LLMError):
    """Provider unreachable or returned 5xx/timeout."""

    def __init__(self, message: str, *, diagnostics: dict[str, object] | None = None) -> None:
        self.diagnostics = diagnostics
        super().__init__(message)


class LLMBudgetExceeded(LLMError):
    """Daily or monthly budget for cloud LLM exceeded."""


class LLMBillingExceeded(LLMBudgetExceeded):
    """Known provider debit exceeded the reservation after transport."""


class LLMSchemaError(LLMError):
    """LLM response does not match requested schema."""


class LLMPricingUnavailable(LLMError):
    """Current model price or currency evidence is unavailable."""


class LLMUsageUnavailable(LLMUnavailable):
    """Provider completed but did not provide trustworthy usage."""


class LLMResponseUnavailable(LLMUnavailable):
    """Invalid final answer with content-free response evidence."""

    def __init__(self, diagnostics: dict[str, object]) -> None:
        super().__init__("Модель не вернула полный итоговый ответ. Автоповтора нет; списание учитывается отдельно.", diagnostics=diagnostics)
