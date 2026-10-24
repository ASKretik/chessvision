"""
Part F verification for the A.5 component.

plan.md F: "ONNX output == PyTorch output on a fixed batch -- conversion can
silently change numbers." A.5 sums log-probabilities over ~40 moves, so a
small per-move drift compounds into the wrong rating band. This checks it,
and two other things that can silently diverge:

  1. ENCODING  -- partA.encode_board() == maia2.utils.board_to_tensor() on the
                  mirrored board. partA reimplements this (it must not import
                  torch), so it can drift from the real source.
  2. CONVERSION -- ONNX logits == PyTorch logits on a fixed batch of real
                  positions.
  3. END-TO-END -- partA's masked log-probs agree with maia2's own
                  _masked_softmax over the real pipeline, every row is a
                  distribution over legal moves only, and illegal moves get
                  exactly zero.

Layer 1 is the one that matters most in practice: a wrong plane layout
produces plausible-looking probabilities for the wrong reasons, and nothing
downstream can detect it.

Offline-only (imports torch). Run from the pinned export venv:

    .venv-export/bin/python tools/verify_maia2_onnx.py
    .venv-export/bin/python tools/verify_maia2_onnx.py --out models/
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Tolerances. ONNX CPU uses the same fp32 kernels, so agreement should be
# near-exact; 1e-4 catches a real change while allowing reassociation noise.
LOGIT_ATOL = 1e-4
PROB_ATOL = 1e-4

# A fixed batch of real positions, chosen to exercise every plane:
# both sides to move, full and partial castling rights, an en-passant
# square, promotions available, and a quiet endgame.
FENS = [
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1",
    "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 5 4",
    "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 4",
    "r3k2r/8/8/8/8/8/8/R3K2R w Kq - 0 1",
    "r3k2r/8/8/8/8/8/8/R3K2R b Qk - 0 1",
    "8/P7/8/8/8/8/7p/K6k w - - 0 1",
    "8/5k2/8/8/8/8/5K2/4R3 b - - 0 1",
    "r1bq1r1k/ppp2ppp/2n5/2bpP3/5P2/2N2N2/PPP3PP/R1BQ1RK1 w - - 0 11",
]

ELOS = [(1500, 1500), (1100, 1900), (2000, 1000), (1750, 1650),
        (900, 900), (1500, 2000), (1234, 1567), (2100, 1050), (1400, 1400)]


def _green(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def verify(onnx_path: str, meta_path: str, model_type: str = "rapid", net=None) -> bool:
    import chess
    import numpy as np
    import onnxruntime as ort
    import torch
    from maia2.utils import (board_to_tensor, create_elo_dict,
                             get_all_possible_moves, map_to_category)

    # partA must be importable without torch; it is on the path one level up.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import partA

    with open(meta_path) as fh:
        meta = json.load(fh)

    results: list[tuple[str, bool, str]] = []

    # ---------------------------------------------------------------
    # 1. ENCODING: partA.encode_board vs the real board_to_tensor
    # ---------------------------------------------------------------
    enc_ok = True
    first_bad = ""
    for fen in FENS:
        board = chess.Board(fen)
        pov = board if board.turn == chess.WHITE else board.mirror()
        real = board_to_tensor(pov).numpy()
        mine = partA.encode_board(board)
        if mine.shape != real.shape or not np.array_equal(real, mine):
            enc_ok = False
            if not first_bad:
                bad = sorted(set(np.where(real != mine)[0].tolist())) if mine.shape == real.shape \
                    else [f"shape {mine.shape} != {real.shape}"]
                first_bad = f"{fen} -> planes {bad}"
    results.append(("encoding: partA.encode_board == maia2.board_to_tensor",
                    enc_ok, first_bad))

    # Metadata must describe the layout partA actually implements.
    results.append((
        "encoding id matches partA.Maia2Session.ENCODING",
        meta.get("encoding") == partA.Maia2Session.ENCODING,
        f"{meta.get('encoding')!r} vs {partA.Maia2Session.ENCODING!r}",
    ))

    # ---------------------------------------------------------------
    # Vocabulary and bucketing must match the live helpers
    # ---------------------------------------------------------------
    all_moves = get_all_possible_moves()
    elo_dict = create_elo_dict()
    results.append(("move_vocab matches get_all_possible_moves() exactly",
                    meta["move_vocab"] == all_moves,
                    f"{len(meta['move_vocab'])} vs {len(all_moves)}"))
    results.append(("elo_dict matches create_elo_dict() exactly",
                    meta["elo_dict"] == elo_dict, ""))

    # partA reproduces map_to_category from the metadata parameters.
    bucket_ok, bad_elo = True, ""
    for elo in [0, 800, 1099, 1100, 1150, 1199, 1200, 1555, 1900, 1999, 2000, 2500, 3000]:
        want = map_to_category(elo, elo_dict)
        got = partA.elo_to_bucket(elo, meta["elo_bucketing"], len(elo_dict))
        if want != got:
            bucket_ok = False
            bad_elo = bad_elo or f"elo {elo}: real {want} != partA {got}"
    results.append(("partA.elo_to_bucket == maia2.map_to_category", bucket_ok, bad_elo))

    # ---------------------------------------------------------------
    # 2. CONVERSION: ONNX vs PyTorch on the fixed batch
    # ---------------------------------------------------------------
    if net is None:
        from maia2 import model as maia2_model
        net = maia2_model.from_pretrained(type=model_type, device="cpu")
        if isinstance(net, tuple):
            net = net[0]
        net.eval()

    boards_t, selfs, oppos = [], [], []
    for fen, (es, eo) in zip(FENS, ELOS):
        board = chess.Board(fen)
        pov = board if board.turn == chess.WHITE else board.mirror()
        boards_t.append(board_to_tensor(pov))
        selfs.append(map_to_category(es, elo_dict))
        oppos.append(map_to_category(eo, elo_dict))

    bt = torch.stack(boards_t)
    st = torch.tensor(selfs, dtype=torch.long)
    ot = torch.tensor(oppos, dtype=torch.long)

    with torch.no_grad():
        ref_policy, _, ref_value = net(bt, st, ot)
    ref_policy = ref_policy.numpy()
    ref_value = ref_value.numpy()

    so = ort.SessionOptions()
    so.intra_op_num_threads = 1
    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"], sess_options=so)
    outs = sess.run(None, {
        meta["input_names"][0]: bt.numpy(),
        meta["input_names"][1]: st.numpy(),
        meta["input_names"][2]: ot.numpy(),
    })
    got_policy = outs[meta["policy_output_index"]]
    got_value = outs[meta["value_output_index"]]

    pol_diff = float(np.abs(ref_policy - got_policy).max())
    val_diff = float(np.abs(ref_value - got_value).max())
    argmax_same = bool((ref_policy.argmax(-1) == got_policy.argmax(-1)).all())

    results.append((f"ONNX policy == PyTorch policy (max |diff| {pol_diff:.3e})",
                    pol_diff <= LOGIT_ATOL, f"tolerance {LOGIT_ATOL}"))
    results.append((f"ONNX value == PyTorch value (max |diff| {val_diff:.3e})",
                    val_diff <= LOGIT_ATOL, f"tolerance {LOGIT_ATOL}"))
    results.append(("policy argmax identical on every position", argmax_same, ""))

    # ---------------------------------------------------------------
    # 3. END-TO-END: partA masked log-probs vs maia2's own masked softmax
    #
    # The reference is maia2.inference._masked_softmax composed with
    # preprocessing() -- the authoritative pipeline, and what get_preds (the
    # batch path) uses.
    #
    # NOT inference_each(): in maia2 0.9.1 that path calls .nonzero() on an
    # already-unsqueezed (1, N) tensor, so .flatten() interleaves the row
    # index 0 with the real columns. It returns 2x the legal moves, half of
    # them vocab[0] ("a1h8"), each with probability 0. Harmless (the bogus
    # entries are masked to zero) but it makes a naive dict comparison fail.
    # Checked separately below so we notice if upstream fixes it.
    # ---------------------------------------------------------------
    from maia2.inference import _masked_softmax, preprocessing

    session = partA.Maia2Session(onnx_path)
    session.load()

    ref_rows, mine_rows, legal_counts = [], [], []
    for fen, (es, eo) in zip(FENS, ELOS):
        board = chess.Board(fen)
        if board.is_game_over():
            continue
        b_in, es_i, eo_i, legal_mask = preprocessing(fen, es, eo, elo_dict, 
                                                     {m: i for i, m in enumerate(all_moves)})
        with torch.no_grad():
            logits, _, _ = net(b_in.unsqueeze(0),
                               torch.tensor([es_i]), torch.tensor([eo_i]))
        ref_rows.append(_masked_softmax(logits, legal_mask.unsqueeze(0))[0].numpy())
        mine_rows.append(np.exp(session.log_probs([board], elo_self=es, elo_oppo=eo)[0]))
        legal_counts.append(int(legal_mask.sum().item()))

    ref = np.stack(ref_rows)
    mine = np.stack(mine_rows)
    worst = float(np.abs(ref - mine).max())
    results.append((
        f"partA masked probs == maia2._masked_softmax (max |diff| {worst:.3e})",
        worst <= PROB_ATOL, f"tolerance {PROB_ATOL}",
    ))

    # Masking is the point of step 4: every row must be a distribution over
    # the legal moves only, not over all 1880.
    sums_ok = bool(np.allclose(mine.sum(axis=1), 1.0, atol=1e-5))
    results.append(("partA rows sum to 1.0 over legal moves", sums_ok,
                    f"sums {mine.sum(axis=1).min():.6f}..{mine.sum(axis=1).max():.6f}"))

    nonzero_counts = (mine > 0).sum(axis=1).tolist()
    mask_ok = nonzero_counts == legal_counts
    results.append(("partA assigns zero probability to every illegal move",
                    mask_ok, f"nonzero {nonzero_counts} vs legal {legal_counts}"))

    # Guard on the upstream quirk: if this starts passing, inference_each
    # was fixed and the note above is stale.
    from maia2.inference import inference_each, prepare

    official, _ = inference_each(net, prepare(), FENS[0], 1500, 1500)
    start_legal = chess.Board(FENS[0]).legal_moves.count()
    quirk_present = len(official) == start_legal + 1 and official.get("a1h8") == 0.0
    results.append((
        "known maia2 0.9.1 inference_each .nonzero() quirk still present",
        quirk_present,
        f"{len(official)} entries for {start_legal} legal moves; "
        f"a1h8={official.get('a1h8')} -- if this FAILS, upstream fixed it "
        "and the comment in this file is stale",
    ))

    # ---------------------------------------------------------------
    print()
    width = max(len(n) for n, _, _ in results)
    all_ok = True
    for name, ok, note in results:
        all_ok &= ok
        line = f"  [{_green(ok)}] {name:<{width}}"
        if note and not ok:
            line += f"   <- {note}"
        elif note and ok:
            line += f"   ({note})"
        print(line)
    print()
    print(f"{sum(1 for _, ok, _ in results if ok)}/{len(results)} checks passed")
    return all_ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="models", help="directory holding maia2.onnx + meta")
    ap.add_argument("--type", default="rapid", choices=["rapid", "blitz"])
    args = ap.parse_args()
    onnx_p = os.path.join(args.out, "maia2.onnx")
    meta_p = os.path.join(args.out, "maia2_meta.json")
    for p in (onnx_p, meta_p):
        if not os.path.exists(p):
            sys.exit(f"missing {p} -- run tools/export_maia2_onnx.py first")
    sys.exit(0 if verify(onnx_p, meta_p, model_type=args.type) else 1)
