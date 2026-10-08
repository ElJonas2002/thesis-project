"""
Training script for U²-Net using PyTorch.

Main commands
    - Train: python u2net_train.py --model (u2net, u2netp) --epochs epochs
"""
import argparse
import csv
import random
import time
from pathlib import Path
import albumentations as alb
import cv2
import numpy as np
import torch
import torch.nn as nn
from albumentations.pytorch import ToTensorV2
from torch.utils.data import DataLoader, Dataset, Subset
from tqdm import tqdm
from u2net import U2Net, U2NetP

# === 1. Define training losses ===
bce_loss = nn.BCEWithLogitsLoss()
sigmoid = torch.sigmoid

def iou_loss(pred, target, eps=1e-6):
    """
    Computes the Intersection over Union (IoU) loss between the predicted and target masks.

    Args:
        pred (torch.Tensor): Predicted mask.
        target (torch.Tensor): Ground truth mask.
        eps (float): Small value to avoid division by zero.

    Returns:
        torch.Tensor: IoU loss.

    """
    intersection = (pred * target).sum()
    union = pred.sum() + target.sum() - intersection
    iou = (intersection + eps) / (union + eps)
    return 1 - iou

def multi_bce_loss(logits, labels_v, verbose=False):
    """
    Computes the combined BCE and IoU loss for multiple side outputs of the U²-Net (all weights are set to 1.0).

    Args:
        logits [(torch.Tensor)]: Side logits (pre-sigmoid outputs) arrayof the U²-Net.
        labels_v (torch.Tensor): Ground truth mask.
        verbose (bool): Whether to print the per-branch breakdown.

    Returns:
        (torch.Tensor, torch.Tensor): Tuple containing the output (loss0) and total losses.
    """
    loss0, loss = None, 0.0

    for i, d in enumerate(logits):
        s = sigmoid(d)  # Probabilistic output for IoU computation
        l = bce_loss(d, labels_v) + iou_loss(s, labels_v)
        loss += l
        if i == 0:
            loss0 = l
        if verbose:
            print(f"  loss{i}: {l.data.item():.3f}")
    if verbose:
        print(f"Total loss: {loss.data.item():.3f}")

    return loss0, loss


# === 2. Define datasets and dataloader ===
DUTSTR_DATASET = 'DUTS-TR'
TO_DATASET = 'TO-vanilla'

# ImageNet statistics: the same normalisation used by the official U²-Net weights.
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)

# Anchored to this file, so the script runs from any working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = REPO_ROOT / 'datasets'


class SaliencyDataset(Dataset):
    """
    Reads (image, binary mask) pairs from two flat folders, matching them by file name.

    Works for DUTS-TR as-is, and for any table-top dataset once its per-object labels
    have been collapsed into a single foreground mask.

    Args:
        image_dir (Path | str): Folder holding the input images.
        mask_dir (Path | str): Folder holding the ground truth masks.
        transform (alb.Compose): Pipeline applied to image and mask together.
        image_ext (str): Extension of the image files.
        mask_ext (str): Extension of the mask files.
    """

    def __init__(self, image_dir, mask_dir, transform, image_ext='.jpg', mask_ext='.png',
                 instance_dir=None):
        self.image_paths = sorted(Path(image_dir).glob('*' + image_ext))
        if not self.image_paths:
            raise FileNotFoundError(f"No '*{image_ext}' files found in {image_dir}")

        # Same stem + mask folder + mask extension = the label of that image.
        self.mask_paths = [Path(mask_dir) / (p.stem + mask_ext) for p in self.image_paths]
        missing = [p for p in self.mask_paths if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"{len(missing)} masks are missing, e.g. {missing[0]}")

        # Per-object ids, only needed to measure how many objects the prediction recovers.
        self.instance_paths = None
        if instance_dir is not None:
            self.instance_paths = [Path(instance_dir) / (p.stem + mask_ext) for p in self.image_paths]

        self.transform = transform

    def __len__(self):
        return len(self.image_paths)

    def __getitem__(self, idx):
        image = cv2.imread(str(self.image_paths[idx]), cv2.IMREAD_COLOR)
        mask = cv2.imread(str(self.mask_paths[idx]), cv2.IMREAD_GRAYSCALE)
        if image is None or mask is None:
            raise RuntimeError(f"Could not decode pair {self.image_paths[idx].name}")

        # OpenCV loads BGR, but the ImageNet statistics assume RGB channel order.
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        # Grey levels -> hard {0, 1} targets, which is what BCE expects.
        mask = (mask > 127).astype(np.float32)

        if self.instance_paths is None:
            # A single call, so image and mask get the exact same geometric transform.
            augmented = self.transform(image=image, mask=mask)
            # ToTensorV2 returns the mask as (H, W); the network predicts (1, H, W).
            return augmented['image'], augmented['mask'].unsqueeze(0)

        instances = cv2.imread(str(self.instance_paths[idx]), cv2.IMREAD_GRAYSCALE)
        augmented = self.transform(image=image, mask=mask, instances=instances)
        return (augmented['image'], augmented['mask'].unsqueeze(0),
                augmented['instances'].unsqueeze(0))


