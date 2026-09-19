# MoSc Thesis Project
This repo contains all necessary files for MoSc thesis project.

## Initial configuration of `package.xml` file
Add the following lines

## Initial configuration of `setup.py` file
1. Import the following libraries:
```python
import os
from glob import glob
```

2. Add the following lines within the `data_files` array parameter of the `setup` function:
```python
data_files=[
    # default elements...

    # Link launch files to the install space so they can be found by ros2 launch
    (os.path.join('share', package_name, 'launch'),
        glob('launch/*.launch.py')),

    # Unitree G1 models (URDF/XML + meshes) used by the launch files
    (os.path.join('share', package_name, 'unitree_g1_models'),
        glob('unitree_g1_models/*.urdf') + glob('unitree_g1_models/*.xml')),
    (os.path.join('share', package_name, 'unitree_g1_models', 'meshes'),
        glob('unitree_g1_models/meshes/*')),
    (os.path.join('share', package_name, 'unitree_g1_models', 'inspire_hand'),
        glob('unitree_g1_models/inspire_hand/*')),
]
```
