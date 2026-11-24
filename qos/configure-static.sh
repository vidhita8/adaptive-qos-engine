
#!/usr/bin/env bash
set -euo pipefail

# Configure static HTB QoS on one router interface.
# Usage: configure-static.sh INTERFACE RATE

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo." >&2
    exit 1
fi

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 INTERFACE RATE" >&2
    exit 1
fi

IFACE="$1"
RATE="$2"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../testbed/topology.env"

# Validate interface and rate.
if [[ ! "$IFACE" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    echo "Invalid interface: $IFACE" >&2
    exit 1
fi

if [[ ! "$RATE" =~ ^[0-9]+(kbit|mbit|gbit)$ ]]; then
    echo "Invalid rate: $RATE" >&2
    exit 1
fi

# Convert a percentage of RATE into an HTB rate.
rate_percent() {
    local rate="$1"
    local percent="$2"
    local value factor total_kbit minimum_kbit

    case "$rate" in
        *kbit)
            value="${rate%kbit}"
            factor=1
            ;;
        *mbit)
            value="${rate%mbit}"
            factor=1000
            ;;
        *gbit)
            value="${rate%gbit}"
            factor=1000000
            ;;
        *)
            echo "Unsupported rate unit: $rate" >&2
            return 1
            ;;
    esac

    total_kbit=$((value * factor))
    minimum_kbit=$((total_kbit * percent / 100))

    echo "${minimum_kbit}kbit"
}

# Ensure the requested interface exists in the router namespace.
ip -n "$ROUTER_NAMESPACE" link show "$IFACE" >/dev/null 2>&1 || {
    echo "Interface $IFACE does not exist in $ROUTER_NAMESPACE" >&2
    exit 1
}

echo "[QOS] Configuring $IFACE at $RATE"

# Recreate the root qdisc. This script owns this interface's root.
ip netns exec "$ROUTER_NAMESPACE" tc qdisc del \
    dev "$IFACE" root 2>/dev/null || true

ip netns exec "$ROUTER_NAMESPACE" tc qdisc add \
    dev "$IFACE" root handle "$QOS_HTB_HANDLE" htb default 40 r2q 100

# Root HTB class.
ip netns exec "$ROUTER_NAMESPACE" tc class replace \
    dev "$IFACE" parent "$QOS_HTB_HANDLE" \
    classid "$QOS_HTB_ROOT_CLASS" htb \
    rate "$RATE" ceil "$RATE"

# Child classes. Each gets a minimum guarantee and can borrow up to RATE.
declare -a classes=(
    "$QOS_CLASS_INTERACTIVE|$QOS_INTERACTIVE_PERCENT|$QOS_INTERACTIVE_PRIORITY"
    "$QOS_CLASS_STREAMING|$QOS_STREAMING_PERCENT|$QOS_STREAMING_PRIORITY"
    "$QOS_CLASS_BULK|$QOS_BULK_PERCENT|$QOS_BULK_PRIORITY"
    "$QOS_CLASS_DEFAULT|$QOS_DEFAULT_PERCENT|$QOS_DEFAULT_PRIORITY"
)

for entry in "${classes[@]}"; do
    IFS="|" read -r class_id percent priority <<< "$entry"
    minimum="$(rate_percent "$RATE" "$percent")"

    ip netns exec "$ROUTER_NAMESPACE" tc class replace \
        dev "$IFACE" parent "$QOS_HTB_ROOT_CLASS" \
        classid "$class_id" htb \
        rate "$minimum" ceil "$RATE" prio "$priority"

    ip netns exec "$ROUTER_NAMESPACE" tc qdisc replace \
        dev "$IFACE" parent "$class_id" fq_codel \
        target "$QOS_FQ_CODEL_TARGET" \
        interval "$QOS_FQ_CODEL_INTERVAL"
done

echo "[QOS] $IFACE configured."
ip netns exec "$ROUTER_NAMESPACE" tc -s class show dev "$IFACE"
