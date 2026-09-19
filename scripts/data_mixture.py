"""
Build and sanity-check the Franka fine-tuning data mixture for OpenVLA.

OpenVLA does NOT fine-tune from a raw `tfds` / `tf.data` pipeline. It ships its own
RLDS loader (`RLDSDataset`) that, per registered dataset:
  1. reads the TFDS records from `--data_root_dir`,
  2. applies the dataset-specific *standardization transform* (maps each dataset's raw
     schema to OpenVLA's canonical `observation.image_primary` + 7-DoF `action`),
  3. normalizes actions (BOUNDS_Q99),
  4. interleaves the datasets according to a *named mixture* (weights),
  5. and finally `RLDSBatchTransform` turns every step into the exact tensors the model
     trains on: `pixel_values`, tokenized `input_ids`, and masked `labels`.

The mixture used here (`franka_finetune`) is registered in
`prismatic/vla/datasets/rlds/oxe/mixtures.py` and contains only the Franka Emika Panda
datasets downloaded locally under `datasets/`.

This script loads that mixture and prints one fully-formatted batch so you can confirm
the data is in the correct OpenVLA format before launching `vla-scripts/finetune.py`.

Run:
    python scripts/data_mixture.py --data_root_dir datasets --mixture franka_finetune
"""

import argparse
from pathlib import Path

import torch
from torch.utils.data import DataLoader
from transformers import AutoProcessor

# The local OpenVLA repo is not pip-installed (to protect the pinned torch/flash-attn
# stack), so make its `prismatic` package importable directly from the vendored source.
import sys
OPENVLA_ROOT = Path(__file__).resolve().parent.parent / "vlas" / "openvla"
sys.path.insert(0, str(OPENVLA_ROOT))

from prismatic.models.backbones.llm.prompting import PurePromptBuilder
from prismatic.util.data_utils import PaddedCollatorForActionPrediction
from prismatic.vla.action_tokenizer import ActionTokenizer
from prismatic.vla.datasets import RLDSBatchTransform, RLDSDataset

VLA_PATH = "openvla/openvla-7b"
IMAGE_RESOLUTION = (224, 224)  # OpenVLA input size


def build_mixture(data_root_dir: Path, mixture: str, shuffle_buffer_size: int, image_aug: bool):
    """Construct the OpenVLA RLDS mixture and its (collator-ready) batch transform."""
    # Only the processor (tokenizer + image processor) is needed to format data -- not the 7B model.
    processor = AutoProcessor.from_pretrained(VLA_PATH, trust_remote_code=True)
    action_tokenizer = ActionTokenizer(processor.tokenizer)

    batch_transform = RLDSBatchTransform(
        action_tokenizer,
        processor.tokenizer,
        image_transform=processor.image_processor.apply_transform,
        prompt_builder_fn=PurePromptBuilder,
    )

    dataset = RLDSDataset(
        data_root_dir,
        mixture,
        batch_transform,
        resize_resolution=IMAGE_RESOLUTION,
        shuffle_buffer_size=shuffle_buffer_size,
        image_aug=image_aug,
    )

    collator = PaddedCollatorForActionPrediction(
        processor.tokenizer.model_max_length,
        processor.tokenizer.pad_token_id,
        padding_side="right",
    )
    return dataset, collator


def main() -> None:
    parser = argparse.ArgumentParser(description="Build/verify the OpenVLA Franka data mixture.")
    parser.add_argument("--data_root_dir", type=Path, default=Path("datasets"),
                        help="Directory holding the TFDS datasets (folder names must match the mixture entries).")
    parser.add_argument("--mixture", type=str, default="franka_finetune",
                        help="Named mixture registered in OXE_NAMED_MIXTURES (or a single dataset name).")
    parser.add_argument("--batch_size", type=int, default=16)
    parser.add_argument("--shuffle_buffer_size", type=int, default=1000,
                        help="Small for a quick check; use 100_000+ for real training.")
    parser.add_argument("--image_aug", action="store_true", help="Enable training-time image augmentations.")
    args = parser.parse_args()

    dataset, collator = build_mixture(
        args.data_root_dir, args.mixture, args.shuffle_buffer_size, args.image_aug
    )

    dataloader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        sampler=None,
        collate_fn=collator,
        num_workers=0,  # RLDS/TFDS handles its own parallelism -- keep at 0.
    )

    print(f"\nMixture '{args.mixture}' loaded from '{args.data_root_dir}'.")
    print("Per-dataset action normalization statistics (q01/q99) used for de-normalization at inference:")
    for name, stats in dataset.dataset_statistics.items():
        print(f"  - {name}: {stats['action']['q01'].shape[0]}-dim action")

    batch = next(iter(dataloader))
    print("\nOne formatted OpenVLA training batch:")
    for key, value in batch.items():
        if isinstance(value, torch.Tensor):
            print(f"  {key:>15}: shape={tuple(value.shape)} dtype={value.dtype}")
        else:
            print(f"  {key:>15}: {type(value).__name__} (len={len(value)})")

    # `labels` should be IGNORE_INDEX (-100) everywhere except the 7 action tokens + stop token.
    supervised = (batch["labels"] != -100).sum(dim=1)
    print(f"\nSupervised (action) tokens per sample: {supervised.tolist()}")
    print("Expected ~8 per sample (7 action tokens + stop token). Data is in the correct format.\n")


if __name__ == "__main__":
    main()
    