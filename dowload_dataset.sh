#!/bin/bash

ACCOUNT=$1
DATASET=$2
VERSION=$3

if [ -z "$ACCOUNT" ]; then
  echo "Usage: $0 <account> <dataset_name> <version>"
  exit 1
fi

if [ -z "$DATASET" ]; then
  echo "Usage: $0 <account> <dataset_name> <version>"
  exit 1
fi

if [ -z "$VERSION" ]; then
  echo "Usage: $0 <account> <dataset_name> <version>"
  exit 1
fi

CLOUDSDK_AUTH_DISABLE_CREDENTIALS=True 
gcloud storage rsync -r   gs://gresearch/robotics/$DATASET/$VERSION datasets/$DATASET/$VERSION --account="$ACCOUNT"