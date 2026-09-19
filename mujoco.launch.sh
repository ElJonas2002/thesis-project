#!/bin/bash

cd ~
cd mujoco-3.12.0/bin

MODEL=$1


if [ -z "$MODEL" ]; then
  echo "Usage: $0 <model_name>"
  exit 1
elif [ "$MODEL" == "franka_emika" ]; then
    ./simulate ~/projects/thesis-project/robots/franka_emika_panda/scene.xml
elif [ "$MODEL" == "unitree_g1" ]; then
    ./simulate ~/projects/thesis-project/robots/unitree_g1/g1_29dof_lock_waist_rev_1_0.xml
else
  echo "Unknown model: $MODEL"
  echo "Available models: franka_emika, unitree_g1"
  exit 1
fi
