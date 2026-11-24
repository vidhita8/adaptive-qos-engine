#!/usr/bin/env bash
set -euo pipefail

# Apply the complete static QoS configuration.
# Usage: apply-static.sh RATE
# Example: sudo /usr/bin/bash qos/apply-static.sh 95mbit

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo." >&2
    exit 1
fi

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 RATE" >&2
    exit 1
fi

RATE="$1"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../testbed/topology.env"

WAN="r-wan"
IFB="$ROUTER_IFB"
TABLE="qos"

echo "[QOS] Applying static QoS at $RATE"

# Verify the router namespace and WAN interface.
ip netns exec "$ROUTER_NAMESPACE" ip link show "$WAN" >/dev/null

# Create the IFB device if it does not already exist.
modprobe ifb

if ! ip -n "$ROUTER_NAMESPACE" link show "$IFB" >/dev/null 2>&1; then
    ip -n "$ROUTER_NAMESPACE" link add "$IFB" type ifb
fi

ip -n "$ROUTER_NAMESPACE" link set dev "$IFB" up

# Configure HTB and fq_codel in both directions.
/usr/bin/bash "$SCRIPT_DIR/configure-static.sh" "$WAN" "$RATE"
/usr/bin/bash "$SCRIPT_DIR/configure-static.sh" "$IFB" "$RATE"

# Replace only the project-owned nftables table.
ip netns exec "$ROUTER_NAMESPACE" nft delete table inet "$TABLE" \
    2>/dev/null || true

ip netns exec "$ROUTER_NAMESPACE" nft -f - <<NFT
table inet qos {
    chain prerouting {
        type filter hook prerouting priority mangle; policy accept;

        udp dport $CALL_PORT meta mark set $QOS_MARK_CALL
        udp dport $GAME_PORT meta mark set $QOS_MARK_GAME
        tcp dport $HTTP_PORT meta mark set $QOS_MARK_STREAMING
        tcp dport $IPERF3_PORT meta mark set $QOS_MARK_BULK
    }
}
NFT

# Upload: route marked packets into their HTB classes.
for iface in "$WAN" "$IFB"; do
    for entry in \
        "$QOS_MARK_CALL|10|$QOS_CLASS_INTERACTIVE" \
        "$QOS_MARK_GAME|11|$QOS_CLASS_INTERACTIVE" \
        "$QOS_MARK_STREAMING|20|$QOS_CLASS_STREAMING" \
        "$QOS_MARK_BULK|30|$QOS_CLASS_BULK"
    do
        IFS="|" read -r mark priority class_id <<< "$entry"

        ip netns exec "$ROUTER_NAMESPACE" tc filter replace \
            dev "$iface" parent 1: protocol all \
            prio "$priority" handle "$mark" fw flowid "$class_id"
    done
done

# Download: classify packets on WAN ingress, then redirect them to IFB.
# The skb mark survives this redirect.
ip netns exec "$ROUTER_NAMESPACE" tc qdisc del \
    dev "$WAN" ingress 2>/dev/null || true

ip netns exec "$ROUTER_NAMESPACE" tc qdisc add \
    dev "$WAN" handle ffff: ingress

add_download_filter() {
    local protocol="$1"
    local ip_protocol="$2"
    local port="$3"
    local mark="$4"
    local priority="$5"

    ip netns exec "$ROUTER_NAMESPACE" tc filter replace \
        dev "$WAN" parent ffff: protocol "$protocol" \
        prio "$priority" flower \
        ip_proto "$ip_protocol" src_port "$port" \
        action skbedit mark "$mark" pipe \
        action mirred egress redirect dev "$IFB"
}

# IPv4 download classification.
add_download_filter ip udp "$CALL_PORT" "$QOS_MARK_CALL" 10
add_download_filter ip udp "$GAME_PORT" "$QOS_MARK_GAME" 11
add_download_filter ip tcp "$HTTP_PORT" "$QOS_MARK_STREAMING" 20
add_download_filter ip tcp "$IPERF3_PORT" "$QOS_MARK_BULK" 30

# IPv6 download classification.
add_download_filter ipv6 udp "$CALL_PORT" "$QOS_MARK_CALL" 110
add_download_filter ipv6 udp "$GAME_PORT" "$QOS_MARK_GAME" 111
add_download_filter ipv6 tcp "$HTTP_PORT" "$QOS_MARK_STREAMING" 120
add_download_filter ipv6 tcp "$IPERF3_PORT" "$QOS_MARK_BULK" 130

# Redirect all remaining download traffic as well.
# Unmarked packets enter the default HTB class on IFB.
# Remove an existing catch-all filter, then add it explicitly.
ip netns exec "$ROUTER_NAMESPACE" tc filter del \
    dev "$WAN" parent ffff: protocol all prio 1000 \
    2>/dev/null || true

ip netns exec "$ROUTER_NAMESPACE" tc filter add \
    dev "$WAN" parent ffff: protocol all prio 1000 \
    matchall action mirred egress redirect dev "$IFB"

echo "[QOS] Static QoS configuration applied."
echo
echo "=== WAN classes ==="
ip netns exec "$ROUTER_NAMESPACE" tc class show dev "$WAN"
echo
echo "=== IFB classes ==="
ip netns exec "$ROUTER_NAMESPACE" tc class show dev "$IFB"
echo
echo "=== WAN ingress filters ==="
ip netns exec "$ROUTER_NAMESPACE" tc filter show dev "$WAN" parent ffff:
echo
echo "=== nftables rules ==="
ip netns exec "$ROUTER_NAMESPACE" nft list table inet "$TABLE"
