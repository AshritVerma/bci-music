"""Auto-rewrite Lyria-filtered prompts via Claude Opus 4.7.

Google's Lyria RealTime model returns a `filtered_prompt` server message
(and zero audio) whenever a prompt names a specific artist, band, song,
or uses "in the style of [name]" phrasing. This module owns the recovery
path: take the original prompt + filter reason, ask Claude Opus to
TRANSLATE the named-artist shorthand into the concrete sonic descriptors
those artists are known for, and hand the rewrite back to the caller so
it can push it into the same Lyria session via `set_weighted_prompts`.

Translation, not censorship. Stripping "Daft Punk" leaves a hole; the
guard fills that hole with the actual sonic fingerprint (filtered
French-touch, sidechain pumping, vocoder leads, chopped disco loops),
which is the form Lyria actually responds to.

The guard is stateless across calls, owns its own AsyncAnthropic client,
and is fully automatic -- no user confirmation step. The caller controls
retry budget (default behavior in the smoke script is one rewrite per
session).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import anthropic


REWRITE_MODEL = "claude-opus-4-7"
DEFAULT_MAX_TOKENS = 400
DEFAULT_LOG_PREFIX = "[prompt-guard]"


# Why this system prompt is shaped this way:
#   - Hard constraint set up front so Opus knows the output grammar before
#     it reads anything else.
#   - Calibration examples in the middle anchor density (12-30 tokens,
#     comma-separated) and style (concrete sonic terms, no fluff).
#   - The "translate, not censor" framing is the single most important
#     line: it's what produces specific sonic fingerprints instead of
#     anodyne fallbacks like "house music with synths".
_SYSTEM_PROMPT = """\
You rewrite music prompts for Google's Lyria RealTime model. Lyria
rejects any prompt that names a specific artist, band, producer, DJ,
song, or album, or that says "in the style of [name]" / "inspired by
[name]".

Your job is to TRANSLATE, not censor. A flagged prompt usually leans on
artist names as shorthand for a sonic fingerprint. Replace each artist
reference with the concrete musical traits that artist is known for --
genre tags, tempo range, instrumentation, production techniques, vocal
treatment, era, mood, signature textures.

Hard rules:
- Output ONE line. No prefix, no quotes, no commentary, no markdown.
- Output ONLY a Lyria-shaped prompt: comma-separated descriptors.
- Never name any artist, band, producer, DJ, song, or album, even
  obliquely (no "French house pioneers", no "the duo with the helmets",
  no "that Scottish duo"). Pure sonic language only.
- Preserve every concrete sonic instruction the user gave (BPM, mood,
  instruments, energy, era, structure).
- Preserve the user's overall vibe and energy. Do not shrink an
  energetic prompt into a generic one.
- If the user said "live DJ set", keep performance-feel cues (mixing,
  transitions, crowd energy, build-ups).
- Aim for 12-30 descriptive tokens. Be specific, not flowery.

Translation calibration (do not copy verbatim; use as style anchors):
- "Daft Punk" -> filtered French-touch house, sidechain-pumped analog
  pads, vocoder leads, chopped disco loops, four-on-the-floor 120 BPM,
  robotic vocal stabs.
- "Fred again.." -> intimate UK garage-leaning two-step shuffle,
  pitched vocal chops, lo-fi piano, warm sub-bass, sampled
  field-recording textures, emotional club energy.
- "Bicep" -> melodic breakbeat, rolling 130 BPM, hypnotic arps,
  rave stabs, washed reverb pads.

You will be given the original prompt and Lyria's filter reason.
Return only the rewritten prompt -- nothing else."""


def _build_user_message(original: str, reason: str | None) -> str:
    return (
        "ORIGINAL PROMPT:\n"
        f"{original}\n"
        "\n"
        "LYRIA FILTER REASON:\n"
        f"{reason or '(none provided)'}\n"
        "\n"
        "Rewrite now."
    )


def _extract_text(response: anthropic.types.Message) -> str:
    """Pull the single text block out of a Messages API response."""
    for block in response.content:
        # `text` blocks are the only thing we expect; tool-use blocks
        # would mean the model misbehaved against our instructions.
        if block.type == "text":
            return block.text.strip()
    raise RuntimeError(
        f"Anthropic response had no text block (stop_reason={response.stop_reason!r})"
    )


@dataclass(frozen=True)
class RewriteResult:
    """The output of a single `PromptGuard.rewrite` call."""

    original: str
    rewritten: str
    reason: str
    model: str
    latency_s: float


class PromptGuard:
    """Async wrapper around Claude Opus 4.7 for Lyria prompt rewrites.

    The caller (smoke script today, Phase 5 lyria_client.py tomorrow) is
    responsible for detecting Lyria's `filtered_prompt` message, calling
    `await guard.rewrite(...)`, and pushing the rewrite back into the
    Lyria session via `set_weighted_prompts`. This class owns only the
    rewrite step and the structured logging around it.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = REWRITE_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        log_prefix: str = DEFAULT_LOG_PREFIX,
    ) -> None:
        if not api_key:
            raise ValueError("PromptGuard requires a non-empty Anthropic API key")
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._log_prefix = log_prefix

    async def rewrite(
        self,
        original: str,
        *,
        reason: str | None,
    ) -> RewriteResult:
        """Translate a filtered prompt into pure sonic descriptors.

        Raises any underlying `anthropic` error (auth, quota, network).
        The caller's existing cancel / cleanup path handles the failure.
        """
        clean_reason = (reason or "").strip()

        print(f"{self._log_prefix} Lyria FLAGGED prompt", flush=True)
        print(f"{self._log_prefix}   original: {original!r}")
        if clean_reason:
            print(f'{self._log_prefix}   reason:   "{clean_reason}"')
        print(f"{self._log_prefix} rewriting via {self._model} ...", flush=True)

        t0 = time.monotonic()
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _build_user_message(original, clean_reason or None),
                }
            ],
        )
        latency = time.monotonic() - t0

        rewritten = _extract_text(response)

        # Defense in depth: if Opus disobeyed and emitted quote wrappers
        # or a leading "Rewrite:" header, strip them so Lyria gets a
        # clean comma-separated descriptor string.
        rewritten = _scrub_wrappers(rewritten)

        print(f"{self._log_prefix}   rewritten: {rewritten!r}")
        print(
            f"{self._log_prefix}   ({latency:.2f}s, "
            f"stop_reason={response.stop_reason})",
            flush=True,
        )

        if not rewritten:
            raise RuntimeError("PromptGuard got an empty rewrite from Opus")

        return RewriteResult(
            original=original,
            rewritten=rewritten,
            reason=clean_reason,
            model=self._model,
            latency_s=latency,
        )


def _scrub_wrappers(text: str) -> str:
    """Strip the few wrappers Opus sometimes adds despite the system prompt."""
    text = text.strip()

    # Strip a leading header like "Rewritten:" or "Prompt:" if present.
    for header in ("Rewritten prompt:", "Rewritten:", "Prompt:", "Rewrite:"):
        if text.lower().startswith(header.lower()):
            text = text[len(header):].strip()
            break

    # Strip matched outer quote characters (straight or curly).
    if len(text) >= 2 and text[0] in "\"'\u201c\u2018" and text[-1] in "\"'\u201d\u2019":
        text = text[1:-1].strip()

    # Collapse to a single line: Lyria prompts are one comma-separated
    # descriptor string, not multi-paragraph prose.
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())

    return text
