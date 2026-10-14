#!/bin/bash
# ================================================
# ROS2 Network Setup Script for Server
# VPN always active
# ================================================

#TODO: Add support for automatic detection of localhost VPN interface and IPs
# ── HARDCODED USER CONFIGURATION ──────────────────────────
VPN_LOCAL_IP=<local_ip>               # Your WSL2 VPN IP
VPN_REMOTE_IP=<destination_ip>        # Remote server VPN IP
VPN_INTERFACE=<local_interface>       # Your VPN interface

# Common Settings
ROS_DOMAIN="42"
CYCLONE_CONFIG="$(pwd)/cyclonedds_config.xml"

# ── HELPER FUNCTIONS ─────────────────────────────

check_dependencies() {
    if ! command -v ros2 &> /dev/null; then
        echo "[ERROR] ROS2 not found. Source your ROS2 installation first."
        exit 1
    fi
    if ! dpkg -l | grep -q "rmw-cyclonedds"; then
        echo "[ERROR] Cyclone DDS not installed."
        echo "        Run: sudo apt install ros-jazzy-rmw-cyclonedds-cpp"
        exit 1
    fi
}

detect_network() {
    echo "[INFO] Detecting network..."

    # Try VPN IP connectivity first
    if ping -c 1 -W 2 "$VPN_REMOTE_IP" &> /dev/null; then
        echo "[INFO] Remote server reachable on VPN IP"
        echo "remote"
    else
        echo "[WARN] Remote server not reachable on any network"
        echo "none"
    fi
}

write_cyclone_config() {
    local interface=$1
    local local_ip=$2
    local remote_ip=$3

    cat > "$CYCLONE_CONFIG" << EOF
<?xml version="1.0" encoding="UTF-8" ?>
<CycloneDDS>
  <Domain>
    <General>
      <Interfaces>
        <NetworkInterface name="${interface}" priority="default" multicast="false"/>
      </Interfaces>
      <AllowMulticast>false</AllowMulticast>
      <EnableMulticastLoopback>false</EnableMulticastLoopback>
    </General>
    <Discovery>
      <ParticipantIndex>auto</ParticipantIndex>
      <MaxAutoParticipantIndex>50</MaxAutoParticipantIndex>
      <Peers>
        <Peer address="${local_ip}"/>
        <Peer address="${remote_ip}"/>
      </Peers>
    </Discovery>
  </Domain>
</CycloneDDS>
EOF
    echo "[INFO] Cyclone config written to $CYCLONE_CONFIG"
}

set_env() {
    local local_ip=$1
    local remote_ip=$2

    export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
    export CYCLONEDDS_URI="file://${CYCLONE_CONFIG}"
    export ROS_DOMAIN_ID="$ROS_DOMAIN"
    export ROS_STATIC_PEERS="${local_ip};${remote_ip}"
    unset ROS_AUTOMATIC_DISCOVERY_RANGE
}

print_status() {
    local mode=$1
    local local_ip=$2
    local remote_ip=$3
    local interface=$4

    echo ""
    echo "================================================"
    echo "  ROS2 Setup Complete: ${mode} mode"
    echo "================================================"
    echo "  RMW:          $RMW_IMPLEMENTATION"
    echo "  Domain ID:    $ROS_DOMAIN_ID"
    echo "  Cyclone URI:  $CYCLONEDDS_URI"
    echo "  Local IP:     ${local_ip}"
    echo "  Remote IP:    ${remote_ip}"
    echo "  Interface:    ${interface}"
    echo "  Static Peers: $ROS_STATIC_PEERS"
    echo "================================================"
    echo ""
}

restart_daemon() {
    ros2 daemon stop &> /dev/null
    sleep 1
    ros2 daemon start &> /dev/null
    echo "[INFO] ROS2 daemon restarted"
}

setup_remote() {
    echo "[INFO] Configuring for REMOTE mode (VPN IPs)..."
    write_cyclone_config "$VPN_INTERFACE" "$VPN_LOCAL_IP" "$VPN_REMOTE_IP"
    set_env "$VPN_LOCAL_IP" "$VPN_REMOTE_IP"
    restart_daemon
    print_status "REMOTE" "$VPN_LOCAL_IP" "$VPN_REMOTE_IP" "$VPN_INTERFACE"
}

# ── MAIN ─────────────────────────────────────────

check_dependencies

case "$1" in
    setup)
        DETECTED=$(detect_network | tail -1)
        case "$DETECTED" in
            remote) setup_remote ;;
            none)
                echo "[ERROR] Remote server not reachable"
                echo "        Check your VPN or network connection"
                exit 1
                ;;
        esac
        ;;
    status)
        echo "=== Current ROS2 Environment ==="
        printenv | grep -E "ROS|CYCLONE|RMW"
        echo ""
        echo "=== Cyclone Config ==="
        cat "$CYCLONE_CONFIG" 2>/dev/null || echo "No config file found"
        ;;
    reset)
        echo "[INFO] Resetting ROS2 environment..."
        unset RMW_IMPLEMENTATION
        unset CYCLONEDDS_URI
        unset ROS_DOMAIN_ID
        unset ROS_STATIC_PEERS
        unset ROS_AUTOMATIC_DISCOVERY_RANGE
        ros2 daemon stop &> /dev/null
        echo "[INFO] Environment cleared"
        ;;
    *)
        echo "Usage: source ~/ros2_setup.sh [OPTION]"
        echo ""
        echo "Options:"
        echo "  setup   Auto-detect and configure environment variables"
        echo "  status  Show current ROS2 configuration"
        echo "  reset   Clear all ROS2 environment variables"
        echo ""
        echo "Examples:"
        echo "  source ~/ros2_setup.sh setup"
        ;;
esac
