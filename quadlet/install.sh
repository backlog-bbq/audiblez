#!/usr/bin/env bash
# Install the audiblez Quadlet units into the right systemd directory.
#
# Default: rootless install (~/.config/containers/systemd/) with the CPU unit.
#
# Usage:
#   quadlet/install.sh                  # rootless, CPU
#   quadlet/install.sh --cuda           # rootless, CUDA
#   quadlet/install.sh --system         # rootful (sudo), CPU
#   quadlet/install.sh --system --cuda  # rootful (sudo), CUDA
#   quadlet/install.sh --start          # also start the service after install
#   quadlet/install.sh --uninstall      # remove the units (matches other flags)
#   quadlet/install.sh --help
#
# Quadlet requires podman >= 4.4 and a systemd-managed host.

set -euo pipefail

variant="cpu"
scope="user"
do_start=0
do_uninstall=0

for arg in "$@"; do
    case "$arg" in
        --cuda) variant="cuda" ;;
        --cpu) variant="cpu" ;;
        --system|--rootful) scope="system" ;;
        --user|--rootless) scope="user" ;;
        --start) do_start=1 ;;
        --uninstall) do_uninstall=1 ;;
        -h|--help)
            sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Run with --help for usage." >&2
            exit 2
            ;;
    esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$scope" == "user" ]]; then
    target_dir="${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd"
    sudo=""
    systemctl_args=(--user)
else
    target_dir="/etc/containers/systemd"
    sudo="sudo"
    systemctl_args=()
fi

container_unit="audiblez-${variant}.container"
service_name="audiblez-${variant}.service"
support_units=(audiblez.volume audiblez-outputs.volume audiblez.network)

# Sanity check: the unit files we want to install actually exist.
for f in "$container_unit" "${support_units[@]}"; do
    if [[ ! -f "$script_dir/$f" ]]; then
        echo "Missing source unit: $script_dir/$f" >&2
        exit 1
    fi
done

if (( do_uninstall )); then
    echo "Removing audiblez Quadlet units from $target_dir"
    for f in "$container_unit" "${support_units[@]}"; do
        path="$target_dir/$f"
        if [[ -e "$path" ]]; then
            $sudo rm -v "$path"
        fi
    done
    echo "Reloading systemd…"
    $sudo systemctl "${systemctl_args[@]}" daemon-reload
    echo "Done. The container itself is not removed; use 'podman rm -f audiblez-${variant}'"
    echo "and 'podman volume rm audiblez-outputs audiblez' if you want to wipe data."
    exit 0
fi

# Warn if the prebuilt image isn't available — install still succeeds, but
# `systemctl start` will fail until you build it.
if command -v podman >/dev/null 2>&1; then
    if ! podman image exists "audiblez:${variant}" 2>/dev/null; then
        echo "Warning: image 'audiblez:${variant}' not found locally."
        echo "  Build it first with:"
        if [[ "$variant" == "cuda" ]]; then
            echo "    podman build -t audiblez:cuda \\"
            echo "      --build-arg BASE_IMAGE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 \\"
            echo "      --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124 ."
        else
            echo "    podman build -t audiblez:cpu ."
        fi
        echo
    fi
fi

echo "Installing audiblez Quadlet units → $target_dir"
$sudo mkdir -p "$target_dir"
for f in "$container_unit" "${support_units[@]}"; do
    $sudo install -m 0644 "$script_dir/$f" "$target_dir/$f"
    echo "  $f"
done

echo "Reloading systemd…"
$sudo systemctl "${systemctl_args[@]}" daemon-reload

if (( do_start )); then
    echo "Starting $service_name…"
    $sudo systemctl "${systemctl_args[@]}" start "$service_name"
    echo
    $sudo systemctl "${systemctl_args[@]}" status --no-pager "$service_name" || true
else
    echo
    echo "Installed. Start the service with:"
    if [[ "$scope" == "user" ]]; then
        echo "  systemctl --user start $service_name"
        echo "  journalctl --user -u $service_name -f"
        echo
        echo "To keep it running after logout:  loginctl enable-linger \$USER"
    else
        echo "  sudo systemctl start $service_name"
        echo "  sudo journalctl -u $service_name -f"
    fi
fi
