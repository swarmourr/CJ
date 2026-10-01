ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG CJ_COMMIT=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CJ_COMMIT=${CJ_COMMIT}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        iproute2 \
        iputils-ping \
        procps \
        stress-ng \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 cj && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash cj

WORKDIR /cj/work
COPY . /cj/work
RUN printf '%s\n' "${CJ_COMMIT}" > /cj/work/COMMIT
RUN python -m pip install --upgrade pip==25.2 \
    && python -m pip install --no-cache-dir -e '.[evaluation]'

RUN mkdir -p /cj/input /cj/output \
    && chown -R cj:cj /cj /home/cj

USER cj:cj
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
