FROM python:3.11 AS builder

WORKDIR /app

# Pin pip below 25.3 for the build toolchain.
#
# pip 25.3 removed the internal `stdlib_pkgs` symbol from
# pip._internal.utils.compat, which pip-tools still imports (piptools/sync.py).
# With an unpinned upgrade the builder picks up current pip (26.x) and the very
# next step dies with:
#   ImportError: cannot import name 'stdlib_pkgs' from 'pip._internal.utils.compat'
# Upgrading pip-tools does NOT help -- the latest release (7.6.0) still imports it.
# This only constrains the build-time resolver; no runtime dependency changes.
RUN pip install --upgrade "pip<25.3" setuptools pip-tools build

COPY pyproject.toml /app/

# replace grpcio==1.51.1 with grpcio
#
# This is a workaround to reduce the container build time for aarch64.
# Installation of grpcio==1.51.1 package for aarch64 takes a long time
# due to full compilation of the package
#
# We need to pin to grpcio==1.51.1 because that is the version used in the SONiC.
# Once the gRPC version is updated in SONiC, we can remove this workaround.
RUN sed -i 's/grpcio==1.51.1/grpcio/g' pyproject.toml
RUN sed -i 's/grpcio-tools==1.51.1/grpcio-tools/g' pyproject.toml

RUN mkdir src && touch src/__init__.py

RUN pip-compile --verbose --strip-extras pyproject.toml

RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# same as above
RUN sed -i 's/grpcio==1.51.1/grpcio/g' pyproject.toml
RUN sed -i 's/grpcio-tools==1.51.1/grpcio-tools/g' pyproject.toml

RUN pip install -e '.[dev]'
# regenerate grpc stubs since the gRPC version is updated
RUN make generate-grpc

RUN python -m build

CMD ["xcvr-emud", "-c", "src/xcvr_emu/config.yaml"]

FROM python:3.11-slim AS runtime

RUN --mount=type=bind,from=builder,source=/app,target=/app \
  pip install --no-cache-dir /app/dist/*.whl

RUN --mount=type=bind,from=builder,source=/app,target=/app \
  cp /app/src/xcvr_emu/config.yaml .

CMD ["xcvr-emud", "-c", "config.yaml"]
