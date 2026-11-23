#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/topology.env"

if [[ "$EUID" -ne 0 ]]; then
    echo "Please run this script with sudo:"
    echo "sudo $0"
    exit 1
fi

ROUTER_IPV4="${ROUTER_LAN_IPV4%/*}"
ROUTER_IPV6="${ROUTER_LAN_IPV6%/*}"
ISP_TARGET_IPV4="${ISP_IPV4%/*}"
ISP_TARGET_IPV6="${ISP_IPV6%/*}"

read -r -a DEVICES <<< "$DEVICE_NAMESPACES"
read -r -a IPV4_ADDRESSES <<< "$DEVICE_IPV4_ADDRESSES"
read -r -a IPV6_ADDRESSES <<< "$DEVICE_IPV6_ADDRESSES"

if [[ "${#DEVICES[@]}" -ne "${#IPV4_ADDRESSES[@]}" ||
      "${#DEVICES[@]}" -ne "${#IPV6_ADDRESSES[@]}" ]]; then
    echo "[ERROR] Device and address list lengths do not match."
    exit 1
fi

echo "======================================"
echo " Adaptive QoS Engine - Smoke Test"
echo "======================================"

TOTAL=0
PASSED=0
FAILED=0

check_ping() {
    local ns="$1"
    local family="$2"
    local target_name="$3"
    local target_ip="$4"

    TOTAL=$((TOTAL + 1))

    echo -n "[TEST] $ns -> $target_name ($family): "

    if ip netns exec "$ns" ping "$family" \
        -n -c 3 -W 2 "$target_ip" >/dev/null 2>&1; then
        echo "PASS"
        PASSED=$((PASSED + 1))
    else
        echo "FAIL"
        FAILED=$((FAILED + 1))
    fi
}

for i in "${!DEVICES[@]}"; do
    ns="${DEVICES[$i]}"

    if ! ip netns list | awk '{print $1}' | grep -Fxq "$ns"; then
        echo "[ERROR] Namespace $ns does not exist."
        exit 1
    fi

    check_ping "$ns" "-4" "router" "$ROUTER_IPV4"
    check_ping "$ns" "-4" "isp" "$ISP_TARGET_IPV4"

    check_ping "$ns" "-6" "router" "$ROUTER_IPV6"
    check_ping "$ns" "-6" "isp" "$ISP_TARGET_IPV6"
done

echo
echo "======================================"
echo " Smoke Test Results"
echo "======================================"
echo "Total:  $TOTAL"
echo "Passed: $PASSED"
echo "Failed: $FAILED"

if [[ "$FAILED" -ne 0 ]]; then
    echo
    echo "[FAIL] Smoke test did not pass."
    exit 1
fi

echo
echo "[PASS] All smoke tests passed."
