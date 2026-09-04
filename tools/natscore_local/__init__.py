"""Local rebuild of natscore (offline): uses cached whisper-small + a local final.pt.

Forked from harrrshall/natscore (Apache-2.0 code / CC-BY-NC-4.0 weights).
This stripped package drops the HF auto-download and lets you point at an
already-downloaded `final.pt` so the scorer runs fully offline.
"""

from __future__ import annotations

from typing import Any

import torch

from .compare import Pair
from .score import Scorer

__version__ = "0.1.0.dev0.local"
__all__ = ["__version__", "load", "load_local", "Scorer", "Pair"]

DEFAULT_MODEL_ID = "harrrshall/natscore-small-v0"
DEFAULT_CHECKPOINT_FILENAME = "final.pt"
_SHORT_ALIASES = {"natscore-small-v0": "harrrshall/natscore-small-v0"}


def _build_head_from_checkpoint(ckpt: dict[str, Any]):
    """Reconstruct a `NatScoreHead` from a saved checkpoint dict.

    Reads the head config from `ckpt["config"]["model"]` and strips the
    `module.` prefix from `model_state` keys (added by DataParallel during
    multi-GPU training).
    """
    from .model import NatScoreHead, NatScoreHeadConfig

    if "model_state" not in ckpt:
        raise KeyError(
            "Checkpoint is missing 'model_state'. Got keys: "
            f"{sorted(ckpt.keys())}. Is this a NatScore checkpoint?"
        )

    model_cfg_raw = ckpt.get("config", {}).get("model", {})
    if not model_cfg_raw:
        raise KeyError(
            "Checkpoint is missing config['model']. Cannot reconstruct head."
        )

    cfg = NatScoreHeadConfig(**model_cfg_raw)
    head = NatScoreHead(cfg)

    state = ckpt["model_state"]
    state = {
        (k[len("module."):] if k.startswith("module.") else k): v
        for k, v in state.items()
    }
    head.load_state_dict(state)
    return head


def _finish_load(ckpt_path, device, dtype, encoder_model_name):
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    device = torch.device(device)

    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    head = _build_head_from_checkpoint(ckpt)
    head = head.to(device).eval()

    from .features import WhisperFeatureExtractor

    extractor = WhisperFeatureExtractor(
        model_name=encoder_model_name,
        device=device,
        dtype=dtype,
    )
    return Scorer(extractor=extractor, head=head, device=device)


def load(
    model_id: str = DEFAULT_MODEL_ID,
    *,
    checkpoint_filename: str = DEFAULT_CHECKPOINT_FILENAME,
    device: str | torch.device | None = None,
    dtype: torch.dtype = torch.float32,
    cache_dir: str | None = None,
    revision: str | None = None,
    encoder_model_name: str = "openai/whisper-small",
) -> Scorer:
    """Load a NatScore checkpoint from HuggingFace Hub and return a ready Scorer."""
    from huggingface_hub import hf_hub_download

    repo_id = _SHORT_ALIASES.get(model_id, model_id)
    ckpt_path = hf_hub_download(
        repo_id=repo_id,
        filename=checkpoint_filename,
        revision=revision,
        cache_dir=cache_dir,
    )
    return _finish_load(ckpt_path, device, dtype, encoder_model_name)


def load_local(
    checkpoint_path,
    *,
    device: str | torch.device | None = None,
    dtype: torch.dtype = torch.float32,
    encoder_model_name: str = "openai/whisper-small",
) -> Scorer:
    """Load a NatScore checkpoint from a local `final.pt` (offline)."""
    return _finish_load(checkpoint_path, device, dtype, encoder_model_name)
