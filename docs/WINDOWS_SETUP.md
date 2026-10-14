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

**Solution**: Replacing multicast discovery with explicit unicast communication through ROS2 static peers.

> **Important**: *The following steps are integrated into `ros2_vpn_setup.sh` file. **Save and adjust this script on both machines** so it will set all environment variables and configurations automatically.*
>
>```bash
>source ros2_vpn_setup.sh auto
>```

### 1. Identify your VPN Network Interface and IPs
The VPN IPs are **not** the ones that appears on your Network & internet settings information (if the server has no connection to a VPN, get its main network interface and IP).
```bash
# On both machines
ip addr show

# Try bilateral communication
ping <remote_vpn_ip>    # On WSL
ping <local_vpn_ip>     # On server
```

### 2. Create CycloneDDS configurations file
CycloneDDS is a DDS implementation that allows an easier unicast configuration for small multi-node networks.

First, create CycloneDDS configurations file on **both** proyect root path and the RealSense workspace
```bash
(project_dir)$ nano cyclonedds_config.xml
```

Then paste the following XML and adjust the variables depending on your case:
```xml
<?xml version="1.0" encoding="UTF-8" ?>
<CycloneDDS>
  <Domain>
    <General>
      <Interfaces>
        <!--NETWORK_INTERFACE must match with gotten one from ip addr show for that machine-->
        <NetworkInterface name="<NETWORK_INTERFACE>" priority="default" multicast="false"/>
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

### Setup environment variables

If you do not have CycloneDDS for ROS2 implementation, install it with the following command:
```bash
sudo apt install ros-jazzy-rmw-cyclonedds-cpp
```

Then export the ROS environment variables in **both** systems
```bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file:///path/to/cyclonedds_config.xml
export ROS_DOMAIN_ID=42   # Both must match!
export ROS_STATIC_PEERS="<REMOTE_IP>,<LOCAL_IP>"
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
