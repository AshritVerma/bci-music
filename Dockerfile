# brAIn-music container image for Railway / any PaaS.
#
# Single-stage build on python:3.11-slim. We avoid the multi-stage
# pattern because the wheels we install (brainflow, sounddevice) bundle
# their native bits already; there's no compile step to throw away.
#
# Why we still install brainflow + libportaudio2 in a CLOUD image that
# never reads a real headset and never plays to a local speaker:
#
#   * `from muse2_music_lab.eeg.brainflow_loop import run_eeg_supervisor`
#     resolves at orchestrator startup. The supervisor only ever spawns
#     the simulated path in --cloud, but the IMPORT itself happens
#     unconditionally. A missing brainflow at import-time crashes boot.
#
#   * `from muse2_music_lab.lyria import run_audio_playback_loop` is
#     imported in main.py for the same reason -- main never CALLS it
#     in --cloud, but the import line `import sounddevice as sd` runs
#     at module load. sounddevice's import shells out to PortAudio via
#     CFFI, which needs libportaudio2 on the system or it raises at
#     import-time. Cheap to install (~200KB), cheaper than refactoring
#     all the imports to be lazy.
#
# Image size: ~700 MB. Most of it is the brainflow native blob.
# Railway's free tier is fine with this.

FROM python:3.11-slim AS runtime

# System packages:
#   libportaudio2  - sounddevice imports it at module load (see above)
#   libusb-1.0-0   - brainflow's BLE backend depends on it on Linux
#   libgomp1       - brainflow links against OpenMP for FFT
#   curl           - $RAILWAY_HEALTHCHECK fallback / debugging
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libportaudio2 \
        libusb-1.0-0 \
        libgomp1 \
        curl \
 && rm -rf /var/lib/apt/lists/*

# Run as non-root. Railway doesn't require this, but it's good hygiene
# (a leaked credential here can't escalate to system writes), and we
# don't need root for anything the orchestrator does.
RUN useradd --create-home --shell /bin/bash --uid 1000 brain
WORKDIR /app

# Install the package + deps as root, then chown so the runtime user
# can read everything. Splitting copy to take advantage of layer cache:
# pyproject changes <<< source changes, so when only Python files change
# we skip the slow `pip install` step.
COPY pyproject.toml README.md ./
COPY src ./src

# pip install . (editable would also work but isn't useful in a baked
# container image; non-editable is slightly smaller). --no-cache-dir
# keeps the image lean by skipping pip's wheel download cache.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

# Static assets last so a CSS/JS tweak invalidates only the very last
# layer. The frontend lives entirely under static/ -- no transpile,
# no bundler, just files the aiohttp server hands out as-is.
COPY static ./static

# Hand ownership over so the non-root user can read everything (and
# write to /app/static/cache for the seed-image cache, even though we
# don't use that in cloud mode). Anything that needs to be written at
# runtime goes inside /app.
RUN chown -R brain:brain /app
USER brain

# Railway / Fly / Render set $PORT dynamically and refuse to route to
# any other port. cli.py reads $PORT in _default_http_port(). EXPOSE
# is documentary only -- the actual port is whatever Railway picks at
# deploy time. We pick 8000 as the local-default for `docker run`
# without -e PORT=...
EXPOSE 8000

# Healthcheck: cli.py serves /health unconditionally when the server
# is up. Railway honors HEALTHCHECK from the image as a fallback when
# `healthcheckPath` in railway.json is unset, but we set it in BOTH
# places so a misconfigured deploy still gets restarted.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8000}/health" || exit 1

# --cloud forces simulated EEG + WS audio broadcast + no browser auto-
# launch + no TUI + binds 0.0.0.0. The visitor types a prompt in the
# browser and clicks Start; one Lyria session is shared across every
# connected client.
ENTRYPOINT ["muse2"]
CMD ["perform", "--cloud"]
