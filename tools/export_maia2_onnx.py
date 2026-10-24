"""
A.5 -- one-time, offline Maia-2 -> ONNX conversion.

Run on a laptop, never in the deployed image: this file imports torch, and
plan.md E.3 requires that the Render image never installs it. `tools/` is in
.dockerignore for exactly that reason.

    python3.12 -m venv .venv-export
    .venv-export/bin/pip install torch==2.4.0 maia2 onnx onnxruntime chess==1.10.0
    .venv-export/bin/python tools/export_maia2_onnx.py --out models/ --type rapid

Python 3.12, not 3.14: maia2 pins torch==2.4.0, which has no 3.14 wheels.
Matching the pin removes any doubt about whether a newer torch changed the
numbers -- which is what tools/verify_maia2_onnx.py exists to catch.

Writes:
    models/maia2.onnx         ~93 MB, fetched at Docker build time (not committed)
    models/maia2_meta.json    ~60 KB, committed

Every value in the metadata is read off the real model or the real maia2
helpers. Nothing here is reconstructed from documentation, because the move
vocabulary's *ordering* is part of the trained weights: a wrong order maps
logits to the wrong moves and produces confident nonsense instead of an error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

# Must match partA.Maia2Session.ENCODING. Bumped from the earlier "maia2-v1",
# which described a reconstructed layout that did not match the real model.
ENCODING_ID = "maia2-board-tensor-18ch"

# Documentation of board_to_tensor's channel layout, verified against
# maia2/utils.py. Written into the metadata so the contract the runtime
# implements is explicit and diffable rather than implied.
PLANE_LAYOUT = [
    "white-pawn", "white-knight", "white-bishop", "white-rook", "white-queen", "white-king",
    "black-pawn", "black-knight", "black-bishop", "black-rook", "black-queen", "black-king",
    "side-to-move-is-white",
    "castling-white-kingside", "castling-white-queenside",
    "castling-black-kingside", "castling-black-queenside",
    "en-passant-square",
]

MIRROR_CONVENTION = (
    "The board is mirrored with chess.Board.mirror() iff black is to move "
    "(maia2/inference.py preprocessing), which flips ranks AND swaps colours, "
    "so planes 0-5 are always the side to move. Played and legal moves are "
    "mirrored to match (maia2/utils.py mirror_move)."
)


def fail(msg: str) -> None:
    sys.exit(f"ERROR: {msg}")


def load_real_maia2(model_type: str):
    """Import the real package and load pretrained weights. No fallbacks."""
    try:
        from maia2 import model as maia2_model
        from maia2.utils import create_elo_dict, get_all_possible_moves
    except ImportError as exc:
        fail(
            f"maia2 is not importable ({exc}). This script is offline-only:\n"
            "    pip install torch==2.4.0 maia2 onnx onnxruntime chess==1.10.0"
        )
    net = maia2_model.from_pretrained(type=model_type, device="cpu")
    if isinstance(net, tuple):
        net = net[0]
    # from_pretrained does NOT do this, and the model has dropout layers.
    # Tracing in train mode would bake dropout into the exported graph.
    net.eval()
    return net, get_all_possible_moves(), create_elo_dict()


def check_consistency(net, all_moves, elo_dict) -> None:
    """
    Cross-check the model against the helpers before exporting anything.

    These are the invariants the runtime depends on. If any fails, the
    vocabulary or bucketing has changed and the metadata would be a lie.
    """
    problems = []
    if net.cfg.input_channels != len(PLANE_LAYOUT):
        problems.append(
            f"cfg.input_channels={net.cfg.input_channels} but PLANE_LAYOUT "
            f"documents {len(PLANE_LAYOUT)} planes -- re-read "
            "maia2/utils.py board_to_tensor and update PLANE_LAYOUT."
        )
    if net.fc_1.out_features != len(all_moves):
        problems.append(
            f"policy head is {net.fc_1.out_features} wide but "
            f"get_all_possible_moves() returned {len(all_moves)} moves"
        )
    if net.elo_embedding.num_embeddings != len(elo_dict):
        problems.append(
            f"elo_embedding has {net.elo_embedding.num_embeddings} rows but "
            f"create_elo_dict() returned {len(elo_dict)} buckets"
        )
    if len(set(all_moves)) != len(all_moves):
        problems.append("move vocabulary contains duplicates")
    if sorted(elo_dict.values()) != list(range(len(elo_dict))):
        problems.append(f"elo bucket indices are not 0..n-1: {sorted(elo_dict.values())}")
    if problems:
        fail("model/helper mismatch:\n  - " + "\n  - ".join(problems))


def export(out_dir: str, model_type: str, opset: int, run_verify: bool) -> None:
    import torch

    os.makedirs(out_dir, exist_ok=True)
    onnx_path = os.path.join(out_dir, "maia2.onnx")
    meta_path = os.path.join(out_dir, "maia2_meta.json")

    net, all_moves, elo_dict = load_real_maia2(model_type)
    check_consistency(net, all_moves, elo_dict)

    n_planes = net.cfg.input_channels
    batch = 4
    boards = torch.zeros(batch, n_planes, 8, 8, dtype=torch.float32)
    elo_self = torch.zeros(batch, dtype=torch.int64)
    elo_oppo = torch.zeros(batch, dtype=torch.int64)

    # The real forward returns three tensors:
    #   logits_maia      (B, 1880)  policy over the move vocabulary
    #   logits_side_info (B, 2021)  auxiliary training head, unused at inference
    #   logits_value     (B,)       win/loss value
    with torch.no_grad():
        outs = net(boards, elo_self, elo_oppo)
    if len(outs) != 3:
        fail(f"expected 3 outputs from forward(), got {len(outs)}")

    input_names = ["boards", "elo_self", "elo_oppo"]
    output_names = ["logits_maia", "logits_side_info", "logits_value"]

    torch.onnx.export(
        net,
        (boards, elo_self, elo_oppo),
        onnx_path,
        input_names=input_names,
        output_names=output_names,
        dynamic_axes={n: {0: "batch"} for n in input_names + output_names},
        opset_version=opset,
        do_constant_folding=True,
        training=torch.onnx.TrainingMode.EVAL,
    )
    size_mb = os.path.getsize(onnx_path) / 1e6
    sha = _sha256(onnx_path)
    print(f"wrote {onnx_path} ({size_mb:.1f} MB)")
    print(f"  sha256: {sha}")

    meta = {
        "encoding": ENCODING_ID,
        "model_type": model_type,
        "input_channels": n_planes,
        "plane_layout": PLANE_LAYOUT,
        "mirror_convention": MIRROR_CONVENTION,
        "input_names": input_names,
        "output_names": output_names,
        "policy_output_index": 0,
        "value_output_index": 2,
        "policy_dim": net.fc_1.out_features,
        "move_vocab": all_moves,
        "elo_dict": elo_dict,
        # Parameters of maia2.utils.map_to_category, so the runtime can
        # reproduce the raw-Elo -> bucket-index mapping exactly instead of
        # approximating it by nearest value.
        "elo_bucketing": {"start": 1100, "end": 2000, "interval": 100},
        "opset": opset,
        "onnx_sha256": sha,
        "onnx_size_bytes": os.path.getsize(onnx_path),
        "versions": {
            "maia2": _version("maia2"),
            "torch": torch.__version__,
            "chess": _version("chess"),
        },
        "note": (
            "Generated by tools/export_maia2_onnx.py from the real maia2 "
            "package. move_vocab ordering is part of the trained weights -- "
            "never regenerate it independently of the model. Verify any "
            "re-export with tools/verify_maia2_onnx.py."
        ),
    }
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=1)
    print(f"wrote {meta_path} ({os.path.getsize(meta_path)/1000:.0f} KB)")
    print(f"  {len(all_moves)} moves, {len(elo_dict)} elo buckets, {n_planes} planes")

    if size_mb > 95:
        print(
            f"\nWARNING: {size_mb:.1f} MB is close to GitHub's 100 MB hard limit. "
            "This file is fetched at Docker build time, not committed -- upload it "
            "as a release asset and set MAIA2_ONNX_URL."
        )

    if run_verify:
        print("\n--- verification ---")
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, here)
        from verify_maia2_onnx import verify

        if not verify(onnx_path, meta_path, model_type=model_type, net=net):
            fail("verification FAILED -- do not publish this .onnx")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _version(mod: str) -> str:
    try:
        return __import__("importlib.metadata", fromlist=["version"]).version(mod)
    except Exception:
        return "unknown"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="models", help="output directory")
    ap.add_argument("--type", default="rapid", choices=["rapid", "blitz"],
                    help="which pretrained Maia-2 model (default: rapid)")
    ap.add_argument("--opset", type=int, default=17)
    ap.add_argument("--no-verify", dest="verify", action="store_false",
                    help="skip the PyTorch-vs-ONNX check (not recommended)")
    args = ap.parse_args()
    export(args.out, args.type, args.opset, args.verify)
