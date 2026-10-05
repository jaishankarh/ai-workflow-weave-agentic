# Sandbox image for the Environment spike: the stock OpenHands agent-server plus
# what an Environment for a React Native Product needs inside the sandbox:
#   - Docker Engine + compose plugin (the *inner* daemon; under sysbox it runs
#     without the host socket and without --privileged)
#   - JDK 17, Node, Android SDK, emulator and one x86_64 system image
#
# Build (see README):
#   docker build --build-arg RUNTIME_USER=<agent-server user> -t weave-spike-environment .
ARG BASE=ghcr.io/openhands/agent-server:latest-python
FROM ${BASE}

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# --- base tools + the shared libraries the headless emulator still links against
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl ca-certificates unzip git procps iproute2 sudo xz-utils file \
      libpulse0 libnss3 libgl1 libxkbfile1 libxcursor1 libxdamage1 libxcomposite1 \
      libxi6 libxtst6 libxrandr2 libdbus-1-3 \
 && (apt-get install -y --no-install-recommends libasound2t64 || apt-get install -y --no-install-recommends libasound2) \
 && rm -rf /var/lib/apt/lists/*

# --- Docker Engine (dockerd, containerd, CLI, compose plugin) for nested Docker
RUN curl -fsSL https://get.docker.com | sh \
 && docker compose version

# --- JDK 17 (Temurin tarball: works whatever Debian release the base image is on)
RUN mkdir -p /opt/jdk17 \
 && curl -fsSL "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse" \
    | tar -xz --strip-components=1 -C /opt/jdk17
ENV JAVA_HOME=/opt/jdk17

# --- Node 22 if the base image has none, or one older than 20
RUN if ! command -v node >/dev/null || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 20 ]; then \
      curl -fsSL https://nodejs.org/dist/v22.20.0/node-v22.20.0-linux-x64.tar.xz | tar -xJ --strip-components=1 -C /usr/local; \
    fi \
 && node --version && npm --version

# --- Android SDK, emulator, one system image. World-writable so Gradle (running
# as the agent-server user) can add the NDK/CMake versions React Native asks for.
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV PATH=/opt/jdk17/bin:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:${PATH}
ARG SYSTEM_IMAGE="system-images;android-34;google_apis;x86_64"
RUN mkdir -p ${ANDROID_HOME}/cmdline-tools \
 && curl -fsSL -o /tmp/clt.zip https://dl.google.com/android/repository/commandlinetools-linux-13114758_latest.zip \
 && unzip -q /tmp/clt.zip -d /tmp/clt && mv /tmp/clt/cmdline-tools ${ANDROID_HOME}/cmdline-tools/latest && rm -rf /tmp/clt /tmp/clt.zip \
 && yes | sdkmanager --licenses >/dev/null \
 && sdkmanager --install "platform-tools" "emulator" "platforms;android-35" "build-tools;35.0.0" "${SYSTEM_IMAGE}" >/dev/null \
 && chmod -R a+rwX ${ANDROID_HOME}
ENV SPIKE_SYSTEM_IMAGE=${SYSTEM_IMAGE}

# Scripts the spike runs inside the sandbox
COPY in-sandbox/ /opt/spike/
RUN chmod +x /opt/spike/*.sh

# The inner dockerd needs root. If the agent-server runs as a normal user, let it
# start dockerd (and only that kind of thing) through passwordless sudo. Inside
# sysbox, "root" is an unprivileged user on the host.
ARG RUNTIME_USER=root
RUN if [ "${RUNTIME_USER}" != "root" ]; then \
      echo "${RUNTIME_USER} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/spike && chmod 440 /etc/sudoers.d/spike \
      && usermod -aG docker "${RUNTIME_USER}" || true; \
    fi
USER ${RUNTIME_USER}
