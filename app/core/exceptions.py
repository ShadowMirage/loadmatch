class LoadMatchError(Exception):
    """Base exception for the LoadMatch system."""
    pass

class CriticalLogicError(LoadMatchError):
    """Irrecoverable logic failure that should move to DEAD_LETTER."""
    pass

class TransientExecutionError(LoadMatchError):
    """Recoverable execution failure that should move to FAILED for retry."""
    pass

class ExternalDependencyError(TransientExecutionError):
    """External API (Anthropic, WhatsApp) failure with retry eligibility."""
    pass
