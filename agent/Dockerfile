FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HERMES_GATEWAY_SRC_ROOT=/app/third_party/hermes \
    HERMES_GATEWAY_PYTHON=/usr/local/bin/python

# Tools the Hermes agent reaches for inside the sandbox. No SSH access
# exists to this container, so nothing here is for humans — every
# package is justified by an agent workflow:
#   curl + ca-certificates : HTTP(S) fetching (Wikipedia, APIs, raw files)
#   git                    : clone/diff/commit; agent does real source work
#   unzip                  : extracting downloaded archives
#   jq                     : parsing JSON output from curl/CLI tools
#   file                   : identifying file types before processing
#   procps                 : ps/kill for inspecting background processes
#                             started via terminal(background=True)
#   tree                   : readable directory listings for reasoning
#   openssh-client         : ssh/scp/sftp for tasks targeting remote hosts
#   build-essential        : gcc/g++/make for pip installs that need to
#                             compile C extensions (pyarrow, lxml, etc.)
#   python3-dev            : Python headers paired with build-essential
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        git \
        unzip \
        jq \
        file \
        procps \
        tree \
        openssh-client \
        build-essential \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY agent/requirements.txt /app/agent/requirements.txt
RUN pip install --no-cache-dir -r /app/agent/requirements.txt

COPY third_party/hermes /app/third_party/hermes
RUN pip install --no-cache-dir /app/third_party/hermes

COPY agent /app/agent

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser
WORKDIR /app/agent

EXPOSE 8088

CMD ["python", "main.py"]
