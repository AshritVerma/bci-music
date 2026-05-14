"""Phase 8: one-shot seed image generation via Google Imagen 3.

The session prompt -> a single PNG written to `static/seed.png`. The
file becomes the visual identity of the session: Phase 9's Three.js
shader loads it as the initial texture and EEG/audio uniforms warp /
blur / colorize it from there.

Why Imagen via google-genai (vs MLX-Diffusion / diffusers+MPS):

  * Zero new dependencies -- google-genai is already pinned for Lyria.
  * Zero local model weights to download (~7-24GB saved).
  * ~3-5s wall time, comparable to a 1-step SDXL Turbo render on this
    machine, faster than a full SDXL pipeline.
  * Same API key plumbing (GEMINI_API_KEY) and same rate-limit posture
    as the rest of the pipeline. One API to know.
  * Trade-off: each call is API quota / cost. The cache mitigates
    repeated dev iteration, and `--skip-seed` lets you bypass entirely
    when you don't care about the visual.

Lifecycle:

  * Called from `main.run()` BEFORE `asyncio.run(...)`. Synchronous on
    purpose: the image is needed by the visualizer at first render, so
    blocking startup until it lands is the simplest correct sequencing.
    (Backgrounding it would mean the orchestrator must signal the
    browser when the seed lands -- moot complexity for a 3-5s call.)

  * Cancellation: SIGINT during the API call propagates through google's
    SDK as a normal exception. We catch + print + return failure code.

  * Errors: missing API key, network / quota / safety-filter rejects all
    raise `SeedImageError` with a human-readable message. The caller
    surfaces it and decides whether to abort or proceed without a seed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from muse2_music_lab import config


class SeedImageError(RuntimeError):
    """Raised when seed generation fails for an actionable reason.

    Distinguishes "we know what's wrong, here's the fix" from a stack
    trace. The orchestrator catches this and surfaces the message
    without dumping a backtrace.
    """


@dataclass
class SeedOptions:
    """Knobs the orchestrator passes through to the generator."""

    prompt: str
    use_cache: bool = True   # False on `--no-seed-cache`: always regenerate
    output_path: Path = None  # type: ignore[assignment]  # filled in __post_init__

    def __post_init__(self) -> None:
        if self.output_path is None:
            self.output_path = _repo_root() / config.SEED_OUTPUT_PATH


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Walk up from this file -> repo root.

    `src/muse2_music_lab/visuals/seed_image.py` -> parents[3] is the
    repo root. Same idiom as `server/app.py._resolve_static_dir`.
    """
    return Path(__file__).resolve().parents[3]


def _cache_dir() -> Path:
    return _repo_root() / config.SEED_CACHE_DIR


def _cache_key(prompt: str) -> str:
    """Stable key spanning prompt + model + ratio.

    Including the model and ratio means swapping either invalidates the
    cache automatically -- you don't have to remember to wipe `static/cache/`
    after editing config. 16 hex chars (~64 bits) is plenty unique for a
    per-user dev cache; not worth the extra path width to use the full hash.
    """
    blob = (
        f"{config.SEED_MODEL_ID}|{config.SEED_ASPECT_RATIO}|{prompt}"
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _cache_path(prompt: str) -> Path:
    return _cache_dir() / f"{_cache_key(prompt)}.png"


# ---------------------------------------------------------------------------
# API key + image bytes (the only network-touching paths)
# ---------------------------------------------------------------------------


def _load_api_key() -> str:
    """Same pattern as lyria/session.py -- .env first, then env."""
    load_dotenv()
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise SeedImageError(
            "GEMINI_API_KEY is missing. Drop it in .env at the repo root "
            "(see .env.example) or export it in the shell before running "
            "`muse2 perform`. Or pass --skip-seed to bypass image generation."
        )
    return api_key


def _call_imagen(api_key: str, prompt: str) -> bytes:
    """One-shot Imagen call. Returns raw PNG bytes.

    Network/quota errors propagate as SeedImageError with a friendly
    message; everything else propagates raw so unknown failures aren't
    silently masked.
    """
    # Late SDK import keeps the package importable on non-google-genai
    # installs (matches lyria/session.py convention).
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    try:
        resp = client.models.generate_images(
            model=config.SEED_MODEL_ID,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio=config.SEED_ASPECT_RATIO,
                # PNG keeps the seed crisp for shader sampling. JPEG
                # compression artifacts would smear high-frequency
                # detail and contaminate the visualizer.
                output_mime_type="image/png",
            ),
        )
    except Exception as e:
        # Wrap with a hint about the most likely operator-fixable causes.
        raise SeedImageError(
            f"Imagen API call failed ({type(e).__name__}): {e}. "
            "Common causes: invalid GEMINI_API_KEY, quota exceeded, or "
            "the prompt was rejected by the safety filter. Try "
            "--skip-seed to proceed without an image."
        ) from e

    if not resp.generated_images:
        raise SeedImageError(
            "Imagen returned no images. The prompt may have been "
            "filtered. Try a less specific prompt, or --skip-seed."
        )

    img = resp.generated_images[0]
    if getattr(img, "rai_filtered_reason", None):
        raise SeedImageError(
            f"Imagen filtered the prompt: {img.rai_filtered_reason}. "
            "Try rephrasing without artist names, copyrighted characters, "
            "or sensitive content. Or pass --skip-seed."
        )

    image_bytes = getattr(img.image, "image_bytes", None)
    if not image_bytes:
        raise SeedImageError(
            "Imagen returned an image with no inline bytes. Likely a "
            "GCS-only response; this code path expects local bytes."
        )
    return bytes(image_bytes)


