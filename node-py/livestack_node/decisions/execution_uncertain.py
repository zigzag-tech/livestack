"""An engine transport failure is not an execution-cessation receipt."""
from .contract import ContractError


class ExecutionUnconfirmed(ContractError):
    """Retain local capacity, busy state and the broker reservation.

    This marker carries no request history. Retained local slots are bounded
    by the existing unit queue's concurrency limit. Recovery requires actual
    cessation evidence; caller expiry and transport closure cannot release it.
    """
