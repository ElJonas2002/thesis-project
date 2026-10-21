# Thesis Project Initial Setup for Windows

## Table of Contents
1. [Windows Subsystem for Linux (WSL) Setup](#windows-subsystem-for-linux-wsl-setup)
2. [Enabling Mirrored Networking](#enabling-mirrored-networking-mode)
3. [ROS2 Communication Over VPN](#ros2-communication-over-vpn-for-vpn-users-only)
4. [Attach USB Camera Port to WSL](#attach-usb-camera-port-to-wsl)

## Windows Subsystem for Linux (WSL) Setup

### What is WSL?
WSL is a Windows feature that allows user to run GNU/Linux environments directly on a Windows machine. It mainly allows the user to:
- Choose between Windows and Linux systems without restarting or partition the disk.
- Execute Linux CLI-only tools, applications and utilities.
- Access to Windows files from Linux.

### Install WSL
To install WSL with Ubuntu 24.04, open an Administrator PowerShell terminal and run the following command:
```powershell
wsl --install -d Ubuntu-24.04
```

Once the installation have finished, **restart** your PC to apply the system features.

### Linux Ubuntu User Account Configuration
1. After your PC restarts, the Linux Ubuntu terminal should open automatically. If it does not, search for Ubuntu in your Windows Start menu and launch it.
2. Wait a minute while the initial setup is complete.
3. The system will prompt you to enter a **new UNIX username and password** to create your user profile.
4. When your user profile is loaded, update your environment packages.
    ```bash
    sudo apt update && sudo apt upgrade -y
    ```
5. Follow the steps to install de [RealSense ROS Wrapper for Ubuntu](https://github.com/realsenseai/realsense-ros#installation-on-ubuntu) into your WSL Linux environment.

## Enabling Mirrored Networking Mode
By default, WSL holds an old NAT (Network Address Translation) that blocks communication with outside interfaces. By enabling Morrored Networking Mode, your Windows host network interfaces will be reflected directly into your Linux environment.

To enable it, open a new PowerShell terminal and run the following command to create the `.wslconfig` file into the respective path:
```powershell
notepad "$env:USERPROFILE\.wslconfig"
```

The system will ask you if you want to edit the file with your text editor program, prompt Yes and write the following lines and save:
```txt
[wsl2]
networkingMode=mirrored
```

Then, in PowerShell, run the following commands:
```powershell
wsl --shutdown
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
```

The second line enables incoming traffic to WSL, since Hyper-V firewall blocks it by default when mirrored networking is enabled.

## ROS2 Communication to a Server Over VPN
If you are a VPN user (personal or corporative) and you are working with a remote server for models inference, this is the most **critical part** of the setup.

**Problem**: VPN tunnels (private and encrypted connections that allow data to travel safely between a device and a server through a public network) **do not route multicast traffic**. Since ROS2 uses Data Distribution Service (DDS) as its communication middleware, which relies on **multicast UDP** to automatically discover nodes in the network, DDS may bind to the wrong network interface (commonly LAN instead of VPN), preventing nodes to find each other and making ROS2 communication impossible.

**Solution**: Use CycloneDDS with multicast disabled and explicit local/remote unicast peers.

### 1. Configure both hosts automatically

Use the repository's [VPN setup script](../scripts/ros2_vpn_setup.sh) on **both machines**. It discovers the local IPv4 address and interface from the system route to the remote IP; no addresses or interface names need to be edited in the script.

Prerequisites: Bash, `ip` (iproute2), `ping` (iputils-ping), a sourced ROS 2 installation and a built workspace containing `install/setup.bash`. Install the CycloneDDS RMW if needed:

```bash
sudo apt install ros-jazzy-rmw-cyclonedds-cpp
```

After connecting the VPN, run these commands from the repository root, replacing the placeholders (including angle brackets):

```bash
# On host A: supply host B's reachable IPv4.
source scripts/ros2_vpn_setup.sh setup <HOST_B_IPV4>

# On host B: supply host A's reachable IPv4.
source scripts/ros2_vpn_setup.sh setup <HOST_A_IPV4>
```

The remote IP is mandatory and has **no default**. `ROS_DOMAIN_ID` defaults to **42** on each setup, even if the shell previously used another domain. To choose a different domain, supply a third argument on both hosts:

```bash
source scripts/ros2_vpn_setup.sh setup <REMOTE_IPV4> 7
source scripts/ros2_vpn_setup.sh status
source scripts/ros2_vpn_setup.sh reset
bash scripts/ros2_vpn_setup.sh --help
```

Domain IDs must be decimal integers from 0 to 232. On Linux, prefer 0-101 to avoid overlap with ephemeral UDP ports; DDS participant counts and other domains can also cause port conflicts. Both hosts must use the same domain.

- Use **`source`** for setup/reset so the current shell inherits the changes; executing them with `bash` is rejected.
- Setup selects the route's `dev` and `src`, verifies the IP belongs to that interface, binds CycloneDDS to both that interface and address, and requires a successful ping from that source IP. Missing routes or blocked ICMP cause a nonzero return, even if UDP might otherwise work. A route and ping do **not** prove VPN use or DDS connectivity: verify the selected interface and source in the output.
- The script loads the repository's `install/setup.bash`, writes `cyclonedds_config.xml` atomically in the repository root, exports the ROS/DDS settings, then starts the daemon after stopping the previous domain's daemon. Both paths are independent of the current directory; use an absolute script path when outside the repository.
- `status` requires neither ROS 2 nor network access. It shows current settings and the XML referenced by a local-file `CYCLONEDDS_URI`; without an active URI it labels the repository XML as inactive.
- `reset` takes no IP/domain, stops the current domain's daemon and clears the ROS/DDS variables, but **keeps the XML**. If ROS 2 is unavailable or daemon shutdown fails, it still clears the environment and returns an error.
- Other failures return nonzero with diagnostics rather than closing the sourced shell. As with any sourced command, a caller's own `set -e` policy can exit on a nonzero return. Daemon startup failure leaves exported settings in place and explicitly reports incomplete setup.
- Setup/reset affect only the current terminal and future child processes. Restart existing nodes and source setup in each terminal used for ROS 2.
- Generated XML stays local and is ignored by Git. Never put particular host IPs into the script or committed examples.

### 2. Verify discovery and data in both directions

After setup in each terminal, run a talker on host A and a listener on host B:

```bash
# Host A
ros2 run demo_nodes_cpp talker

# Host B
ros2 run demo_nodes_cpp listener
```

Check that the listener receives messages, then reverse the host roles. In another configured terminal, inspect `ros2 node list`, `ros2 topic list` and `ros2 topic info /chatter`. Install the ROS demo packages if absent. Ping alone is not an acceptance test: VPN routing and firewall rules must allow bidirectional DDS UDP discovery **and** user data. This live test requires access to both machines.

### Manual diagnostics (not additional setup steps)

To inspect the addresses and the route selected by Linux:

```bash
ip addr show
ip -4 route get <REMOTE_IPV4>
ping -n -c 1 -W 2 -I <LOCAL_IPV4> <REMOTE_IPV4>
```

The generated XML has the following structure. The placeholders below are illustrative; do not copy them literally as XML:

```xml
<?xml version="1.0" encoding="UTF-8" ?>
<CycloneDDS>
  <Domain>
    <General>
      <Interfaces>
        <NetworkInterface name="<NETWORK_INTERFACE>" address="<LOCAL_VPN_IP>" priority="default" multicast="false"/>
      </Interfaces>
      <AllowMulticast>false</AllowMulticast>
      <EnableMulticastLoopback>false</EnableMulticastLoopback>
    </General>
    <Discovery>
      <ParticipantIndex>auto</ParticipantIndex>
      <MaxAutoParticipantIndex>50</MaxAutoParticipantIndex>
      <Peers>
        <Peer address="<REMOTE_VPN_IP>"/>
        <Peer address="<LOCAL_VPN_IP>"/>
      </Peers>
    </Discovery>
  </Domain>
</CycloneDDS>
```

For manual diagnosis, these are the settings applied by setup (the script also loads the overlay and restarts the daemon):

```bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file:///path/to/cyclonedds_config.xml
export ROS_DOMAIN_ID=42   # Both must match!
export ROS_STATIC_PEERS="<LOCAL_IP>;<REMOTE_IP>"
unset ROS_AUTOMATIC_DISCOVERY_RANGE
```

## Attach USB Camera Port to WSL

Since USB ports are not shared with the WSL session, you must use `usbipd` to attach the decive to your WSL session. 

1. Download the latest MSI Installer (`.msi`) from the [dorssel/usbipd-win GitHub Releases](https://github.com/dorssel/usbipd-win/releases/tag/v5.3.0) page and run the installer.

2. Once the installation have finished, connect the RealSense camera and open new PowerShell terminal to run the following commands:
    ```powershell
    usbipd list                             # Check for the BUSID for the RealSense and copy it
    usbipd bind --busid <BUSID> -f          # Forces to bind device so it can be shared with WSL
    usbipd attach --wsl --busid <BUSID>     # Attach the device to WSL
    ```

3. Open a new Administrator PowerShell terminal and start a new WSL session:
    ```powershell
    wsl --cd ~
    ```

4. Verify that the RealSense appears in WSL:
    ```bash
    lsusb
    ```

5. Once the ROS package is built, launch the RealSense node. You should see the log that the RealSense Node is ready.
    ```bash
    ros2 launch realsense2_camera rs_launch.py
    ```

***Note***: *Everytime you start a new session, connect your camera to another port, or reconnect it to the same port you must check if the device is **Attached**. If not (Shared or Not Attached), RealSense will log that it could not find a device, and you must attach it again and relaunch the node.*