# ---------------------------------------------------------------------------
# Top-level entry point used by main.py
# ---------------------------------------------------------------------------


def generate_seed_image(opts: SeedOptions) -> Path:
    """Resolve cache, call Imagen if needed, write `static/seed.png`.

    Returns the absolute path written to. Raises SeedImageError on
    unrecoverable failure. Idempotent: a cache hit is a single file
    copy, no API call, no network.
    """
    prompt = opts.prompt.strip()
    if not prompt:
        raise SeedImageError("Cannot generate a seed image from an empty prompt.")

    output_path: Path = opts.output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cached = _cache_path(prompt)
    cached.parent.mkdir(parents=True, exist_ok=True)

    # ------- Cache hit -------
    if opts.use_cache and cached.is_file():
        # Copy (don't symlink) so static-file serving is independent of
        # the cache layout; future cache-clean operations can't break a
        # running server.
        shutil.copyfile(cached, output_path)
        size_kb = output_path.stat().st_size / 1024
        print(
            f"[seed] cache HIT ({_cache_key(prompt)}.png, "
            f"{size_kb:.0f} KB) -> {output_path.relative_to(_repo_root())}",
            flush=True,
        )
        return output_path

    # ------- Cache miss: call Imagen -------
    print(
        f"[seed] generating via {config.SEED_MODEL_ID} "
        f"(prompt={prompt!r}, aspect={config.SEED_ASPECT_RATIO})...",
        flush=True,
    )
    api_key = _load_api_key()

    t0 = time.monotonic()
    image_bytes = _call_imagen(api_key, prompt)
    elapsed = time.monotonic() - t0

    # Atomic-ish write: write to a tempfile alongside the target, then
    # rename. Prevents a SIGINT mid-write from leaving a half-PNG that
    # Phase 9's shader would refuse to load.
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_bytes(image_bytes)
    os.replace(tmp, output_path)

    # Mirror to the cache.
    if opts.use_cache:
        try:
            shutil.copyfile(output_path, cached)
        except OSError as e:
            # Cache write failure isn't fatal -- the seed itself is on
            # disk. Just warn so the operator can clear disk space.
            print(f"[seed] warn: couldn't update cache ({e})", flush=True)

    size_kb = output_path.stat().st_size / 1024
    print(
        f"[seed] generated in {elapsed:.1f}s ({size_kb:.0f} KB) -> "
        f"{output_path.relative_to(_repo_root())}  "
        f"[cache key {_cache_key(prompt)}]",
        flush=True,
    )
    return output_path


def maybe_load_existing_seed(output_path: Optional[Path] = None) -> Optional[Path]:
    """Return the path if a seed already exists, else None.

    Used by `--skip-seed` so the visualizer can still pick up a previous
    session's image instead of rendering a blank canvas.
    """
    path = output_path or (_repo_root() / config.SEED_OUTPUT_PATH)
    return path if path.is_file() else None


# ---------------------------------------------------------------------------
# Reusable building blocks (used by Phase 10 seed_evolver)
# ---------------------------------------------------------------------------
#
# The evolver wants to (a) call Imagen on a freshly-mutated prompt every
# N seconds, (b) write to static/seed.png atomically, (c) keep a cache.
# Same plumbing as Phase 8's one-shot, just driven by a different
# scheduler. Exposing these helpers lets the evolver reuse the
# Imagen-call path without duplicating the API-key-loading,
# error-wrapping, and atomic-write logic.

def load_api_key() -> str:
    """Public alias for the internal API-key loader. Re-exported so the
    evolver can do its own key check up front (and skip itself cleanly
    if the key is missing) without touching `_` internals."""
    return _load_api_key()


