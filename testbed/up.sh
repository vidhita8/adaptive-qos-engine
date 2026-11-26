#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/topology.env"

if [[ "$EUID" -ne 0 ]]; then
    echo "Please run this script with sudo:"
    echo "sudo $0"
    exit 1
fi

echo "======================================"
echo " Adaptive QoS Engine - Setup"
echo "======================================"

# Clean up any previous testbed instance
"$SCRIPT_DIR/down.sh"

# Create all project namespaces
for ns in $DEVICE_NAMESPACES $INFRA_NAMESPACES; do
    echo "[CREATE] Namespace: $ns"
    ip netns add "$ns"
    ip -n "$ns" link set lo up
done
# Create the router download IFB interface.
# It must exist in both no-QoS and static-QoS configurations.
echo "[CREATE] Router download IFB: $ROUTER_IFB"

modprobe ifb

ip -n qos-router link add \
    name "$ROUTER_IFB" \
    type ifb

ip -n qos-router link set "$ROUTER_IFB" up
# Create router LAN bridge
echo "[CREATE] Router LAN bridge: $ROUTER_BRIDGE"

ip -n qos-router link add \
    name "$ROUTER_BRIDGE" \
    type bridge

# Device-to-router interface mappings
declare -A DEVICE_LINKS=(
    [qos-call]="$CALL_ROUTER_IF"
    [qos-game]="$GAME_ROUTER_IF"
    [qos-tv1]="$TV1_ROUTER_IF"
    [qos-tv2]="$TV2_ROUTER_IF"
    [qos-tv3]="$TV3_ROUTER_IF"
    [qos-bulk]="$BULK_ROUTER_IF"
    [qos-laptop]="$LAPTOP_ROUTER_IF"
)

# Create virtual Ethernet pairs
for ns in $DEVICE_NAMESPACES; do
    router_if="${DEVICE_LINKS[$ns]}"
    host_if="v-${router_if}"

    echo "[CREATE] $ns <-> qos-router ($host_if / $router_if)"

    ip link add "$host_if" type veth peer name "peer-${router_if}"

    ip link set "peer-${router_if}" netns "$ns"
    ip -n "$ns" link set "peer-${router_if}" name "$DEVICE_INTERFACE"

    ip link set "$host_if" netns qos-router
    ip -n qos-router link set "$host_if" name "$router_if"

    ip -n qos-router link set "$router_if" master "$ROUTER_BRIDGE"

    ip -n "$ns" link set "$DEVICE_INTERFACE" mtu "$TESTBED_MTU"
    ip -n qos-router link set "$router_if" mtu "$TESTBED_MTU"

    ip -n "$ns" link set "$DEVICE_INTERFACE" up
    ip -n qos-router link set "$router_if" up
done

# Bring up the LAN bridge
ip -n qos-router link set "$ROUTER_BRIDGE" mtu "$TESTBED_MTU"
ip -n qos-router link set "$ROUTER_BRIDGE" up

# ==========================================
# Router-to-modem link
# ==========================================

echo "[CREATE] qos-router <-> qos-modem"

ip link add "$ROUTER_MODEM_IF" type veth \
    peer name "peer-${ROUTER_MODEM_IF}"

ip link set "$ROUTER_MODEM_IF" netns qos-router
ip link set "peer-${ROUTER_MODEM_IF}" netns qos-modem

ip -n qos-modem link set "peer-${ROUTER_MODEM_IF}" name "$MODEM_ROUTER_IF"

ip -n qos-router link set "$ROUTER_MODEM_IF" mtu "$TESTBED_MTU"
ip -n qos-modem link set "$MODEM_ROUTER_IF" mtu "$TESTBED_MTU"

ip -n qos-router link set "$ROUTER_MODEM_IF" up
ip -n qos-modem link set "$MODEM_ROUTER_IF" up

# ==========================================
# Modem-to-ISP link
# ==========================================

echo "[CREATE] qos-modem <-> qos-isp"

ip link add "$MODEM_ISP_IF" type veth \
    peer name "peer-${MODEM_ISP_IF}"

ip link set "$MODEM_ISP_IF" netns qos-modem
ip link set "peer-${MODEM_ISP_IF}" netns qos-isp

ip -n qos-isp link set "peer-${MODEM_ISP_IF}" name "$ISP_INTERFACE"

