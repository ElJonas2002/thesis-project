#!/bin/bash
# Source this script so ROS 2 nodes launched in this shell inherit the settings.

_ros2_vpn_usage() {
    cat <<'EOF'
Usage:
  source scripts/ros2_vpn_setup.sh setup <REMOTE_IPV4> [DOMAIN_ID]
  source scripts/ros2_vpn_setup.sh status
  source scripts/ros2_vpn_setup.sh reset
  bash scripts/ros2_vpn_setup.sh --help

setup  Select the source IPv4 and interface from the route to REMOTE_IPV4.
       DOMAIN_ID defaults to 42 (decimal, 0-232). Both hosts must match.
       Requires a working ROS 2 overlay, CycloneDDS, iproute2 and ping.
       A successful ping is required; a route alone does not prove VPN use.
status Show the current environment and active/local CycloneDDS XML.
reset  Stop the current ROS 2 daemon and clear ROS/DDS settings; keep XML.

XML and install/setup.bash are resolved relative to the repository root,
not the current directory. Restart existing nodes to apply new settings.
DDS domains 0-101 are recommended on Linux to avoid ephemeral UDP ports.
EOF
}

_ros2_vpn_error() {
    printf '[ERROR] %s\n' "$*" >&2
}

_ros2_vpn_ipv4() {
    local value=$1 octet
    local -a octets
    [[ $value =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    IFS=. read -r -a octets <<< "$value"
    for octet in "${octets[@]}"; do
        # Reject ambiguous leading zeros as well as out-of-range octets.
        [[ $octet == 0 || $octet != 0* ]] || return 1
        (( 10#$octet <= 255 )) || return 1
    done
    (( octets[0] > 0 && octets[0] != 127 && octets[0] < 224 ))
}

_ros2_vpn_xml_escape() {
    local value=$1
    value=${value//&/\&amp;}
    value=${value//</\&lt;}
    value=${value//>/\&gt;}
    value=${value//\"/\&quot;}
    value=${value//\'/\&apos;}
    printf '%s' "$value"
}

_ros2_vpn_write_config() {
    local config=$1 interface=$2 local_ip=$3 remote_ip=$4 temporary
    if [[ -e $config && ! -f $config ]]; then
        _ros2_vpn_error "Config path is not a regular file: $config"
        return 1
    fi
    interface=$(_ros2_vpn_xml_escape "$interface") || return 1
    if ! temporary=$(mktemp "${config}.XXXXXX"); then
        _ros2_vpn_error "Cannot create a temporary config beside $config"
        return 1
    fi
    if ! cat > "$temporary" <<EOF
<?xml version="1.0" encoding="UTF-8" ?>
<CycloneDDS>
  <Domain>
    <General>
      <Interfaces>
        <NetworkInterface name="${interface}" address="${local_ip}" priority="default" multicast="false"/>
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
    then
        _ros2_vpn_error "Cannot write CycloneDDS config: $temporary"
        rm -f -- "$temporary"
        return 1
    fi
    if ! mv -fT -- "$temporary" "$config"; then
        _ros2_vpn_error "Cannot replace CycloneDDS config: $config"
        rm -f -- "$temporary"
        return 1
    fi
    printf '[INFO] CycloneDDS config written to %s\n' "$config"
}

_ros2_vpn_status() {
    local config=$1 name
    printf '=== Current ROS 2 Environment ===\n'
    for name in RMW_IMPLEMENTATION CYCLONEDDS_URI ROS_DOMAIN_ID \
        ROS_STATIC_PEERS ROS_AUTOMATIC_DISCOVERY_RANGE; do
        printf '%s=%s\n' "$name" "${!name-<unset>}"
    done
    if [[ -n ${CYCLONEDDS_URI-} ]]; then
        case $CYCLONEDDS_URI in
            file:///*) config=${CYCLONEDDS_URI#file://} ;;
            *)
                printf '[INFO] CYCLONEDDS_URI is not a single local file URI; not reading it.\n'
                return 0
                ;;
        esac
        printf '\n=== Active CycloneDDS file: %s ===\n' "$config"
    else
        printf '\n=== Repository config (not active): %s ===\n' "$config"
    fi
    if [[ ! -e $config ]]; then
        printf '[INFO] No config file found.\n'
    elif [[ ! -f $config ]] || ! cat -- "$config"; then
        _ros2_vpn_error "Cannot read config file: $config"
        return 1
    fi
}

_ros2_vpn_main() {
    local mode=${1-} root config overlay remote_ip domain
    local route interface='' local_ip='' addresses token address found=0 result=0
    local previous_domain=${ROS_DOMAIN_ID-0}
    local -a fields

    case $mode in
        help|-h|--help)
            if (( $# != 1 )); then
                _ros2_vpn_error "Help takes no additional arguments."
                return 1
            fi
            _ros2_vpn_usage
            return 0
            ;;
        setup)
            if (( $# < 2 || $# > 3 )); then
                _ros2_vpn_error "setup requires REMOTE_IPV4 and an optional DOMAIN_ID."
                _ros2_vpn_usage
                return 1
            fi
            remote_ip=$2
            domain=${3-42}
            if ! _ros2_vpn_ipv4 "$remote_ip"; then
                _ros2_vpn_error "REMOTE_IPV4 must be a unicast IPv4 address (not loopback)."
                return 1
            fi
            if [[ ! $domain =~ ^[0-9]{1,3}$ ]] || (( 10#$domain > 232 )); then
                _ros2_vpn_error "DOMAIN_ID must be a decimal integer from 0 to 232."
                return 1
            fi
            domain=$((10#$domain))
            ;;
        status|reset)
            if (( $# != 1 )); then
                _ros2_vpn_error "$mode takes no additional arguments."
                return 1
            fi
            ;;
        *)
            _ros2_vpn_error "Missing or unknown mode: $mode"
            _ros2_vpn_usage
            return 1
            ;;
    esac
    if [[ $mode != status && ${BASH_SOURCE[0]} == "$0" ]]; then
        _ros2_vpn_error "Use 'source ${BASH_SOURCE[0]} $mode ...' to change this shell."
        return 1
    fi
    root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P) || {
        _ros2_vpn_error "Cannot resolve repository root."
        return 1
    }
    config="$(pwd)/cyclonedds_config.xml"
    overlay="$(pwd)/install/setup.bash"

    case $mode in
        status) _ros2_vpn_status "$config"; return $? ;;
        reset)
            if ! command -v ros2 > /dev/null; then
                _ros2_vpn_error "ROS 2 not found; cannot stop daemon. Clearing environment anyway."
                result=1
            elif ! ros2 daemon stop; then
                _ros2_vpn_error "Cannot stop ROS 2 daemon. Clearing environment anyway."
                result=1
            fi
            unset RMW_IMPLEMENTATION CYCLONEDDS_URI ROS_DOMAIN_ID \
                ROS_STATIC_PEERS ROS_AUTOMATIC_DISCOVERY_RANGE
            printf '[INFO] ROS 2 environment cleared; XML preserved.\n'
            return "$result"
            ;;
    esac

    for token in ip ping; do
        if ! command -v "$token" > /dev/null; then
            _ros2_vpn_error "Required command not found: $token"
            return 1
        fi
    done
    if ! route=$(ip -4 route get "$remote_ip"); then
        _ros2_vpn_error "Cannot find an IPv4 route to $remote_ip"
        return 1
    fi
    read -r -a fields <<< "$route"
    case ${fields[0]-} in
        unreachable|blackhole|prohibit|local|broadcast|multicast)
            _ros2_vpn_error "Route is not usable for a remote unicast peer: $route"
            return 1
            ;;
    esac
    for (( result=0; result < ${#fields[@]} - 1; result++ )); do
        case ${fields[result]} in
            dev) interface=${fields[result+1]} ;;
            src) local_ip=${fields[result+1]} ;;
        esac
    done
    if [[ -z $interface ]] || ! _ros2_vpn_ipv4 "$local_ip"; then
        _ros2_vpn_error "Route must contain a usable dev and src: $route"
        return 1
    fi
    if ! addresses=$(ip -o -4 addr show dev "$interface"); then
        _ros2_vpn_error "Cannot inspect interface: $interface"
        return 1
    fi
    while read -r -a fields; do
        for (( result=0; result < ${#fields[@]} - 1; result++ )); do
            if [[ ${fields[result]} == inet ]]; then
                address=${fields[result+1]%/*}
                [[ $address != "$local_ip" ]] || found=1
            fi
        done
    done <<< "$addresses"
    if (( ! found )) || [[ $local_ip == "$remote_ip" ]]; then
        _ros2_vpn_error "Route source must belong to $interface and differ from the remote IP."
        return 1
    fi
    printf '[INFO] Route to %s: interface=%s source=%s\n' "$remote_ip" "$interface" "$local_ip"
    if ! ping -n -c 1 -W 2 -I "$local_ip" "$remote_ip"; then
        _ros2_vpn_error "Remote peer did not answer ping; check VPN, routing and ICMP permissions."
        return 1
    fi
    if [[ ! -r $overlay ]]; then
        _ros2_vpn_error "ROS 2 overlay not found/readable: $overlay. Build the workspace first."
        return 1
    fi
    if ! source "$overlay"; then
        _ros2_vpn_error "Cannot source ROS 2 overlay: $overlay"
        return 1
    fi
    if ! command -v ros2 > /dev/null; then
        _ros2_vpn_error "ROS 2 not found. Source your ROS 2 installation first."
        return 1
    fi
    if ! ros2 pkg prefix rmw_cyclonedds_cpp > /dev/null; then
        _ros2_vpn_error "CycloneDDS not found in the loaded ROS environment."
        printf 'Install ros-%s-rmw-cyclonedds-cpp and source ROS 2.\n' "${ROS_DISTRO:-jazzy}" >&2
        return 1
    fi
    _ros2_vpn_write_config "$config" "$interface" "$local_ip" "$remote_ip" || return 1
    # The overlay may change ROS_DOMAIN_ID; stop the daemon of the old shell.
    if ! ROS_DOMAIN_ID="$previous_domain" ros2 daemon stop; then
        _ros2_vpn_error "Cannot stop the previous ROS 2 daemon."
        return 1
    fi
    export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
    export CYCLONEDDS_URI="file://$config"
    export ROS_DOMAIN_ID="$domain"
    export ROS_STATIC_PEERS="${local_ip};${remote_ip}"
    unset ROS_AUTOMATIC_DISCOVERY_RANGE
    if ! ros2 daemon start; then
        _ros2_vpn_error "Cannot start ROS 2 daemon; settings are exported but setup is incomplete."
        return 1
    fi
    printf '[INFO] ROS 2 unicast setup complete: domain=%s interface=%s local=%s remote=%s\n' \
        "$domain" "$interface" "$local_ip" "$remote_ip"
}

if _ros2_vpn_main "$@"; then
    return 0 2>/dev/null || exit 0
else
    return 1 2>/dev/null || exit 1
fi
