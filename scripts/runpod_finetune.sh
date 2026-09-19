#!/usr/bin/env bash
# =============================================================================
# RunPod bootstrap — OpenVLA + Franka LoRA fine-tuning (Option 1: Pod Volume Disk)
# =============================================================================
# Create the Pod like this (NO Network Volume):
#   - Template : "RunPod PyTorch 2.2" (Python 3.10 + CUDA 12.1)  <- flash-attn wheel needs this
#   - Volume Disk (the pod's own disk): ~150 GB, mounted at /workspace
#   - GPU      : A100 80GB / H100 (use_quantization False) or 24GB+ (set USE_QUANTIZATION=True)
#
# Then upload THIS repo (with the edited vlas/openvla containing the `franka_finetune`
# mixture) to /workspace/thesis-project and run:
#   export HF_TOKEN=hf_xxx                       # needed to pull openvla/openvla-7b AND to upload
#   export WANDB_API_KEY=xxx                     # optional; omit to disable logging
#   export HF_UPLOAD_REPO=your-user/openvla-franka   # optional; auto-pushes the model when training ends
#   bash /workspace/thesis-project/scripts/runpod_finetune.sh
#
# The Pod Volume Disk persists across stop/start and is billed only while the Pod
# exists; `terminate` deletes it. Set HF_UPLOAD_REPO so the model is pushed to the
# Hub automatically before you terminate (otherwise copy it off-pod manually).
# =============================================================================
set -euo pipefail

# ============================ CONFIG (edit me) ============================
WORKSPACE="${WORKSPACE:-/workspace}"
DATA_DIR="${DATA_DIR:-$WORKSPACE/datasets}"
RUN_DIR="${RUN_DIR:-$WORKSPACE/runs}"
ADAPTER_DIR="${ADAPTER_DIR:-$WORKSPACE/adapter-tmp}"

# Franka datasets to pull from the public Open-X GCS bucket (name:version).
# Comment out the two big ones (taco_play, iamlab) to train a ~17 GB subset.
DATASETS=(
  "cmu_franka_exploration_dataset_converted_externally_to_rlds:0.1.0"   # 0.6 GB
  "nyu_franka_play_dataset_converted_externally_to_rlds:0.1.0"          # 5.2 GB
  "viola:0.1.0"                                                          # 11 GB
  "taco_play:0.1.0"                                                      # 48 GB
  "iamlab_cmu_pickup_insert_converted_externally_to_rlds:0.1.0"         # 51 GB
)

MIXTURE="${MIXTURE:-franka_finetune}"
BATCH_SIZE="${BATCH_SIZE:-16}"
GRAD_ACCUM="${GRAD_ACCUM:-1}"
MAX_STEPS="${MAX_STEPS:-20000}"
SAVE_STEPS="${SAVE_STEPS:-2500}"
SHUFFLE_BUFFER="${SHUFFLE_BUFFER:-100000}"
LEARNING_RATE="${LEARNING_RATE:-5e-4}"
USE_QUANTIZATION="${USE_QUANTIZATION:-False}"   # big GPU -> False; set True for <24 GB VRAM
NPROC="${NPROC:-1}"                              # number of GPUs on the Pod
WANDB_PROJECT="${WANDB_PROJECT:-openvla-franka}"
WANDB_ENTITY="${WANDB_ENTITY:-}"                 # your W&B entity (leave empty if disabled)

# Auto-upload the fine-tuned model to HuggingFace Hub (leave HF_UPLOAD_REPO empty to skip).
HF_UPLOAD_REPO="${HF_UPLOAD_REPO:-}"             # e.g. "your-username/openvla-franka"
HF_UPLOAD_PRIVATE="${HF_UPLOAD_PRIVATE:-true}"   # create the repo as private
UPLOAD_MODE="${UPLOAD_MODE:-merged}"             # merged = self-contained model (~14GB) | adapter = LoRA only (small)
# =========================================================================

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENVLA_DIR="$REPO_ROOT/vlas/openvla"
MIXTURES_FILE="$OPENVLA_DIR/prismatic/vla/datasets/rlds/oxe/mixtures.py"
log() { echo -e "\n\033[1;36m[runpod]\033[0m $*"; }

# --- 1. Sanity checks ---------------------------------------------------------
log "GPU check:"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || { echo "No GPU visible!"; exit 1; }

PYVER="$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[ "$PYVER" = "3.10" ] || log "WARN: Python is $PYVER, but the flash-attn wheel in requirements.txt is cp310. Use a Python 3.10 template or swap the wheel."

grep -q "\"$MIXTURE\"" "$MIXTURES_FILE" \
  || { echo "ERROR: mixture '$MIXTURE' not found in $MIXTURES_FILE — did you upload the EDITED vlas/openvla?"; exit 1; }

# --- 2. Install gcloud (for the dataset download) -----------------------------
if ! command -v gcloud >/dev/null 2>&1; then
  log "Installing google-cloud-cli (non-interactive)..."
  curl -sSL https://sdk.cloud.google.com | bash -s -- --disable-prompts >/tmp/gcloud_install.log 2>&1
  source "$HOME/google-cloud-sdk/path.bash.inc"
fi