ip -n qos-modem link set "$MODEM_ISP_IF" mtu "$TESTBED_MTU"
ip -n qos-isp link set "$ISP_INTERFACE" mtu "$TESTBED_MTU"

ip -n qos-modem link set "$MODEM_ISP_IF" up
ip -n qos-isp link set "$ISP_INTERFACE" up

# ==========================================
# Device IP addressing
# ==========================================

declare -A DEVICE_IPV4=(
    [qos-call]="$CALL_IPV4"
    [qos-game]="$GAME_IPV4"
    [qos-tv1]="$TV1_IPV4"
    [qos-tv2]="$TV2_IPV4"
    [qos-tv3]="$TV3_IPV4"
    [qos-bulk]="$BULK_IPV4"
    [qos-laptop]="$LAPTOP_IPV4"
)

declare -A DEVICE_IPV6=(
    [qos-call]="$CALL_IPV6"
    [qos-game]="$GAME_IPV6"
    [qos-tv1]="$TV1_IPV6"
    [qos-tv2]="$TV2_IPV6"
    [qos-tv3]="$TV3_IPV6"
    [qos-bulk]="$BULK_IPV6"
    [qos-laptop]="$LAPTOP_IPV6"
)

echo "[CONFIG] Device IP addresses"

for ns in $DEVICE_NAMESPACES; do
    ip -n "$ns" addr add "${DEVICE_IPV4[$ns]}/24" dev "$DEVICE_INTERFACE"
    ip -n "$ns" -6 addr add "${DEVICE_IPV6[$ns]}/64" dev "$DEVICE_INTERFACE" nodad
done

# ==========================================
# Router IP addressing
# ==========================================

echo "[CONFIG] Router IP addresses"

ip -n qos-router addr add "$ROUTER_LAN_IPV4" dev "$ROUTER_BRIDGE"
ip -n qos-router -6 addr add "$ROUTER_LAN_IPV6" dev "$ROUTER_BRIDGE" nodad

ip -n qos-router addr add "$ROUTER_WAN_IPV4" dev "$ROUTER_MODEM_IF"
ip -n qos-router -6 addr add "$ROUTER_WAN_IPV6" dev "$ROUTER_MODEM_IF" nodad

# ==========================================
# Modem IP addressing
# ==========================================

echo "[CONFIG] Modem IP addresses"

ip -n qos-modem addr add "$MODEM_LAN_IPV4" dev "$MODEM_ROUTER_IF"
ip -n qos-modem -6 addr add "$MODEM_LAN_IPV6" dev "$MODEM_ROUTER_IF" nodad

ip -n qos-modem addr add "$MODEM_WAN_IPV4" dev "$MODEM_ISP_IF"
ip -n qos-modem -6 addr add "$MODEM_WAN_IPV6" dev "$MODEM_ISP_IF" nodad

# ==========================================
# ISP IP addressing
# ==========================================

echo "[CONFIG] ISP IP addresses"

ip -n qos-isp addr add "$ISP_IPV4" dev "$ISP_INTERFACE"
ip -n qos-isp -6 addr add "$ISP_IPV6" dev "$ISP_INTERFACE" nodad

# ==========================================
# Enable IPv4 and IPv6 forwarding
# ==========================================

echo "[CONFIG] Enabling packet forwarding"

for ns in qos-router qos-modem; do
    ip netns exec "$ns" sysctl -w net.ipv4.ip_forward="$ENABLE_IPV4_FORWARDING"

    ip netns exec "$ns" sysctl -w net.ipv6.conf.all.forwarding="$ENABLE_IPV6_FORWARDING"

    ip netns exec "$ns" sysctl -w net.ipv6.conf.default.accept_ra="$DISABLE_ACCEPT_RA"
    ip netns exec "$ns" sysctl -w net.ipv6.conf.all.accept_ra="$DISABLE_ACCEPT_RA"
done


# ==========================================
# Configure static routes
# ==========================================

echo
echo "[ROUTES] Configuring device default routes..."

# All devices send external traffic to the router
for ns in $DEVICE_NAMESPACES; do
    ip -n "$ns" route add default via 10.10.0.1 dev "$DEVICE_INTERFACE"
    ip -n "$ns" -6 route add default via fd10:10::1 dev "$DEVICE_INTERFACE"
done

echo "[ROUTES] Configuring router routes..."

# Router sends external traffic to the modem
ip -n qos-router route add default via 10.20.0.2 dev "$ROUTER_MODEM_IF"
ip -n qos-router -6 route add default via fd10:20::2 dev "$ROUTER_MODEM_IF"

