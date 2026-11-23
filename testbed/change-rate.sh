#!/usr/bin/env bash

set -euo pipefail

# Resolve project directory
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Load central topology configuration
source "$SCRIPT_DIR/topology.env"

# Require root privileges
if [[ "$EUID" -ne 0 ]]; then
    echo "Please run this script with sudo:"
    echo "sudo $0 <rate>"
    exit 1
fi

# Validate argument count
if [[ "$#" -ne 1 ]]; then
    echo "Usage: sudo $0 <rate>"
    echo "Examples: 20mbit, 50mbit, 100mbit"
    exit 1
fi

NEW_RATE="$1"

# Accept positive integer rates with supported units
if [[ ! "$NEW_RATE" =~ ^[1-9][0-9]*(bit|kbit|mbit|gbit)$ ]]; then
    echo "[ERROR] Invalid rate: $NEW_RATE"
    echo "Use a positive integer followed by bit, kbit, mbit or gbit."
    exit 1
fi

echo "======================================"
echo " Adaptive QoS Engine - Rate Controller"
echo "======================================"

# Verify that the modem namespace exists
if ! ip netns list | awk '{print $1}' | grep -Fxq "qos-modem"; then
    echo "[ERROR] qos-modem namespace does not exist."
    echo "Run sudo ./testbed/up.sh first."
    exit 1
fi

# Verify the configured root qdisc and child qdisc exist
for iface in "$MODEM_ISP_IF" "$MODEM_ROUTER_IF"; do
    if ! ip netns exec qos-modem tc qdisc show dev "$iface" \
        | grep -Fq "qdisc $MODEM_QDISC_ROOT_KIND $MODEM_QDISC_ROOT_HANDLE root"; then
        echo "[ERROR] Expected $MODEM_QDISC_ROOT_KIND root missing on $iface."
        exit 1
    fi

    if ! ip netns exec qos-modem tc qdisc show dev "$iface" \
        | grep -Fq "qdisc $MODEM_QDISC_CHILD_KIND $MODEM_QDISC_CHILD_HANDLE parent $MODEM_QDISC_CHILD_PARENT"; then
        echo "[ERROR] Expected $MODEM_QDISC_CHILD_KIND child missing on $iface."
        exit 1
    fi
done

echo "[RATE] Updating modem upload and download to $NEW_RATE..."

# Update upload TBF without replacing the qdisc hierarchy
ip netns exec qos-modem tc qdisc change \
    dev "$MODEM_ISP_IF" root \
    handle "$MODEM_QDISC_ROOT_HANDLE" \
    "$MODEM_QDISC_ROOT_KIND" \
    rate "$NEW_RATE" \
    burst "$MODEM_BURST" \
    limit "$MODEM_LIMIT"

# Update download TBF without replacing the qdisc hierarchy
ip netns exec qos-modem tc qdisc change \
    dev "$MODEM_ROUTER_IF" root \
    handle "$MODEM_QDISC_ROOT_HANDLE" \
    "$MODEM_QDISC_ROOT_KIND" \
    rate "$NEW_RATE" \
    burst "$MODEM_BURST" \
    limit "$MODEM_LIMIT"

echo
echo "[RATE] Rate updated successfully."
echo "[RATE] Upload:   $NEW_RATE"
echo "[RATE] Download: $NEW_RATE"

echo
echo "===== Upload qdisc ====="
ip netns exec qos-modem tc -s qdisc show dev "$MODEM_ISP_IF"

echo
echo "===== Download qdisc ====="
ip netns exec qos-modem tc -s qdisc show dev "$MODEM_ROUTER_IF"
