# Build environment for the MQTT Pipe Android app (Chaquopy).
# Usage (from repo root):
#   docker build -t mqtt-android-build - < android/Dockerfile
#   docker run --rm -v "$PWD":/work -v gradle-cache:/gradle-cache \
#     mqtt-android-build gradle :app:assembleDebug
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      openjdk-17-jdk-headless wget unzip ca-certificates \
      python3 python3-pip python3-venv python3-dev \
 && rm -rf /var/lib/apt/lists/*

ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV ANDROID_HOME=/opt/android-sdk
RUN mkdir -p $ANDROID_SDK_ROOT/cmdline-tools \
 && wget -q https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -O /tmp/clt.zip \
 && unzip -q /tmp/clt.zip -d $ANDROID_SDK_ROOT/cmdline-tools \
 && mv $ANDROID_SDK_ROOT/cmdline-tools/cmdline-tools $ANDROID_SDK_ROOT/cmdline-tools/latest \
 && rm /tmp/clt.zip
ENV PATH=$PATH:$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$ANDROID_SDK_ROOT/platform-tools

RUN yes | sdkmanager --licenses > /dev/null \
 && sdkmanager "platform-tools" "platforms;android-34" "build-tools;34.0.0" > /dev/null

RUN wget -q https://services.gradle.org/distributions/gradle-8.9-bin.zip -O /tmp/g.zip \
 && unzip -q /tmp/g.zip -d /opt && rm /tmp/g.zip
ENV PATH=$PATH:/opt/gradle-8.9/bin
ENV GRADLE_USER_HOME=/gradle-cache
WORKDIR /work/android
