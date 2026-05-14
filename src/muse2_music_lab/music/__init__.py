"""Music generation + prompt safety layer.

Used by both `scripts/lyria_smoke.py` today and the Phase 5 orchestrator
tomorrow. Single source of truth for anything that touches Lyria.
"""

from muse2_music_lab.music.prompt_guard import PromptGuard, RewriteResult

__all__ = [
    "PromptGuard",
    "RewriteResult",
]