def call_imagen(api_key: str, prompt: str) -> bytes:
    """Public alias for the internal Imagen call. Returns raw PNG bytes
    or raises SeedImageError. Synchronous; the evolver wraps it in
    `loop.run_in_executor(...)` so it doesn't block the event loop
    during the 3-5s API call."""
    return _call_imagen(api_key, prompt)


def write_seed_atomic(image_bytes: bytes, prompt: str, *, use_cache: bool) -> Path:
    """Write the bytes to static/seed.png and (optionally) the cache.

    Atomic: writes to a tempfile and renames, so a SIGINT mid-write
    never leaves a half-PNG that the visualizer would refuse to load.
    Returns the canonical output path.
    """
    output_path = _repo_root() / config.SEED_OUTPUT_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_bytes(image_bytes)
    os.replace(tmp, output_path)

    if use_cache:
        cached = _cache_path(prompt)
        cached.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(output_path, cached)
        except OSError as e:
            print(f"[seed] warn: couldn't update cache ({e})", flush=True)
    return output_path


def cache_path_for(prompt: str) -> Path:
    """Public alias for `_cache_path(prompt)` so the evolver can check
    a cache hit before paying for an Imagen call on a previously-seen
    evolved prompt (rare but free)."""
    return _cache_path(prompt)


# ---------------------------------------------------------------------------
# Phase 10: deferred async wrapper
# ---------------------------------------------------------------------------

async def run_initial_seed_loop(state, *, use_cache: bool, skip: bool) -> None:
    """Generate the initial seed image once the user clicks Start.

    Phase 8 originally ran synchronously BEFORE asyncio.run() on the
    assumption that the prompt was always present at CLI launch. Phase
    10's browser-driven Start flow means we don't know the prompt
    until well after the orchestrator boots, so the seed step is now
    a deferred async task gated on `state.start_requested`.

    Lifecycle:
      1. Wait for start_requested.
      2. Read state.prompt (set by the server's WS action handler or
         by main.py if --prompt was passed on the CLI).
      3. Run the (blocking) Imagen call in the default executor.
      4. On success, bump state.seed_version so the browser refetches
         /static/seed.png and the visualizer cross-fades to it.
      5. On SeedImageError, fall back to whatever's already on disk
         (previous session's image, or nothing -> procedural texture).

    The browser shows the procedural fallback for the 3-5s gap between
    Start and seed_version bumping. That's expected and visually fine.
    """
    import asyncio  # local to avoid polluting the sync helpers above

    # Always wait for Start, even with --skip-seed -- we don't want to
    # surface "skipped" log lines before the user has even chosen a path.
    await state.start_requested.wait()

    if skip:
        existing = maybe_load_existing_seed()
        if existing is not None:
            print(
                f"[seed] skipped (--skip-seed); existing image at "
                f"{existing.name} will be used by the visualizer",
                flush=True,
            )
            # Bump version so the browser reloads the on-disk image
            # (the procedural fallback may be showing right now).
            state.seed_version += 1
        else:
            print(
                "[seed] skipped (--skip-seed) and no existing seed found; "
                "the visualizer will keep its procedural texture",
                flush=True,
            )
    else:
        prompt = state.prompt.strip()
        if not prompt:
            print(
                "[seed] no prompt available at start; skipping initial generation",
                flush=True,
            )
        else:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None,
                    generate_seed_image,
                    SeedOptions(prompt=prompt, use_cache=use_cache),
                )
                state.seed_version += 1
                # state.seed_prompt is the source of truth the evolver
                # drifts from; align it with what we actually rendered.
                state.seed_prompt = prompt
            except SeedImageError as e:
                print(f"[seed] FAIL: {e}", flush=True)
                existing = maybe_load_existing_seed()
                if existing is not None:
                    print(
                        f"[seed] continuing with existing {existing.name} "
                        "(may be from a previous prompt)",
                        flush=True,
                    )
                    # Still bump so the browser at least loads the stale file.
                    state.seed_version += 1
                else:
                    print(
                        "[seed] no existing seed to fall back to; "
                        "visualizer will keep its procedural texture",
                        flush=True,
                    )
            except asyncio.CancelledError:
                print("[seed] cancelled mid-generation", flush=True)
                raise

    # Park forever: this is a one-shot task, but the orchestrator's
    # FIRST_COMPLETED race interprets any clean task return as a fatal
    # "task exited" condition (because most tasks are supposed to live
    # for the whole session). Sleeping until cancelled keeps the rest
    # of the pipeline alive after we've done our one job. The cost is
    # ~0 -- a parked Event.wait() consumes no CPU and one waiter slot.
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise
