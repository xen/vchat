FROM python:3.11-bookworm

WORKDIR /workspace

# Ensure pip is available so `uv pip compile` can bootstrap environments naturally.
RUN python3 -m ensurepip --upgrade
RUN python3 -m pip install --upgrade pip pip-tools

# Create non-root user for container execution (security best practice)
RUN useradd -m -s /bin/bash -u 1000 compiler && \
    chown -R compiler:compiler /workspace

# Expose the workspace, so volume mounts (especially requirements/) can map cleanly.
VOLUME /workspace

# Health check to verify Python environment is working
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import pip; print('ok')" || exit 1

# Run as non-root user
USER compiler
