# 🤖️ Master of Sciences in Robotics and AI Thesis Project

> **Superquadrics Module Version: 1.1**

## 📋️ Table of Contents

1. [Prerequisites](#️-prerequisites)
1. [Initial Setup](#️-initial-setup)
2. [Quick Start](#️-quick-start)
3. [Project Structure](#️-project-structure)
3. [Resources](#️-resources)

## 🖥️ Prerequisites
- **Python environment version**: 3.12
- **Ubuntu version**: 24.04
- **ROS2 distro**: Jazzy

## 🛠️ Initial Setup

> ⚠️***Warning***: *We highly recommend to run this code in Ubuntu systems, since it is intended for them. If you want to run it on a Windows machine, review the [Windows setup guide](docs/WINDOWS_SETUP.md) for more details.*

1. Activate your virtual environment and install the required dependencies using the `requirements.txt` file:
    ```sh
    pip install -r requirements.txt
    ```

2. Follow the steps to install the [ROS Wrapper for RealSense on Ubuntu](https://github.com/realsenseai/realsense-ros#installation-on-ubuntu).

3. Clone the [SuperDec](https://github.com/elisabettafedele/superdec.git) repository into your root project path and follow the instructions for a Quick Start and Download Pre-trained Models.
4. Replace the content of [`backend.py`](superdec/superdec/functional/backend.py) in SuperDec repo with the following lines. SuperDec builds its PVCNN backend as a JIT C++/CUDA extension at import time, and three things must be fixed for that build to succeed on this setup:
    - **CUDA toolkit selection**: `torch >= 2.14` compiles ATen headers as C++20, which the system `CUDA 12.0 nvcc` cannot do. The script points `CUDA_HOME` and `PATH` to the pip CUDA toolkit (`nvidia/cu13`) *before* importing `cpp_extension.load`, since the latter resolves `CUDA_HOME` once at import time, and its version must match torch's CUDA runtime.
    - **Host compiler**: `nvcc` from CUDA 13 rejects the default GCC 14 of Ubuntu 24.04, so `CXX` is pinned to `g++-13` (install it with `sudo apt install g++-13` if missing). `setdefault` is used so an externally exported `CXX` still wins.
    - **`libcudart.so` symlink**: PyTorch always appends `-lcudart` to the link command, but the NVIDIA pip wheels only ship the versioned `libcudart.so.13`, so `ld` fails with `cannot find -lcudart`. The script creates an unversioned symlink inside a private `.cudart_link` directory and injects it through `extra_ldflags`; the `-L` flag lands *before* PyTorch's own `-lcudart` because `load()` appends its flags after the user-provided ones, and `-Wl,-rpath` lets the resulting `.so` locate the real runtime. If a full CUDA toolkit with the unversioned symlink is already present, `_extra_ldflags` stays empty and nothing changes.
    ```python
    import glob
    import os
    import sysconfig

    _cuda_home = os.path.join(sysconfig.get_paths()['purelib'], 'nvidia', 'cu13')
    if os.path.isdir(_cuda_home):
        os.environ['CUDA_HOME'] = _cuda_home
        os.environ['PATH'] = os.path.join(_cuda_home, 'bin') + os.pathsep + os.environ.get('PATH', '')
    os.environ.setdefault('CXX', 'g++-13')

    from torch.utils.cpp_extension import load

    _src_path = os.path.dirname(os.path.abspath(__file__))

    _extra_ldflags = []
    _cuda_lib = os.path.join(_cuda_home, 'lib')

    if os.path.isdir(_cuda_lib) and not os.path.exists(os.path.join(_cuda_lib, 'libcudart.so')):
        _versioned = sorted(glob.glob(os.path.join(_cuda_lib, 'libcudart.so.*')))
        if _versioned:
            _link_dir = os.path.join(_src_path, '.cudart_link')
            os.makedirs(_link_dir, exist_ok=True)
            _stub = os.path.join(_link_dir, 'libcudart.so')
            if not os.path.islink(_stub):
                os.symlink(_versioned[-1], _stub)
            _extra_ldflags = [f'-L{_link_dir}', f'-Wl,-rpath,{_cuda_lib}']

    _backend = load(name='_pvcnn_backend',
                    extra_cflags=['-O3', '-std=c++20'],
                    extra_cuda_cflags=['-std=c++20'],
                    extra_ldflags=_extra_ldflags,
                    sources=[os.path.join(_src_path,'src', f) for f in [
                        'voxelization/vox.cpp',
                        'voxelization/vox.cu',
                        'interpolate/trilinear_devox.cpp',
                        'interpolate/trilinear_devox.cu',
                        'bindings.cpp',
                    ]]
                    )

    __all__ = ['_backend']
    ```

5. Build the project:
    ```sh
    colcon build
    ```

## 🚀️ Quick Start

For multi-machine ROS 2 over a VPN, configure both hosts with the [automatic CycloneDDS setup guide](docs/WINDOWS_SETUP.md#1-configure-both-hosts-automatically). The script detects the local IPv4/interface from the route to the required remote IP; the domain defaults to 42.

1. Launch the ROS Wrapper for RealSense with the following arguments:
    - `align_depth`: Forces to publish RGB raw image with its corresponding depth image synchronously (depth image will be published in         `camera/camera/aligned_depth_to_color/image_raw` topic).
    - `spatial_filter`: Preserves edges and object features while smoothing the image to reduce noise without blurring it.  
    - `temporal_filter`: Reduces depth image noise by combining secuential frames with Exponential Mobile Average (EMA).
    - `decimation_filter`: Reduces depth image resolution and scene complexity to save data bandwidth and lower host CPU load.
    - `rgb_camera.color_profile`: Modifies RBG image resolution and FPS.
    - `depth_module.depth_profile`: Modifies Depth image resolution and FPS.

    ```sh
    . install/local_setup.bash
    ros2 launch realsense2_camera rs_launch.py align_depth.enable:=true \
        decimation_filter.enable:=true \
        spatial_filter.enable:=true \
        temporal_filter.enable:=true \
        rgb_camera.color_profile:=640,480,30 \
        depth_module.depth_profile:=640,480,30
    ```

    **Note**: *If the node warns a USB overflow multiple times, this means that bandwidth is too low to send RGB+D images at default resolution or FPS. Try reducing both RGB and Depth image resolution as follows*:

    ```bash
    ros2 launch realsense2_camera rs_launch.py align_depth.enable:=true \
        spatial_filter.enable:=true \
        temporal_filter.enable:=true \
        decimation_filter.enable:=true \
        rgb_camera.color_profile:=424x240x30 \
        depth_module.depth_profile:=424x240x30
    ```

2. Open a second terminal and launch the superquadrics generator with the following command (*this launch file is intended to be a headless version; use the `show_window` argument to see an OpenCV debug window if yout system allows it*):
    ```sh
    source install/setup.bash
    ros2 launch intel_realsense sq_launch.py show_window:=true
    ```
    > **💡️ Tip**: Use the `-s` short-handed argument to see all ROS arguments that this launch file can receive.

3. Open a third terminal and run the PromptCLI interface for FastSAM model:
    ```sh
    source install/setup.bash
    ros2 run intel_realsense prompt_cli
    ```
    - Press `t`  to write a **simple prompt** (*e.g. purple cube*) or **comma-separated simple prompts** (*e.g. orange cube, black drill, ...*) describing the object(s) you want the model to find. You are supposed to see the desired objects segmented with color masks in the OpenCV window.
    - Press `s` to save captures of the raw RGB+D images, RGB with background removed and segmentation masks.
    - Press `c` to segment all objects in the scene.
    - Press `q` to exit.
  5. To visualize binary segmentation mask, supequadrics and point clouds, open RViz2 and load the [RViz2 Visualization file](visualization/sq_visualization.rviz).
## 🪾️ Project Structure
```
.
├── .github                     # GitHub Copilot agents
├── doc                         # Project documentation files
│   └── metrics_sq              # Superquadrics Module Metrics
├── isaacsim                    # Related files for Isaac Sim simulation
├── models                      # DL Models (will be created after first-time run)
├── refs                        # Bib documents
├── scripts                     # Scripts for other tasks
├── src                         # ROS2 packages
│   └── intel_realsense         # Superquadrics Module package
│       ├── intel_realsense
│       └── launch
├── visualization               # Visualization filess
└── weights                     # CLIP weights file (created after the first processed prompt)

```

## 🧪️ Resources
- [Superquadrics Generator Metrics](docs/metrics_sq)
- [Project Logbook](docs/PROJECT_LOGBOOK.md)
- [Windows Initial Setup Guide](docs/WINDOWS_SETUP.md)
