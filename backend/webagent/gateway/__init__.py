"""Structured actions over managed browsers; only the Worker may dispatch."""
from .service import BrowserGateway
from .permissions import WriteAuthorization

__all__ = ['BrowserGateway', 'WriteAuthorization']
