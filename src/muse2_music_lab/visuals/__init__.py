"""Phase 8 visuals subpackage.

Currently exposes the one-shot seed image generator. Phase 9 will add
the Three.js shader assets (those live in static/, not here -- this
package is for Python-side visual asset production).
"""

from muse2_music_lab.visuals.seed_image import (
    SeedImageError,
    SeedOptions,
    generate_seed_image,
    maybe_load_existing_seed,
    run_initial_seed_loop,
)

__all__ = [
    "SeedImageError",
    "SeedOptions",
    "generate_seed_image",
    "maybe_load_existing_seed",
    "run_initial_seed_loop",
]
