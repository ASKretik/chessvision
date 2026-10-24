# models/

Holds the A.5 rating-estimate artefacts.

    maia2.onnx         93.2 MB  NOT committed -- fetched at Docker build time
    maia2_meta.json      21 KB  committed

The `.onnx` is 7 MB from GitHub's hard 100 MB file limit, so it is not in the
repo. Upload it as a release asset and pass the URL at build time:

    docker build \
      --build-arg MAIA2_ONNX_URL=https://github.com/<you>/<repo>/releases/download/maia2-rapid-v1/maia2.onnx \
      --build-arg MAIA2_ONNX_SHA256=<sha from the export output> .

Build time, never boot time: a cold start after Render's 15-minute idle sleep
must not also wait on a 93 MB download.

Generate both files with a Python **3.12** venv -- maia2 pins `torch==2.4.0`,
which has no 3.14 wheels, and matching the pin keeps the conversion check
meaningful:

    python3.12 -m venv .venv-export
    .venv-export/bin/pip install torch==2.4.0 maia2 onnx onnxruntime chess==1.10.0
    .venv-export/bin/python tools/export_maia2_onnx.py --out models/ --type rapid
    .venv-export/bin/python tools/verify_maia2_onnx.py --out models/

`--type` is `rapid` (default) or `blitz`; the choice is recorded in the
metadata and surfaced as `rating_estimate.model_type`.

The deployed container ships `onnxruntime` and these two files and **never
installs torch** -- torch alone would not fit Render's 512 MB (plan.md E.3).

If these files are absent the app still runs: `partA.Maia2Session` reports
the rating estimate as unavailable with a reason, and every other part of
the analysis is unaffected. Check `analyses[].white.rating_estimate.reason`
in the API response.

`maia2_meta.json` is required alongside the `.onnx`. The move vocabulary is
an ordered list mapping policy logits to UCI moves and is part of the trained
weights -- a wrong ordering produces confident nonsense rather than an error,
so the runtime refuses to guess it. The metadata also carries the Elo
bucketing, the 18-plane layout, output indices and the `.onnx` sha256.

`tools/verify_maia2_onnx.py` is the Part F check for this component. It
asserts three things that can each drift silently: that `partA.encode_board`
is byte-identical to the real `maia2.utils.board_to_tensor`, that ONNX
matches PyTorch on a fixed batch, and that illegal moves are excluded from
the softmax. Run it after any re-export or maia2 upgrade -- the encoding is
the one error nothing downstream can detect, because a wrong plane layout
still yields a plausible-looking probability distribution.
