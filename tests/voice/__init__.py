"""Voice and ASR regression tests.

``unittest discover -s tests`` imports this package as top-level ``voice``.
Extend its lookup path with the production package so that test discovery does
not shadow ``<repo>/voice``.
"""

from pathlib import Path

_PRODUCTION_VOICE = Path(__file__).resolve().parents[2] / "voice"
if str(_PRODUCTION_VOICE) not in __path__:
    __path__.append(str(_PRODUCTION_VOICE))
