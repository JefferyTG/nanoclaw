"""NanoClaw agent regression tests package."""
from pathlib import Path

_PRODUCTION_AGENT = Path(__file__).resolve().parents[2] / "agent"
if str(_PRODUCTION_AGENT) not in __path__:
    __path__.append(str(_PRODUCTION_AGENT))
