#!/usr/bin/env bash
# Installs the NVIDIA Container Toolkit on Debian/Ubuntu so Docker can use the GPU
# (needed by docker-compose.gpu.yml for the transcriber). Requires sudo.
set -euo pipefail

echo "=== Adding NVIDIA Container Toolkit repo ==="
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

echo "=== Installing nvidia-container-toolkit ==="
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit

echo "=== Configuring Docker runtime ==="
sudo nvidia-ctk runtime configure --runtime=docker

echo "=== Restarting Docker ==="
sudo systemctl restart docker

echo "=== Verifying GPU access from Docker ==="
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi

echo "=== Done! GPU is available in Docker. ==="
