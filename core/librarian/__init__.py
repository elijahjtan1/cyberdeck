"""Offline retrieval interface for Phase 3. Importing performs no I/O."""
from .rank import Passage

__all__ = ['Passage', 'retrieve']


def retrieve(question, budget_tokens=1400, *, telemetry=None):
    from .service import Retriever
    return Retriever.from_environment(telemetry).retrieve(question, budget_tokens)