# --- 3. Download datasets to the Pod Volume Disk (skip if already present) -----
mkdir -p "$DATA_DIR"
for entry in "${DATASETS[@]}"; do
  name="${entry%%:*}"; ver="${entry##*:}"
  dest="$DATA_DIR/$name/$ver"
  if [ -d "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
    log "[$name] already present — skipping download."
  else
    log "[$name] downloading version $ver from gs://gresearch/robotics ..."
    mkdir -p "$dest"
    CLOUDSDK_AUTH_DISABLE_CREDENTIALS=True \
      gcloud storage rsync -r "gs://gresearch/robotics/$name/$ver" "$dest"
  fi
done
log "Datasets on disk:"; du -sh "$DATA_DIR"/*/ 2>/dev/null || true

# --- 4. Python environment (staged; dlimp gets --no-deps to protect the pins) --
log "Installing Python dependencies..."
grep -v '^dlimp' "$REPO_ROOT/requirements.txt" > /tmp/req.nodlimp.txt
pip install --no-cache-dir -r /tmp/req.nodlimp.txt
pip install --no-cache-dir --no-deps "dlimp @ git+https://github.com/moojink/dlimp_openvla"
pip install --no-cache-dir -e "$OPENVLA_DIR" --no-deps          # register the `prismatic` package
python -c "import prismatic, flash_attn, tensorflow_datasets, torch; print('[env] OK — torch', torch.__version__)"

# --- 5. Auth ------------------------------------------------------------------
if [ -n "${HF_TOKEN:-}" ]; then
  huggingface-cli login --token "$HF_TOKEN" --add-to-git-credential || true
else
  log "WARN: HF_TOKEN not set — the openvla/openvla-7b download may fail."
fi
if [ -n "${WANDB_API_KEY:-}" ]; then
  export WANDB_API_KEY
else
  export WANDB_MODE=disabled
  log "WANDB_API_KEY not set — Weights & Biases disabled."
fi

# --- 6. Launch fine-tuning ----------------------------------------------------
mkdir -p "$RUN_DIR" "$ADAPTER_DIR"
log "Launching fine-tuning on mixture '$MIXTURE' (quantization=$USE_QUANTIZATION, GPUs=$NPROC)..."
cd "$OPENVLA_DIR"
torchrun --standalone --nnodes 1 --nproc-per-node "$NPROC" vla-scripts/finetune.py \
  --vla_path "openvla/openvla-7b" \
  --data_root_dir "$DATA_DIR" \
  --dataset_name "$MIXTURE" \
  --run_root_dir "$RUN_DIR" \
  --adapter_tmp_dir "$ADAPTER_DIR" \
  --batch_size "$BATCH_SIZE" \
  --grad_accumulation_steps "$GRAD_ACCUM" \
  --max_steps "$MAX_STEPS" \
  --save_steps "$SAVE_STEPS" \
  --shuffle_buffer_size "$SHUFFLE_BUFFER" \
  --learning_rate "$LEARNING_RATE" \
  --use_lora True \
  --use_quantization "$USE_QUANTIZATION" \
  --wandb_project "$WANDB_PROJECT" \
  --wandb_entity "$WANDB_ENTITY"

log "Training done. Checkpoints + dataset_statistics.json are in $RUN_DIR."

# --- 7. Upload the fine-tuned model to HuggingFace Hub ------------------------
if [ -z "${HF_UPLOAD_REPO:-}" ]; then
  log "HF_UPLOAD_REPO not set — skipping upload. IMPORTANT: copy the model off-pod before you terminate!"
elif [ -z "${HF_TOKEN:-}" ]; then
  log "WARN: HF_UPLOAD_REPO set but HF_TOKEN missing — cannot upload. Copy the model off-pod manually!"
else
  MODEL_DIR="$(ls -dt "$RUN_DIR"/*/ 2>/dev/null | head -1)"   # newest exp_id run dir
  [ -n "$MODEL_DIR" ] || { echo "No run directory found under $RUN_DIR"; exit 1; }
  STATS="${MODEL_DIR%/}/dataset_statistics.json"
  priv_flag=""; [ "$HF_UPLOAD_PRIVATE" = "true" ] && priv_flag="--private"

  case "$UPLOAD_MODE" in
    merged)
      [ -f "$STATS" ] || log "WARN: dataset_statistics.json not found in $MODEL_DIR (needed to de-normalize actions)."
      log "Uploading merged model from $MODEL_DIR -> $HF_UPLOAD_REPO ..."
      huggingface-cli upload "$HF_UPLOAD_REPO" "$MODEL_DIR" . \
        --repo-type model $priv_flag --commit-message "OpenVLA Franka fine-tune (merged)"
      ;;
    adapter)
      up="/tmp/adapter_upload"; rm -rf "$up"; mkdir -p "$up"
      cp -r "$ADAPTER_DIR"/. "$up"/
      [ -f "$STATS" ] && cp "$STATS" "$up"/ || log "WARN: dataset_statistics.json not found — upload it manually."
      log "Uploading LoRA adapter (+dataset_statistics.json) -> $HF_UPLOAD_REPO ..."
      huggingface-cli upload "$HF_UPLOAD_REPO" "$up" . \
        --repo-type model $priv_flag --commit-message "OpenVLA Franka fine-tune (LoRA adapter)"
      ;;
    *) echo "Unknown UPLOAD_MODE '$UPLOAD_MODE' (use 'merged' or 'adapter')"; exit 1;;
  esac
  log "Upload complete: https://huggingface.co/$HF_UPLOAD_REPO — safe to terminate the Pod."
fi