def build_train_transform(resize=320, crop=288, strong=False):
    """
    Builds the training augmentation pipeline.

    The default reproduces the original paper (resize, random crop, horizontal flip).
    `strong` swaps it for the heavier set meant for the cluttered table-top stage.

    Args:
        resize (int): Side length the image is resized to before cropping.
        crop (int): Side length of the crop actually fed to the network.
        strong (bool): Whether to enable the aggressive augmentations.

    Returns:
        alb.Compose: The augmentation pipeline.
    """
    if strong:
        spatial = [
            alb.RandomResizedCrop(size=(crop, crop), scale=(0.6, 1.0), ratio=(0.75, 1.33)),
            alb.HorizontalFlip(p=0.5),
            alb.Affine(rotate=(-15, 15), translate_percent=(-0.05, 0.05), p=0.5),
        ]
        photometric = [
            alb.ColorJitter(brightness=0.3, contrast=0.3, saturation=0.3, hue=0.05, p=0.8),
            # Mild noise on purpose: the library default drowns the image.
            alb.GaussNoise(std_range=(0.02, 0.10), p=0.3),
            alb.MotionBlur(blur_limit=5, p=0.2),
        ]
    else:
        spatial = [
            alb.Resize(resize, resize),
            alb.RandomCrop(crop, crop),
            alb.HorizontalFlip(p=0.5),
        ]
        photometric = []

    # No vertical flip on purpose: objects always rest on top of the table.
    return alb.Compose(spatial + photometric + [
        alb.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD),
        ToTensorV2(),
    ])


def build_val_transform(size=288):
    """
    Builds the validation pipeline: no randomness, so the metric is comparable across epochs.

    Args:
        size (int): Side length the image is resized to.

    Returns:
        alb.Compose: The deterministic pipeline.
    """
    return alb.Compose([
        alb.Resize(size, size),
        alb.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD),
        ToTensorV2(),
    ], additional_targets={'instances': 'mask'})


def seed_worker(worker_id):
    """
    Reseeds each DataLoader worker; otherwise forked workers draw identical augmentations.

    Args:
        worker_id (int): Index of the worker, unused but required by the DataLoader API.
    """
    seed = torch.initial_seed() % 2 ** 32
    np.random.seed(seed)
    random.seed(seed)


