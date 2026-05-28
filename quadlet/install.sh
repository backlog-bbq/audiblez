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
#   quadlet/install.sh --build          # build the image first (uses podman)
#   quadlet/install.sh --start          # also start the service after install
#   quadlet/install.sh --no-restart     # don't auto-restart a running service
#   quadlet/install.sh --uninstall      # remove the units (matches other flags)
#   quadlet/install.sh --help
#
# Quadlet only runs containers — it never builds. You must either build the
# image yourself (`podman build -t audiblez:cpu .`) before starting the
# service, or pass --build here.
#
# If the service is already running when you reinstall, the script restarts
# it after daemon-reload so the new unit / image takes effect. Pass
# --no-restart to skip that.
#
# Quadlet requires podman >= 4.4 and a systemd-managed host.

set -euo pipefail

variant="cpu"
scope="user"
do_start=0
do_build=0
do_uninstall=0
do_restart=1   # auto-restart if the service was running pre-reinstall

for arg in "$@"; do
    case "$arg" in
        --cuda) variant="cuda" ;;
        --cpu) variant="cpu" ;;
        --system|--rootful) scope="system" ;;
        --user|--rootless) scope="user" ;;
        --build) do_build=1 ;;
        --start) do_start=1 ;;
        --no-restart) do_restart=0 ;;
        --uninstall) do_uninstall=1 ;;
        -h|--help)
            sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
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
repo_root="$(cd "$script_dir/.." && pwd)"

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

build_image() {
    if ! command -v podman >/dev/null 2>&1; then
        echo "podman not found in PATH; can't build the image." >&2
        exit 1
    fi
    echo "Building localhost/audiblez:${variant} from $repo_root"
    build_sudo=""
    [[ "$scope" == "system" ]] && build_sudo="sudo"
    if [[ "$variant" == "cuda" ]]; then
        $build_sudo podman build \
            -t "localhost/audiblez:cuda" \
            --build-arg BASE_IMAGE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 \
            --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124 \
            "$repo_root"
    else
        $build_sudo podman build -t "localhost/audiblez:cpu" "$repo_root"
    fi
}

# Build the image now, or warn that the user has to do it themselves.
# Note: rootless podman keeps its image store under the calling user; rootful
# uses a different store. The quadlet service runs in the same scope as the
# install, so the image must exist in *that* scope's store.
if (( do_build )); then
    build_image
elif command -v podman >/dev/null 2>&1; then
    check_sudo=""
    [[ "$scope" == "system" ]] && check_sudo="sudo"
    if ! $check_sudo podman image exists "localhost/audiblez:${variant}" 2>/dev/null; then
        echo "Warning: image 'localhost/audiblez:${variant}' not found in the"
        echo "${scope} podman store. The service will fail to start until you"
        echo "build it. Re-run with --build, or build manually:"
        if [[ "$variant" == "cuda" ]]; then
            echo "    ${check_sudo} podman build -t localhost/audiblez:cuda \\"
            echo "      --build-arg BASE_IMAGE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 \\"
            echo "      --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124 ."
        else
            echo "    ${check_sudo} podman build -t localhost/audiblez:cpu ."
        fi
        echo
    fi
fi

# Capture whether the service is already running so we can restart it after
# daemon-reload to pick up the new unit / image. Has to happen *before* we
# overwrite the unit files — otherwise the answer is meaningless.
was_active=0
if $sudo systemctl "${systemctl_args[@]}" is-active --quiet "$service_name" 2>/dev/null; then
    was_active=1
    echo "Detected running $service_name; will restart after reinstall."
fi

echo "Installing audiblez Quadlet units → $target_dir"
$sudo mkdir -p "$target_dir"
for f in "$container_unit" "${support_units[@]}"; do
    $sudo install -m 0644 "$script_dir/$f" "$target_dir/$f"
    echo "  $f"
done

echo "Reloading systemd…"
$sudo systemctl "${systemctl_args[@]}" daemon-reload

# Verify the Quadlet generator actually produced the .service unit.
# If a .container file has a syntax error or unknown directive, the unit
# is silently skipped and `systemctl start` later reports "Unit not found".
if ! $sudo systemctl "${systemctl_args[@]}" cat "$service_name" >/dev/null 2>&1; then
    echo
    echo "ERROR: Quadlet did not generate $service_name." >&2
    echo "Common causes:" >&2
    echo "  - podman < 4.4 (no Quadlet support)" >&2
    echo "  - a syntax error or unknown directive in a .container/.volume/.network file" >&2
    echo "  - rootless: your user systemd instance isn't running (try 'loginctl enable-linger \$USER')" >&2
    echo
    # Quadlet ships a dry-run binary that prints exactly what it would (or would not) generate.
    quadlet_bin=""
    for candidate in /usr/libexec/podman/quadlet /usr/lib/podman/quadlet /usr/lib/systemd/user-generators/podman-user-generator; do
        if [[ -x "$candidate" ]]; then quadlet_bin="$candidate"; break; fi
    done
    if [[ -n "$quadlet_bin" ]]; then
        echo "Quadlet dry-run output ($quadlet_bin):" >&2
        if [[ "$scope" == "user" ]]; then
            "$quadlet_bin" -dryrun -user 2>&1 | sed 's/^/  /' >&2
        else
            $sudo "$quadlet_bin" -dryrun 2>&1 | sed 's/^/  /' >&2
        fi
    fi
    exit 1
fi
echo "Generated: $service_name"

if (( was_active )) && (( do_restart )); then
    echo "Restarting $service_name to pick up the new unit…"
    $sudo systemctl "${systemctl_args[@]}" restart "$service_name"
    echo
    $sudo systemctl "${systemctl_args[@]}" status --no-pager "$service_name" || true
elif (( do_start )); then
    echo "Starting $service_name…"
    $sudo systemctl "${systemctl_args[@]}" start "$service_name"
    echo
    $sudo systemctl "${systemctl_args[@]}" status --no-pager "$service_name" || true
else
    echo
    if (( was_active )); then
        echo "Skipped auto-restart (--no-restart). Apply the new unit yourself with:"
    else
        echo "Installed. Start the service with:"
    fi
    if [[ "$scope" == "user" ]]; then
        if (( was_active )); then
            echo "  systemctl --user restart $service_name"
        else
            echo "  systemctl --user start $service_name"
        fi
        echo "  journalctl --user -u $service_name -f"
        echo
        echo "To keep it running after logout:  loginctl enable-linger \$USER"
    else
        if (( was_active )); then
            echo "  sudo systemctl restart $service_name"
        else
            echo "  sudo systemctl start $service_name"
        fi
        echo "  sudo journalctl -u $service_name -f"
    fi
fi
