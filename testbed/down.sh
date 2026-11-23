#!/usr/bin/env bash

set -euo pipefail

# Resolve project directory
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Load central topology configuration
source "$SCRIPT_DIR/topology.env"

# Require root privileges
if [[ "$EUID" -ne 0 ]]; then
    echo "Please run this script with sudo:"
    echo "sudo $0"
    exit 1
fi

echo "======================================"
echo " Adaptive QoS Engine - Teardown"
echo "======================================"

# Remove only namespaces declared by this project
for ns in $DEVICE_NAMESPACES $INFRA_NAMESPACES; do
    if ip netns list | awk '{print $1}' | grep -Fxq "$ns"; then
        echo "[REMOVE] $ns"
        ip netns delete "$ns"
    else
        echo "[SKIP]   $ns does not exist"
    fi
done

echo
echo "Teardown complete."