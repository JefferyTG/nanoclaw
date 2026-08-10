"""NanoClaw reminders regression tests package."""
from pathlib import Path

_PRODUCTION_REMINDERS = Path(__file__).resolve().parents[2] / "reminders"
if str(_PRODUCTION_REMINDERS) not in __path__:
    __path__.append(str(_PRODUCTION_REMINDERS))