def build_dataloaders(args):
    """
    Builds the training and validation loaders.

    With `--val-image-dir` the two splits are separate folders, which is what a scene level
    split needs. Otherwise a fraction of a single folder is held out: two dataset objects
    read that folder so validation images are never augmented, and the split happens on
    indices, which keeps both halves disjoint.

    Args:
        args (argparse.Namespace): Parsed command line configuration.

    Returns:
        (DataLoader, DataLoader): Training and validation loaders.
    """
    train_transform = build_train_transform(args.resize, args.crop, args.strong_aug)
    val_transform = build_val_transform(args.crop)

    if args.val_image_dir is not None:
        train_set = SaliencyDataset(args.image_dir, args.mask_dir, train_transform,
                                    args.image_ext, args.mask_ext)
        val_set = SaliencyDataset(args.val_image_dir, args.val_mask_dir, val_transform,
                                  args.image_ext, args.mask_ext, args.instance_dir)
    else:
        augmented = SaliencyDataset(args.image_dir, args.mask_dir, train_transform,
                                    args.image_ext, args.mask_ext)
        clean = SaliencyDataset(args.image_dir, args.mask_dir, val_transform,
                                args.image_ext, args.mask_ext, args.instance_dir)

        # Fixed seed so the same images land in validation on every run.
        generator = torch.Generator().manual_seed(args.seed)
        indices = torch.randperm(len(augmented), generator=generator).tolist()
        n_val = int(len(augmented) * args.val_fraction)

        train_set = Subset(augmented, indices[n_val:])
        val_set = Subset(clean, indices[:n_val])

    # drop_last avoids a final short batch, which destabilises the BatchNorm statistics.
    train_loader = DataLoader(train_set, batch_size=args.batch_size_train, shuffle=True,
                              num_workers=args.workers, pin_memory=True,
                              persistent_workers=args.workers > 0, drop_last=True,
                              worker_init_fn=seed_worker)
    val_loader = DataLoader(val_set, batch_size=args.batch_size_val, shuffle=False,
                            num_workers=args.workers, pin_memory=True,
                            persistent_workers=args.workers > 0,
                            worker_init_fn=seed_worker)
    return train_loader, val_loader


def save_batch_preview(images, masks, path):
    """
    Writes a PNG with every image next to a copy of itself tinted red by its mask.

    If the red areas do not follow the objects, image and mask were augmented differently.

    Args:
        images (torch.Tensor): Normalised batch of shape (B, 3, H, W).
        masks (torch.Tensor): Binary batch of shape (B, 1, H, W).
        path (Path | str): Destination PNG file.
    """
    mean = torch.tensor(IMAGENET_MEAN).view(1, 3, 1, 1)
    std = torch.tensor(IMAGENET_STD).view(1, 3, 1, 1)
    rgb = (images * std + mean).clamp(0, 1)

    overlay = rgb.clone()
    overlay[:, 0:1] = torch.maximum(overlay[:, 0:1], masks)

    side_by_side = torch.cat([rgb, overlay], dim=3)
    strip = torch.cat(list(side_by_side), dim=1)
    bgr = (strip.permute(1, 2, 0).numpy()[:, :, ::-1] * 255).astype(np.uint8)
    cv2.imwrite(str(path), bgr)


