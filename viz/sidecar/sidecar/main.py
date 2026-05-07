"""muse2-viz-sidecar entry point.

Wires together:
  * VizOscServer   (/viz/* -> VizState)
  * file watcher   (viz/prompts/live.txt -> VizState.base_prompt)
  * PromptBuilder  (VizState -> PromptPlan)
  * DiffusionBackend (PromptPlan -> frame)
  * SyphonPublisher (frame -> TD)
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .diffusion import RenderRequest, build_backend
from .osc_server import VizOscServer
from .prompt_builder import PromptBuilder
from .state import VizState


class PromptFileWatcher:
    """Poll a text file and push its contents as /viz/prompt/base."""

    def __init__(self, path: Path, state: VizState, poll_s: float = 1.0) -> None:
        self.path = path
        self.state = state
        self.poll_s = poll_s
        self._last_mtime: Optional[float] = None
        self._last_text: Optional[str] = None

    def tick(self, now: float) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return
        if self._last_mtime == st.st_mtime:
            return
        self._last_mtime = st.st_mtime
        try:
            text = self.path.read_text().strip()
        except OSError:
            return
        if text != self._last_text:
            self._last_text = text
            self.state.set_base_prompt(text)
            print(f"[prompt] live.txt updated: {text!r}", flush=True)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="muse2-viz-sidecar")
    p.add_argument("--listen-host", default="127.0.0.1")
    p.add_argument("--listen-port", type=int, default=9100)
    p.add_argument(
        "--prompts",
        default=str(Path(__file__).resolve().parents[2] / "prompts" / "default.yaml"),
        help="Path to prompt bank YAML file",
    )
    p.add_argument(
        "--live-prompt-file",
        default=str(Path(__file__).resolve().parents[2] / "prompts" / "live.txt"),
        help="File watched for /viz/prompt/base updates (leave empty to disable)",
    )
    p.add_argument("--syphon-name", default="Muse2Viz")
    p.add_argument("--width", type=int, default=512)
    p.add_argument("--height", type=int, default=512)
    p.add_argument("--steps", type=int, default=1, help="Diffusion steps per frame")
    p.add_argument(
        "--backend",
        choices=("diffusers", "fake"),
        default="diffusers",
        help="Diffusion backend. 'fake' = procedural RGBA noise for pipeline testing.",
    )
    p.add_argument(
        "--vae",
        choices=("tiny", "full"),
        default="tiny",
        help=(
            "VAE choice for the diffusers backend. 'tiny' = TAESD "
            "(madebyollin/taesdxl, ~5-10x faster decode on MPS, slight quality "
            "loss); 'full' = standard SDXL VAE."
        ),
    )
    p.add_argument(
        "--prompt-source",
        choices=("auto", "manual", "mix"),
        default="auto",
        help="Initial prompt-source mode. Overridden at runtime by /viz/prompt/source.",
    )
    p.add_argument(
        "--no-syphon",
        action="store_true",
        help="Skip publishing to Syphon (for headless smoke tests)",
    )
    p.add_argument(
        "--target-fps",
        type=float,
        default=0.0,
        help="Cap frame rate (0 = unlimited). SDXL-Turbo on MPS usually caps itself.",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    state = VizState(prompt_source=args.prompt_source)
    builder = PromptBuilder(args.prompts)

    banks = builder.available_banks()
    if banks:
        print(f"[prompt] loaded banks: {', '.join(banks)}", flush=True)
    else:
        print(f"[prompt] WARNING: no banks loaded from {args.prompts}", flush=True)

    osc = VizOscServer(args.listen_host, args.listen_port, state)
    osc.start()
    print(f"[osc] listening on {args.listen_host}:{args.listen_port}", flush=True)

    watcher: PromptFileWatcher | None = None
    if args.live_prompt_file:
        watcher = PromptFileWatcher(Path(args.live_prompt_file), state)
        print(f"[prompt] watching {args.live_prompt_file}", flush=True)

    backend = build_backend(args.backend, width=args.width, height=args.height, vae=args.vae)

    publisher = None
    if not args.no_syphon:
        try:
            from .syphon_out import SyphonPublisher

            publisher = SyphonPublisher(args.syphon_name, args.width, args.height)
            print(
                f"[syphon] publishing as {args.syphon_name!r} "
                f"({args.width}x{args.height})",
                flush=True,
            )
        except Exception as e:
            print(f"[syphon] failed to start ({e}); continuing without Syphon", file=sys.stderr)

    stop_flag = [False]

    def _handle_sig(_signum, _frame):
        stop_flag[0] = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    prev_frame: Optional[np.ndarray] = None
    frame_count = 0
    last_fps_log = time.monotonic()
    fps_frames = 0

    min_interval = 1.0 / args.target_fps if args.target_fps > 0 else 0.0

    try:
        while not stop_flag[0]:
            step_start = time.monotonic()

            if watcher is not None:
                watcher.tick(step_start)

            snap = state.snapshot()
            plan = builder.build(snap)

            req = RenderRequest(
                plan=plan,
                prev_frame=prev_frame,
                width=args.width,
                height=args.height,
                steps=args.steps,
            )
            try:
                frame = backend.render(req)
            except Exception as e:
                print(f"[render] error: {e}", file=sys.stderr)
                time.sleep(0.1)
                continue

            prev_frame = frame

            if publisher is not None:
                try:
                    publisher.publish(frame)
                except Exception as e:
                    print(f"[syphon] publish error: {e}", file=sys.stderr)

            frame_count += 1
            fps_frames += 1
            now = time.monotonic()
            if now - last_fps_log >= 2.0:
                fps = fps_frames / (now - last_fps_log)
                print(
                    f"[loop] fps={fps:.1f} source={snap.source} "
                    f"blend={snap.params.get('prompt_blend', 0):.2f} "
                    f"strength={plan.strength:.2f}",
                    flush=True,
                )
                last_fps_log = now
                fps_frames = 0

            if min_interval > 0:
                elapsed = time.monotonic() - step_start
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)
    finally:
        osc.stop()
        if publisher is not None:
            publisher.close()
        print(f"\n[exit] rendered {frame_count} frames. stopped cleanly.", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
