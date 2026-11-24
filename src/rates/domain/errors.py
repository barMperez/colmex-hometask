"""Domain errors. The API layer maps each to a problem-details response."""


class DomainError(Exception):
    """Base class for expected, classified failures."""


class InvalidRequest(DomainError):
    pass


class UnsupportedPair(DomainError):
    def __init__(self, base: str, quote: str) -> None:
        super().__init__(f"Currency pair {base}/{quote} is not supported or not enabled")
        self.base = base
        self.quote = quote


class RateNotFound(DomainError):
    def __init__(self, base: str, quote: str) -> None:
        super().__init__(f"No rate has been ingested yet for {base}/{quote}")
        self.base = base
        self.quote = quote


class PolicyNotEvaluable(DomainError):
    """The freshness policy cannot be evaluated; callers must fail closed."""


class StorageUnavailable(DomainError):
    pass


class AuditUnavailable(DomainError):
    """The audit record for a rate could not be stored: the rate must not be returned."""


class SnapshotRejected(DomainError):
    """A BoI snapshot failed validation; nothing from it may be stored."""


class AlreadyDeclared(DomainError):
    pass
