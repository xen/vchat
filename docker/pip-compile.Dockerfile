FROM python:3.11-bookworm

WORKDIR /workspace

# Ensure pip is available so `uv pip compile` can bootstrap environments naturally.
RUN python3 -m ensurepip --upgrade
RUN python3 -m pip install --upgrade pip pip-tools

# Expose the workspace, so volume mounts (especially requirements/) can map cleanly.
VOLUME /workspace
