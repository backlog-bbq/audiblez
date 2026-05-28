# Audiblez Quadlet units

[Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html) is
podman's native way of declaring containers as systemd units. Each `*.container`,
`*.volume`, and `*.network` file in this folder is generated into a real systemd
service at daemon-reload time. Compared to `compose.yaml`, Quadlet gives you:

- True systemd lifecycle (logging via `journalctl`, restart policies, dependencies).
- No long-running compose process — `systemd` supervises podman directly.
- Native rootless support with user-level units.

## Pick a unit

| File | Purpose |
|------|---------|
| `audiblez-cpu.container` | CPU-only image (`audiblez:cpu`). |
| `audiblez-cuda.container` | NVIDIA CUDA image (`audiblez:cuda`). Requires NVIDIA Container Toolkit + CDI. |
| `audiblez-outputs.volume` | Named volume for generated audiobooks (per-job folders with `.m4b` + intermediate `.wav` files). |
| `audiblez.volume` | Named volume for the HuggingFace model cache. |
| `audiblez.network` | Dedicated bridge network. |

## Build the image first

```sh
# CPU
podman build -t audiblez:cpu .

# CUDA
podman build -t audiblez:cuda \
  --build-arg BASE_IMAGE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04 \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124 .
```

## Install (rootless, recommended)

```sh
mkdir -p ~/.config/containers/systemd
cp quadlet/audiblez-cpu.container     ~/.config/containers/systemd/   # or cuda
cp quadlet/audiblez.volume            ~/.config/containers/systemd/
cp quadlet/audiblez-outputs.volume    ~/.config/containers/systemd/
cp quadlet/audiblez.network           ~/.config/containers/systemd/

systemctl --user daemon-reload
systemctl --user start audiblez-cpu.service
systemctl --user status audiblez-cpu.service
journalctl --user -u audiblez-cpu.service -f
```

Keep the unit running after logout:

```sh
loginctl enable-linger $USER
```

## Install (rootful)

```sh
sudo cp quadlet/audiblez-cpu.container     /etc/containers/systemd/
sudo cp quadlet/audiblez.volume            /etc/containers/systemd/
sudo cp quadlet/audiblez-outputs.volume    /etc/containers/systemd/
sudo cp quadlet/audiblez.network           /etc/containers/systemd/
sudo systemctl daemon-reload
sudo systemctl start audiblez-cpu.service
```

## CUDA on rootless podman

```sh
# Once per host, after installing the NVIDIA Container Toolkit:
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml

# Verify podman sees the device:
podman info | grep -A2 'cdi'
```

`AddDevice=nvidia.com/gpu=all` in `audiblez-cuda.container` then exposes the GPU
to the container without `--privileged`.

## Outputs

Generated audiobooks live in the `audiblez-outputs` named volume (declared by
`audiblez-outputs.volume`). The volume is managed by podman, survives container
rebuilds, and avoids host-side ownership / SELinux headaches.

The primary way to retrieve files is the web UI's download API — open
`http://localhost:8000`, finish a job, and click the download link next to the
`.m4b`. From the shell:

```sh
# List files in a job:
curl -s http://localhost:8000/api/jobs/<job-id>/files

# Download an M4B:
curl -OJ http://localhost:8000/api/jobs/<job-id>/download/<filename>.m4b
```

If you want direct filesystem access to the volume:

```sh
# Print the on-disk path (under ~/.local/share/containers/ for rootless):
podman volume inspect audiblez-outputs --format '{{.Mountpoint}}'

# Copy a file out:
podman cp audiblez-cpu:/app/outputs/<job-id>/<filename>.m4b ./
```

To clear out old jobs, either DELETE them via the API
(`DELETE /api/jobs/<job-id>`) or wipe the whole volume:

```sh
podman volume rm audiblez-outputs
```
