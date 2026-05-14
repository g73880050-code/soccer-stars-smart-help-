# =============================================================================
# Soccer Stars Analyzer — Build Image
# =============================================================================
# Base: ubuntu:22.04 (glibc 2.35 — compatible with NDK r25b)
#
# What is frozen inside this image
# ---------------------------------
#   Python          3.11
#   Java            17 (temurin via apt)
#   Android SDK     platform-tools, build-tools 33.0.0, platforms android-33
#   Android NDK     r25b  (25.1.8937393)
#   Buildozer       1.5.0
#   Cython          3.0.10
#   All SDK licenses pre-accepted
#
# The sdkmanager interceptor is also baked in (/usr/local/bin/sdkmanager).
# If python-for-android's bootstrap tries to call sdkmanager to fetch a
# newer build-tools version, the interceptor silently rewrites the request
# back to 33.0.0 and calls the real binary.
#
# Build locally
# -------------
#   docker build -t soccer-stars-builder .
#   docker run --rm -v "$PWD":/workspace -w /workspace soccer-stars-builder \
#     buildozer android debug
# =============================================================================

FROM ubuntu:22.04

# ── Non-interactive apt ──────────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# ── Android SDK/NDK paths (frozen) ──────────────────────────────────────────
ENV ANDROID_HOME=/opt/android/sdk
ENV ANDROID_SDK_ROOT=/opt/android/sdk
ENV ANDROIDSDK=/opt/android/sdk
ENV ANDROIDNDK=/opt/android/sdk/ndk/25.1.8937393
ENV ANDROID_NDK_HOME=/opt/android/sdk/ndk/25.1.8937393
ENV ANDROIDAPI=33
ENV ANDROIDMINAPI=26
ENV ANDROIDNDKAPI=26

# Gradle: disable daemon + block its own SDK downloader + cap heap
ENV GRADLE_OPTS="-Dorg.gradle.daemon=false -Dandroid.builder.sdkDownload=false -Dorg.gradle.jvmargs=-Xmx3g"

# PATH — build-tools 33 is prepended so every aidl/d8/apksigner lookup wins
ENV PATH="/usr/local/bin:/opt/android/sdk/build-tools/33.0.0:/opt/android/sdk/platform-tools:/opt/android/sdk/cmdline-tools/latest/bin:${PATH}"

# ── 1. System packages ───────────────────────────────────────────────────────
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        software-properties-common \
        wget curl ca-certificates \
        git zip unzip \
        openjdk-17-jdk \
        python3.11 python3.11-dev python3-pip \
        autoconf automake libtool \
        pkg-config \
        zlib1g-dev \
        libncurses6 libncursesw6 \
        libssl-dev libffi-dev \
        libsqlite3-dev \
        build-essential ccache cmake ninja-build gettext && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ── 2. Pin python3 / pip to 3.11 ────────────────────────────────────────────
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 && \
    update-alternatives --install /usr/bin/python  python  /usr/bin/python3.11 1 && \
    python3 -m pip install --upgrade pip wheel setuptools

# ── 3. Buildozer 1.5.0 + Cython ─────────────────────────────────────────────
RUN pip install "cython==3.0.10" "buildozer==1.5.0"

# ── 4. Android command-line tools ───────────────────────────────────────────
RUN mkdir -p "$ANDROID_HOME/cmdline-tools" && \
    wget -q \
      "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip" \
      -O /tmp/ct.zip && \
    unzip -q /tmp/ct.zip -d /tmp/ct_unpack && \
    mv /tmp/ct_unpack/cmdline-tools "$ANDROID_HOME/cmdline-tools/latest" && \
    rm -f /tmp/ct.zip

# ── 5. Accept all SDK licenses ──────────────────────────────────────────────
RUN yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses 2>/dev/null || true && \
    yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses 2>/dev/null || true

# ── 6. Install SDK components — ONLY what we need ───────────────────────────
RUN "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" \
      "platform-tools" \
      "build-tools;33.0.0" \
      "platforms;android-33" \
      "ndk;25.1.8937393"

# ── 7. Verify aidl and ndk-build are present ────────────────────────────────
RUN test -f "$ANDROID_HOME/build-tools/33.0.0/aidl" || \
      { echo "ERROR: aidl not found!"; exit 1; } && \
    echo "aidl OK: $ANDROID_HOME/build-tools/33.0.0/aidl" && \
    test -f "$ANDROIDNDK/ndk-build" || \
      { echo "ERROR: ndk-build not found!"; exit 1; } && \
    echo "ndk-build OK: $ANDROIDNDK/ndk-build"

# ── 8. sdkmanager interceptor (belt + suspenders) ───────────────────────────
# If p4a's bootstrap tries to request build-tools;37 or ;36 during the build,
# the interceptor silently rewrites it to 33.0.0 and calls the real binary.
# The real binary is /opt/android/sdk/cmdline-tools/latest/bin/sdkmanager.
RUN REAL="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" && \
    cat > /usr/local/bin/sdkmanager << WRAPPER
#!/usr/bin/env bash
# sdkmanager interceptor — rewrites 36/37 → 33.0.0, strips --update
REAL_BIN="$REAL"
ARGS=()
for arg in "\$@"; do
  case "\$arg" in
    build-tools\;37*) ARGS+=("build-tools;33.0.0"); echo "[interceptor] rewrote \$arg → build-tools;33.0.0" ;;
    build-tools\;36*) ARGS+=("build-tools;33.0.0"); echo "[interceptor] rewrote \$arg → build-tools;33.0.0" ;;
    --update)         echo "[interceptor] dropped --update" ;;
    *)                ARGS+=("\$arg") ;;
  esac
done
exec "\$REAL_BIN" "\${ARGS[@]}"
WRAPPER
RUN chmod +x /usr/local/bin/sdkmanager

# ── 9. Symlink build-tools/37.0.0 → 33.0.0 ─────────────────────────────────
# Final safety net for any Gradle script that constructs the path as a literal.
RUN ln -sf "$ANDROID_HOME/build-tools/33.0.0" \
           "$ANDROID_HOME/build-tools/37.0.0"

# ── 10. Final check ──────────────────────────────────────────────────────────
RUN echo "=== Build image verification ===" && \
    java -version && \
    python --version && \
    buildozer --version && \
    which sdkmanager && sdkmanager --version && \
    which aidl && aidl --version && \
    echo "NDK: $ANDROIDNDK" && ls "$ANDROIDNDK/ndk-build" && \
    echo "=== Image ready ==="

# ── Default working directory ────────────────────────────────────────────────
WORKDIR /workspace
