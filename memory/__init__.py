"""memory — per-app markdown memory store and knowledge base for Clicky Windows."""
from .store import MemoryStore
from . import kb

__all__ = ["MemoryStore", "kb"]
