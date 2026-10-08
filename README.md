# 🤖️ Master of Sciences in Robotics and AI Thesis Project

> **Superquadrics Module Version: 1.1**

## 📋️ Table of Contents

1. [Prerequisites](#️-prerequisites)
1. [Initial Setup](#️-initial-setup)
2. [Quick Start](#️-quick-start)
3. [Project Structure](#️-project-structure)
3. [Resources](#️-resources)

## 🖥️ Prerequisites
- **Python venv version**: 3.12
- **Ubuntu version**: 24.04
- **ROS2 version**: Jazzy

## 🛠️ Initial Setup


1. Activate your virtual environment and install the required dependencies using the `requirements.txt` file:
    ```sh
    pip install -r requirements.txt
    ```

2. Follow the steps to install the [ROS Wrapper for RealSense on Ubuntu](https://github.com/realsenseai/realsense-ros#installation-on-ubuntu).
3. Clone the [SuperDec](https://github.com/elisabettafedele/superdec.git) repository into your root project path and follow the instructions for a Quick Start and Download Pre-trained Models.
4. Replace the content of [`backend.py`](superdec/superdec/functional/backend.py) in SuperDec repo with the following lines. The reason is that `torch >= 2.14` compiles ATen headers as C++20, which the system `CUDA 12.0 nvcc` cannot do. The script below selects the pip CUDA toolkit *before* importing `cpp_extension.load`, since the latter resolves `CUDA_HOME` once at import time, and its version must match torch's cuda runtime.
    ```python
    import os
    import sysconfig

    _cuda_home = os.path.join(sysconfig.get_paths()['purelib'], 'nvidia', 'cu13')
    if os.path.isdir(_cuda_home):
        os.environ['CUDA_HOME'] = _cuda_home
        os.environ['PATH'] = os.path.join(_cuda_home, 'bin') + os.pathsep + os.environ.get('PATH', '')
    os.environ.setdefault('CXX', 'g++-13')

    from torch.utils.cpp_extension import load

    _src_path = os.path.dirname(os.path.abspath(__file__))
    _backend = load(name='_pvcnn_backend',
                    extra_cflags=['-O3', '-std=c++20'],
                    extra_cuda_cflags=['-std=c++20'],
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
1. Launch the ROS Wrapper for RealSense with the following arguments:
    - `align_depth`: Forces to publish RGB raw image with its corresponding depth image synchronously (depth image will be published in         `camera/camera/aligned_depth_to_color/image_raw` topic).
    - `spatial_filter`: Preserves edges and object features while smoothing the image to reduce noise without blurring it.  
    - `temporal_filter`: Reduces depth image noise by combining secuential frames with Exponential Mobile Average (EMA).
    - `decimation_filter`: Reduces depth image resolution and scene complexity to save data bandwidth and lower host CPU load.

    ```sh
    ros2 launch realsense2_camera rs_launch.py align_depth.enable:=true decimation_filter.enable:=true spatial_filter.enable:=true temporal_filter.enable:=true
    ```

2. Open a second terminal and launch the superquadrics generator with the following command (*this launch file is intended to be a headless version; use the `show_window` argument to see an OpenCV debug window*):
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
    - Press `c` to segment all objects in the scene.
    - Press `q` to exit.
  5. To visualize binary segmentation mask, supequadrics and point clouds, open RViz2 and load the [RViz2 Visualization file](visualization/sq_visualization.rviz).
## 🪾️ Project Structure
```
.
├── doc                         # Project documentation files
│   └── metrics_sq              # Superquadrics Module Metrics 
├── models                      # DL Models (will be created after first-time run)
├── refs                        # Bib documents
├── scripts                     # Scripts for other tasks
├── src                         # ROS2 packages
│   └── intel_realsense         # Superquadrics Module package
│       ├── intel_realsense
│       └── launch
└── visualization               # Visualization files
```

## 🧪️ Resources
- [Superquadrics Generator Metrics](doc/metrics_sq)
- [Project Logbook](doc/PROJECT_LOGBOOK.md)