def parse_args(argv=None):
    """
    Declares every knob of the data pipeline, so nothing depends on the current directory.

    Args:
        argv (list[str] | None): Argument list, or None to read sys.argv.

    Returns:
        argparse.Namespace: Parsed configuration.
    """
    duts_dir = DATASETS_DIR / DUTSTR_DATASET
    parser = argparse.ArgumentParser(description='Train U²-Net for binary foreground segmentation.')
    parser.add_argument('--model', choices=('u2net', 'u2netp'), default='u2net')
    parser.add_argument('--image-dir', type=Path, default=duts_dir / (DUTSTR_DATASET + '-Image'))
    parser.add_argument('--mask-dir', type=Path, default=duts_dir / (DUTSTR_DATASET + '-Mask'))
    parser.add_argument('--val-image-dir', type=Path, default=None,
                        help='Separate validation images; needed for a scene level split')
    parser.add_argument('--val-mask-dir', type=Path, default=None)
    parser.add_argument('--instance-dir', type=Path, default=None,
                        help='Per-object id maps of the validation split, to measure object recall')
    parser.add_argument('--image-ext', default='.jpg')
    parser.add_argument('--mask-ext', default='.png')
    parser.add_argument('--resize', type=int, default=320)
    parser.add_argument('--crop', type=int, default=288)
    parser.add_argument('--strong-aug', action='store_true',
                        help='Heavier augmentations for the table-top fine-tuning stage')
    parser.add_argument('--batch-size-train', type=int, default=12)
    parser.add_argument('--batch-size-val', type=int, default=1)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--val-fraction', type=float, default=0.05)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--min-lr', type=float, default=1e-6)
    parser.add_argument('--weight-decay', type=float, default=1e-2)
    parser.add_argument('--warmup-epochs', type=float, default=1.0)
    parser.add_argument('--grad-clip', type=float, default=1.0)
    parser.add_argument('--amp', choices=('off', 'bf16', 'fp16'), default='bf16')
    parser.add_argument('--patience', type=int, default=15,
                        help='Epochs without validation IoU improvement before stopping')
    parser.add_argument('--max-steps', type=int, default=0,
                        help='Stop each epoch after N iterations (0 = full epoch), for quick checks')
    #parser.add_argument('--out-dir', type=Path, default=REPO_ROOT / 'runs' / 'u2net')
    parser.add_argument('--version', type=str, default='1',
                        help='Version of the model, used to create the output directory')
    parser.add_argument('--dry-run', action='store_true',
                        help='Only inspect one batch and exit, without training')
    parser.add_argument('--weights', type=Path, default=None,
                        help='Checkpoint written by this script, to fine-tune from (weights only)')
    parser.add_argument('--resume', type=Path, default=None,
                        help='Checkpoint to continue from, restoring optimizer, schedule and epoch')
    parser.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu')
    parser.add_argument('--preview', type=Path, default=REPO_ROOT / 'batch_preview.png')
    return parser.parse_args(argv)


def set_seed(seed):
    """
    Fixes every random source so two runs with the same seed are comparable.

    Args:
        seed (int): Seed shared by random, numpy and torch.
    """
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


# === 3. Load model ===
def build_model(args):
    """
    Creates the network and, optionally, restores the weights of a previous run.

    The checkpoint has to come from this same implementation: the official `u2net.pth`
    names its layers differently (stage1, stage1d, ...) and would need a key remapping.
    Convolutions keep their default Kaiming initialisation, as in the original code.

    Args:
        args (argparse.Namespace): Parsed command line configuration.

    Returns:
        (U2Net, torch.device): The model, already placed on the target device.
    """
    device = torch.device(args.device)
    model = U2Net() if args.model == 'u2net' else U2NetP()

    source = args.resume or args.weights
    if source is not None:
        checkpoint = torch.load(source, map_location='cpu')
        # Checkpoints of this script keep the weights under 'model'; raw state dicts also work.
        model.load_state_dict(checkpoint.get('model', checkpoint))
        print(f"weights restored from {source}")

    model.to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"U2Net on {device} | {n_params / 1e6:.1f}M parameters")
    return model, device


# === 4. Define optimizer and scheduler ===
def build_optimizer(model, args):
    """
    Builds AdamW, keeping biases and BatchNorm parameters out of the weight decay.

    Decaying BatchNorm scales and biases hurts convergence, so only convolution
    weights are regularised.

    Args:
        model (nn.Module): The network to optimise.
        args (argparse.Namespace): Parsed command line configuration.

    Returns:
        torch.optim.Optimizer: The configured optimizer.
    """
    decay, no_decay = [], []
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
        (no_decay if param.ndim <= 1 or name.endswith('.bias') else decay).append(param)

    groups = [{'params': decay, 'weight_decay': args.weight_decay},
              {'params': no_decay, 'weight_decay': 0.0}]
    return torch.optim.AdamW(groups, lr=args.lr, betas=(0.9, 0.999), eps=1e-8)


def build_scheduler(optimizer, args, steps_per_epoch):
    """
    Linear warmup followed by cosine annealing, both advanced once per iteration.

    Args:
        optimizer (torch.optim.Optimizer): Optimizer whose learning rate is scheduled.
        args (argparse.Namespace): Parsed command line configuration.
        steps_per_epoch (int): Number of iterations in one epoch.

    Returns:
        torch.optim.lr_scheduler.LRScheduler: The composed scheduler.
    """
    warmup_steps = max(1, int(args.warmup_epochs * steps_per_epoch))
    cosine_steps = max(1, args.epochs * steps_per_epoch - warmup_steps)

    warmup = torch.optim.lr_scheduler.LinearLR(optimizer, start_factor=0.01,
                                               total_iters=warmup_steps)
    cosine = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=cosine_steps,
                                                        eta_min=args.min_lr)
    return torch.optim.lr_scheduler.SequentialLR(optimizer, [warmup, cosine],
                                                 milestones=[warmup_steps])