echo "[ROUTES] Configuring modem routes..."

# Modem sends external traffic to the ISP
ip -n qos-modem route add default via 10.30.0.2 dev "$MODEM_ISP_IF"
ip -n qos-modem -6 route add default via fd10:30::2 dev "$MODEM_ISP_IF"

# Modem sends LAN return traffic back through the router
ip -n qos-modem route add "$LAN_IPV4" via 10.20.0.1 dev "$MODEM_ROUTER_IF"
ip -n qos-modem -6 route add "$LAN_IPV6" via fd10:20::1 dev "$MODEM_ROUTER_IF"

echo "[ROUTES] Configuring ISP return routes..."

# ISP routes LAN and router transit traffic back through the modem
ip -n qos-isp route add "$LAN_IPV4" via 10.30.0.1 dev "$ISP_INTERFACE"
ip -n qos-isp route add 10.20.0.0/30 via 10.30.0.1 dev "$ISP_INTERFACE"

ip -n qos-isp -6 route add "$LAN_IPV6" via fd10:30::1 dev "$ISP_INTERFACE"
ip -n qos-isp -6 route add fd10:20::/126 via fd10:30::1 dev "$ISP_INTERFACE"

echo "[ROUTES] Static routes configured."


# ==========================================
# Disable virtual interface offloads
# ==========================================

echo
echo "[OFFLOAD] Disabling TSO, GSO and GRO..."

disable_offloads() {
    local ns="$1"
    local iface="$2"

    echo "[OFFLOAD] $ns:$iface"

    ip netns exec "$ns" ethtool -K "$iface" tso off gso off gro off || {
        echo "[ERROR] Failed to disable offloads on $ns:$iface" >&2
        exit 1
    }
}

# Device-side veth interfaces
for ns in $DEVICE_NAMESPACES; do
    disable_offloads "$ns" "$DEVICE_INTERFACE"
done

# Router-side device veth interfaces
disable_offloads qos-router "$CALL_ROUTER_IF"
disable_offloads qos-router "$GAME_ROUTER_IF"
disable_offloads qos-router "$TV1_ROUTER_IF"
disable_offloads qos-router "$TV2_ROUTER_IF"
disable_offloads qos-router "$TV3_ROUTER_IF"
disable_offloads qos-router "$BULK_ROUTER_IF"
disable_offloads qos-router "$LAPTOP_ROUTER_IF"

# Router-modem and modem-ISP veth interfaces
disable_offloads qos-router "$ROUTER_MODEM_IF"
disable_offloads qos-modem "$MODEM_ROUTER_IF"
disable_offloads qos-modem "$MODEM_ISP_IF"
disable_offloads qos-isp "$ISP_INTERFACE"

echo "[OFFLOAD] All virtual interface offloads disabled."


# ==========================================
# Configure modem broadband bottleneck
# ==========================================

echo
echo "[QDISC] Configuring modem bottleneck..."

configure_bottleneck() {
    local iface="$1"
    local direction="$2"

    echo "[QDISC] $direction on $iface"

    # Root qdisc: controls bandwidth and queue size
    ip netns exec qos-modem tc qdisc add \
        dev "$iface" root \
        handle "$MODEM_QDISC_ROOT_HANDLE" \
        "$MODEM_QDISC_ROOT_KIND" \
        rate "$MODEM_RATE" \
        burst "$MODEM_BURST" \
        limit "$MODEM_LIMIT"

    # Child qdisc: introduces delay, jitter and packet loss
    ip netns exec qos-modem tc qdisc add \
        dev "$iface" \
        parent "$MODEM_QDISC_CHILD_PARENT" \
        handle "$MODEM_QDISC_CHILD_HANDLE" \
        "$MODEM_QDISC_CHILD_KIND" \
        limit "$MODEM_NETEM_LIMIT" \
        delay "$MODEM_DELAY" "$MODEM_JITTER" \
        loss "$MODEM_LOSS"
}


# Upload: traffic leaving the modem towards the ISP
configure_bottleneck "$MODEM_ISP_IF" "UPLOAD"

# Download: traffic leaving the modem towards the router
configure_bottleneck "$MODEM_ROUTER_IF" "DOWNLOAD"

echo "[QDISC] Modem bottleneck configured."

echo
echo "LAN and infrastructure links created and addressed."
