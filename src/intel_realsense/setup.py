import os
from glob import glob
from setuptools import find_packages, setup

package_name = 'intel_realsense'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob(os.path.join('launch', '*launch.[pxy][yma]*')))
    ],
    install_requires=['setuptools', 'numpy', 'opencv-python'],
    zip_safe=True,
    # colcon runs under the system interpreter, so the generated console scripts would be
    # pinned to it; `env python3` follows whichever venv is active instead.
    options={'build_scripts': {'executable': '/usr/bin/env python3'}},
    maintainer='Jonathan Piña',
    maintainer_email='jonas.orlaineta02@gmail.com',
    description='This package provides initialization and handling for Intel RealSense cameras in ROS 2.',
    license='Apache-2.0',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    entry_points={
        'console_scripts': [
            #'irs_node = intel_realsense.depth_ransac_bgrem:main',
            'superdec_node = intel_realsense.superdec_node:main',
            'fastsam_node = intel_realsense.fastsam_bgrem:main',
        ],
    },
)