# === 5. Training loop ===
def train_one_epoch(model, loader, optimizer, scheduler, scaler, device, args, epoch):
    """
    Runs one full pass over the training set.

    Args:
        model (nn.Module): The network being trained.
        loader (DataLoader): Training data.
        optimizer (torch.optim.Optimizer): Optimizer.
        scheduler (torch.optim.lr_scheduler.LRScheduler): Per-iteration learning rate schedule.
        scaler (torch.amp.GradScaler | None): Gradient scaler, only used with fp16.
        device (torch.device): Where the tensors live.
        args (argparse.Namespace): Parsed command line configuration.
        epoch (int): Current epoch, for the progress bar caption.

    Returns:
        (float, float): Mean total loss and mean fused-output loss over the epoch.
    """
    model.train()
    amp_dtype = {'bf16': torch.bfloat16, 'fp16': torch.float16}.get(args.amp)
    total, total0, n_steps = 0.0, 0.0, 0

    bar = tqdm(loader, desc=f"epoch {epoch}", leave=False)
    for images, masks in bar:
        images = images.to(device, non_blocking=True)
        masks = masks.to(device, non_blocking=True)

        optimizer.zero_grad(set_to_none=True)
        with torch.autocast(device.type, dtype=amp_dtype, enabled=amp_dtype is not None):
            loss0, loss = multi_bce_loss(model(images), masks)

        if scaler is not None:
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)  # Gradients must be unscaled before clipping.
            nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
            optimizer.step()
        scheduler.step()

        total += loss.item()
        total0 += loss0.item()
        n_steps += 1
        bar.set_postfix(loss=f"{loss.item():.3f}", lr=f"{scheduler.get_last_lr()[0]:.2e}")

        if args.max_steps and n_steps >= args.max_steps:
            break

    return total / n_steps, total0 / n_steps


def count_recovered_objects(preds, instances, coverage=0.5):
    """
    Count how many annotated objects the prediction actually found.

    The network outputs a single blob covering every object, so per-object IoU would be
    meaningless; what matters is whether each object is covered by the prediction.

    Args:
        preds (torch.Tensor): Binary predictions of shape (B, 1, H, W).
        instances (torch.Tensor): Per-object ids of shape (B, 1, H, W), 0 being background.
        coverage (float): Fraction of an object that must be predicted to count as found.

    Returns:
        (int, int): Objects recovered and objects present.
    """
    found = total = 0
    for pred, ids in zip(preds.bool(), instances):
        for label in torch.unique(ids):
            if label == 0:
                continue
            target = ids == label
            total += 1
            found += int((pred & target).sum() >= coverage * target.sum())
    return found, total


@torch.no_grad()
def evaluate(model, loader, device, threshold=0.5):
    """
    Measures segmentation quality on the validation split using the fused output.

    IoU and Dice are averaged per image, so small objects weigh the same as large ones.
    When the loader also yields per-object ids, the recall over objects is reported too.

    Args:
        model (nn.Module): The network being evaluated.
        loader (DataLoader): Validation data.
        device (torch.device): Where the tensors live.
        threshold (float): Probability above which a pixel counts as foreground.

    Returns:
        dict: Mean IoU, Dice, MAE and, when available, object recall over the split.
    """
    model.eval()
    iou_sum, dice_sum, mae_sum, n_images = 0.0, 0.0, 0.0, 0
    objects_found, objects_total = 0, 0

    for batch in tqdm(loader, desc='val', leave=False):
        images, masks = batch[0].to(device, non_blocking=True), batch[1].to(device, non_blocking=True)

        probs = sigmoid(model(images)[0])
        preds = (probs > threshold).float()

        if len(batch) > 2:
            found, total = count_recovered_objects(preds, batch[2].to(device))
            objects_found += found
            objects_total += total

        dims = (1, 2, 3)
        intersection = (preds * masks).sum(dims)
        union = preds.sum(dims) + masks.sum(dims) - intersection

        iou_sum += ((intersection + 1e-6) / (union + 1e-6)).sum().item()
        dice_sum += ((2 * intersection + 1e-6) / (preds.sum(dims) + masks.sum(dims) + 1e-6)).sum().item()
        mae_sum += (probs - masks).abs().mean(dims).sum().item()
        n_images += images.size(0)

    metrics = {'iou': iou_sum / n_images, 'dice': dice_sum / n_images, 'mae': mae_sum / n_images}
    metrics['obj_recall'] = objects_found / objects_total if objects_total else float('nan')
    return metrics


