#!/usr/bin/env bash
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
# forgefixture: build, flash and watch the bench actuator's firmware.
#
#   ./fixture.sh env              fixture.env from the example, with a fresh API key
#   ./fixture.sh build            build (native idf.py if installed, else the IDF container)
#   ./fixture.sh flash [PORT]     write the build to the board over USB (esptool)
#   ./fixture.sh monitor [PORT]   the board's log (idf.py monitor, or pyserial's miniterm)
#   ./fixture.sh test             the host test of the policy (needs a C compiler)
#
# The container build needs docker or podman and pulls espressif/idf once
# (IDF_IMAGE below); flashing never runs in the container: esptool is a
# pip package (pip install esptool) and talks to the port directly.
set -euo pipefail
cd "$(dirname "$0")"

IDF_IMAGE="${IDF_IMAGE:-docker.io/espressif/idf:v5.5.5}"
CHIP=esp32s3
BAUD="${BAUD:-460800}"

have() { command -v "$1" >/dev/null 2>&1; }

runner() {
    if have docker; then echo docker; elif have podman; then echo podman; else
        echo "neither idf.py nor docker/podman found: install ESP-IDF, or a container runtime for $IDF_IMAGE" >&2
        exit 1
    fi
}

# A Windows path for the bind mount when run from Git Bash, which would
# otherwise rewrite /project into a Windows path.
host_path() {
    if have cygpath; then cygpath -w "$PWD"; else echo "$PWD"; fi
}

cmd_env() {
    if [ -e fixture.env ]; then
        echo "fixture.env exists; edit it, or remove it first" >&2
        exit 1
    fi
    key=$( (have openssl && openssl rand -hex 16) || python3 -c 'import secrets; print(secrets.token_hex(16))')
    sed -e "s/^API_KEY=.*/API_KEY=$key/" fixture.env.example > fixture.env
    chmod 600 fixture.env 2>/dev/null || true
    echo "fixture.env written with a fresh API key; fill in WIFI_SSID and WIFI_PSK"
}

cmd_build() {
    [ -e fixture.env ] || { echo "no fixture.env: run ./fixture.sh env first" >&2; exit 1; }
    # The file holds the WiFi PSK and the API key: nobody but the owner
    # reads it (an MSYS shell cannot express the mode, so it is not judged).
    if [ "$(uname -o 2>/dev/null)" != "Msys" ] && [ -n "$(find fixture.env -perm /077 2>/dev/null)" ]; then
        echo "fixture.env is group- or world-readable: chmod 600 fixture.env" >&2
        exit 1
    fi
    if have idf.py; then
        idf.py set-target "$CHIP" >/dev/null
        idf.py build
    else
        r=$(runner)
        MSYS_NO_PATHCONV=1 "$r" run --rm -v "$(host_path):/project" -w /project -e HOME=/tmp "$IDF_IMAGE" \
            bash -c "idf.py set-target $CHIP >/dev/null && idf.py build"
    fi
}

port_arg() {
    if [ -n "${1:-}" ]; then echo "$1"; elif [ -n "${PORT:-}" ]; then echo "$PORT"; else
        echo "which port? ./fixture.sh flash PORT (COM5, /dev/ttyUSB0, /dev/cu.usbserial-*)" >&2
        exit 1
    fi
}

cmd_flash() {
    port=$(port_arg "${1:-}")
    [ -e build/flash_args ] || { echo "no build yet: ./fixture.sh build" >&2; exit 1; }
    if have idf.py; then
        idf.py -p "$port" flash
    else
        have esptool.py || have esptool || { echo "esptool not found: pip install esptool" >&2; exit 1; }
        tool=$(have esptool.py && echo esptool.py || echo esptool)
        (cd build && "$tool" --chip "$CHIP" -p "$port" -b "$BAUD" --before default_reset --after hard_reset \
            write_flash @flash_args)
    fi
}

cmd_monitor() {
    port=$(port_arg "${1:-}")
    if have idf.py; then
        idf.py -p "$port" monitor
    else
        python3 -m serial.tools.miniterm --raw "$port" 115200
    fi
}

cmd_test() {
    sh test/run.sh
}

case "${1:-}" in
    env) cmd_env ;;
    build) cmd_build ;;
    flash) cmd_flash "${2:-}" ;;
    monitor) cmd_monitor "${2:-}" ;;
    test) cmd_test ;;
    *) sed -n '2,12p' "$0"; exit 1 ;;
esac
