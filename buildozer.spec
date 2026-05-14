# ============================================================================
# buildozer.spec — Soccer Stars Analyzer
# ============================================================================
# Key design decisions
# --------------------
# 1. android.sdk_path and android.ndk_path are set to the GitHub Actions
#    runner paths.  Buildozer reads these first; if they point to an existing
#    installation it never calls sdkmanager to re-download anything.
# 2. android.build_tools_version = 33.0.0 — explicit pin.
# 3. android.accept_sdk_license = True — Buildozer will answer "y" to any
#    license prompt it generates itself (belt + suspenders alongside the
#    sdkmanager interceptor in main.yml).
# ============================================================================

[app]

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
title          = Soccer Stars Analyzer
package.name   = soccerstarsanalyzer
package.domain = com.soccerstars

# ---------------------------------------------------------------------------
# Source layout
# ---------------------------------------------------------------------------
source.dir          = .
source.include_exts = py,png,jpg,jpeg,kv,atlas,json,ttf
source.main         = main.py

# Background foreground service (MediaProjection requires foreground type)
services = SoccerStarsService:service/main.py:foreground

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
version = 1.2.0

# ---------------------------------------------------------------------------
# Requirements
# python-for-android recipe names (NOT pip package names)
# ---------------------------------------------------------------------------
requirements = python3==3.11.9,kivy==2.3.0,numpy,opencv,pyjnius,android

# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------
orientation = portrait
fullscreen   = 0

# ---------------------------------------------------------------------------
# Android SDK — THE LOCK SECTION
# ---------------------------------------------------------------------------
android.api   = 33
android.minapi = 26
android.ndk_api = 26

# Explicit build-tools pin — p4a reads this before calling sdkmanager
android.build_tools_version = 33.0.0

# Tell Buildozer the SDK and NDK already exist at these paths.
# When these are set and the directories exist, Buildozer skips its own
# sdkmanager download entirely.
# On local builds: leave blank or override via env (ANDROIDSDK / ANDROIDNDK).
# On CI: the workflow sets ANDROID_HOME which overrides these.
android.sdk_path = /usr/local/lib/android/sdk
android.ndk_path = /usr/local/lib/android/sdk/ndk/25.1.8937393

# Automatically accept any license prompts Buildozer itself generates
android.accept_sdk_license = True

# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------
android.archs = arm64-v8a

# ---------------------------------------------------------------------------
# AndroidX + Gradle
# ---------------------------------------------------------------------------
android.enable_androidx      = True
android.gradle_dependencies  = androidx.core:core:1.13.1

# Prevent the Android Gradle Plugin from calling its own SDK downloader.
# This is the Gradle-level complement to -Dandroid.builder.sdkDownload=false
# in GRADLE_OPTS.
android.gradle_repositories  = google(), mavenCentral()

# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
android.permissions =
    android.permission.SYSTEM_ALERT_WINDOW,
    android.permission.FOREGROUND_SERVICE,
    android.permission.FOREGROUND_SERVICE_MEDIA_PROJECTION,
    android.permission.WAKE_LOCK,
    android.permission.INTERNET,
    android.permission.ACCESS_NETWORK_STATE,
    android.permission.VIBRATE

# ---------------------------------------------------------------------------
# Manifest extras
# ---------------------------------------------------------------------------
android.extra_manifest_application_arguments =
    android:usesCleartextTraffic="true"

# ---------------------------------------------------------------------------
# python-for-android
# ---------------------------------------------------------------------------
p4a.branch = develop

# ---------------------------------------------------------------------------
# Buildozer meta
# ---------------------------------------------------------------------------
[buildozer]
build_dir = .buildozer
bin_dir   = ./bin
log_level = 2