def save_checkpoint(path, model, epoch, best_iou, epochs_without_gain=0,
                    optimizer=None, scheduler=None, scaler=None):
    """
    Writes a checkpoint holding only tensors and plain types, so torch.load stays safe.

    The optimizer, schedule and scaler states are what makes an exact resume possible;
    they are skipped for `best.pth`, which only has to carry the weights.

    Args:
        path (Path): Destination file.
        model (nn.Module): Network whose weights are stored.
        epoch (int): Epoch the weights come from.
        best_iou (float): Best validation IoU seen so far.
        epochs_without_gain (int): Early stopping counter.
        optimizer (torch.optim.Optimizer | None): Optimizer to store.
        scheduler (torch.optim.lr_scheduler.LRScheduler | None): Schedule to store.
        scaler (torch.amp.GradScaler | None): Gradient scaler to store, fp16 only.
    """
    checkpoint = {'model': model.state_dict(), 'epoch': epoch, 'best_iou': best_iou,
                  'epochs_without_gain': epochs_without_gain}
    if optimizer is not None:
        checkpoint['optimizer'] = optimizer.state_dict()
    if scheduler is not None:
        checkpoint['scheduler'] = scheduler.state_dict()
    if scaler is not None:
        checkpoint['scaler'] = scaler.state_dict()
    torch.save(checkpoint, path)


def restore_training_state(path, optimizer, scheduler, scaler):
    """
    Restores optimizer, schedule and counters from a checkpoint.

    Checkpoints written before this was added only carry weights, so the run continues
    from the right epoch but with a fresh optimizer and schedule.

    Args:
        path (Path): Checkpoint to read.
        optimizer (torch.optim.Optimizer): Optimizer to repopulate.
        scheduler (torch.optim.lr_scheduler.LRScheduler): Schedule to repopulate.
        scaler (torch.amp.GradScaler | None): Gradient scaler to repopulate, fp16 only.

    Returns:
        (int, float, int): Epoch to start from, best IoU so far and early stopping counter.
    """
    checkpoint = torch.load(path, map_location='cpu')

    if 'optimizer' in checkpoint:
        optimizer.load_state_dict(checkpoint['optimizer'])
        scheduler.load_state_dict(checkpoint['scheduler'])
        if scaler is not None and 'scaler' in checkpoint:
            scaler.load_state_dict(checkpoint['scaler'])
    else:
        print("warning: old checkpoint without optimizer state, restarting the schedule")

    start_epoch = checkpoint['epoch'] + 1
    best_iou = checkpoint['best_iou']
    print(f"resuming at epoch {start_epoch} | best IoU so far {best_iou:.4f}")
    return start_epoch, best_iou, checkpoint.get('epochs_without_gain', 0)


def log_metrics(path, row):
    """
    Appends one row to the CSV metrics file, writing the header on first use.

    Args:
        path (Path): CSV file.
        row (dict): Values to append; its keys become the header.
    """
    is_new = not path.exists()
    with path.open('a', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def inspect_batch(args, loader, model, device):
    """
    Prints shapes, ranges and losses of a single batch, and writes the overlay preview.

    Args:
        args (argparse.Namespace): Parsed command line configuration.
        loader (DataLoader): Loader to pull the batch from.
        model (nn.Module): Network used for the untrained forward pass.
        device (torch.device): Where the tensors live.
    """
    images, masks = next(iter(loader))
    print(f"images {tuple(images.shape)} {images.dtype} [{images.min():.2f}, {images.max():.2f}]")
    print(f"masks  {tuple(masks.shape)} {masks.dtype} [{masks.min():.2f}, {masks.max():.2f}]")
    print(f"foreground ratio: {masks.mean():.3f}")

    save_batch_preview(images[:4], masks[:4], args.preview)
    print(f"preview written to {args.preview}")

    model.eval()
    with torch.no_grad():
        logits = model(images.to(device))
        multi_bce_loss(logits, masks.to(device), verbose=True)
    print(f"side outputs: {[tuple(d.shape) for d in logits]}")


def main(argv=None):
    """
    Trains U²-Net, keeping the best checkpoint by validation IoU.

    Args:
        argv (list[str] | None): Argument list, or None to read sys.argv.
    """
    args = parse_args(argv)
    set_seed(args.seed)
    torch.backends.cudnn.benchmark = True  # Input size is fixed, so cuDNN can cache its plans.

    train_loader, val_loader = build_dataloaders(args)
    print(f"train: {len(train_loader.dataset)} images | val: {len(val_loader.dataset)} images")
    model, device = build_model(args)

    if args.dry_run:
        inspect_batch(args, train_loader, model, device)
        return

    optimizer = build_optimizer(model, args)
    scheduler = build_scheduler(optimizer, args, args.max_steps or len(train_loader))
    scaler = torch.amp.GradScaler(device.type) if args.amp == 'fp16' else None

    out_dir = Path(REPO_ROOT / 'runs' / args.model / f'v{args.version}')
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = out_dir / 'metrics.csv'
    start_epoch, best_iou, epochs_without_gain = 1, 0.0, 0
    if args.resume is not None:
        start_epoch, best_iou, epochs_without_gain = restore_training_state(
            args.resume, optimizer, scheduler, scaler)

    for epoch in range(start_epoch, args.epochs + 1):
        started = time.time()
        loss, loss0 = train_one_epoch(model, train_loader, optimizer, scheduler,
                                      scaler, device, args, epoch)
        metrics = evaluate(model, val_loader, device)

        print(f"epoch {epoch:3d} | loss {loss:.3f} (fused {loss0:.3f}) | "
              f"val IoU {metrics['iou']:.4f} dice {metrics['dice']:.4f} mae {metrics['mae']:.4f} "
              f"obj {metrics['obj_recall']:.4f} | "
              f"lr {scheduler.get_last_lr()[0]:.2e} | {time.time() - started:.0f}s")
        log_metrics(metrics_path, {'epoch': epoch, 'loss': round(loss, 4),
                                   'loss_fused': round(loss0, 4),
                                   'val_iou': round(metrics['iou'], 4),
                                   'val_dice': round(metrics['dice'], 4),
                                   'val_mae': round(metrics['mae'], 4),
                                   'val_obj_recall': round(metrics['obj_recall'], 4),
                                   'lr': scheduler.get_last_lr()[0]})

        improved = metrics['iou'] > best_iou
        if improved:
            best_iou, epochs_without_gain = metrics['iou'], 0
        else:
            epochs_without_gain += 1

        # Counters are updated first, so last.pth always mirrors the current state.
        save_checkpoint(out_dir / 'last.pth', model, epoch, best_iou,
                        epochs_without_gain, optimizer, scheduler, scaler)
        if improved:
            save_checkpoint(out_dir / 'best.pth', model, epoch, best_iou)
            print(f"  new best IoU {best_iou:.4f} -> best.pth")
        elif epochs_without_gain >= args.patience:
            print(f"stopping early: {args.patience} epochs without improvement")
            break

    print(f"done | best val IoU {best_iou:.4f} | checkpoints in {out_dir}")


if __name__ == '__main__':
    main()