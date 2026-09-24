#!/usr/bin/env python3
"""Breeze TTS worker — stdin/stdout JSONL-RPC（由 adapters/tts_local.py 拉起）。

协议：每行一个请求 {"id":seg_id, "text":..., "out":"…/x.wav"}
     回复 {"id", "ok":true, "dur":sec, "gen_s":sec} 或 {"id","ok":false,"err"}
启动时先打一行 {"ready":true,"vram_gb":x} 完成握手，再进请求循环。
模型只载一次；每请求 set_all_seeds(seed) 保逐句可复现（bakeoff 胜出配方：
ref_clone_tata + guidance_scale=1.0 + seed 42 + bf16 eager）。

跑法：venvs/breeze/bin/python tools/tts_workers/breeze.py \
        --repo tools/tts_workers/breeze-tts --weights <ckpt> \
        --ref-audio state/tts-bakeoff/refs/g_orig.wav --ref-text "…"
"""
import argparse
import json
import os
import sys
import time
import warnings
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="breeze-tts clone 路径")
    ap.add_argument("--weights", required=True,
                    help="ckpt 目录（须含 audio_tokenizer/ 子目录）")
    ap.add_argument("--ref-audio", required=True)
    ap.add_argument("--ref-text", required=True)
    ap.add_argument("--tpl", default="ref_clone_tata")
    ap.add_argument("--gs", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-new", type=int, default=1500)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    sys.path.insert(0, str(repo))
    os.chdir(repo)                       # breeze_infer/models 相对导入
    warnings.filterwarnings("ignore")

    import soundfile as sf               # noqa: E402
    import torch                         # noqa: E402
    from breeze_infer.runtime import (   # noqa: E402
        load_runtime, resolve_device, set_all_seeds,
        update_generation_config_for_breeze)
    from breeze_infer.templates import (  # noqa: E402
        get_template, prepare_inputs)
    from models.generation_breeze import (  # noqa: E402
        _extract_decoded_audio_tensor)

    tok, model, atok = load_runtime(
        Path(args.weights), device=resolve_device(),
        attn_implementation="eager")
    update_generation_config_for_breeze(model)
    tpl = get_template(args.tpl)
    vram = (torch.cuda.max_memory_allocated() / 2**30
            if torch.cuda.is_available() else 0.0)
    print(json.dumps({"ready": True, "vram_gb": round(vram, 2)}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = {}
        try:
            req = json.loads(line)
            rid, text, out = req["id"], req["text"], req["out"]
            t0 = time.time()
            set_all_seeds(args.seed)
            inputs = prepare_inputs(
                tok, atok, model,
                [{"id": rid, "text": text, "speaker": "S0",
                  "ref_audio_path": args.ref_audio,
                  "ref_text": args.ref_text}],
                tpl, guidance_scale=args.gs,
                guidance_scale_ref=None, guidance_scale_ins=None)
            gen = model.generate(**inputs, output_audio=True,
                                 audio_tokenizer=atok,
                                 max_new_tokens=args.max_new)
            wav = _extract_decoded_audio_tensor(gen).float().cpu().numpy()
            sf.write(out, wav, 24000)
            print(json.dumps({"id": rid, "ok": True,
                              "dur": round(len(wav) / 24000, 3),
                              "gen_s": round(time.time() - t0, 2)}),
                  flush=True)
        except Exception as e:
            print(json.dumps({"id": req.get("id"), "ok": False,
                              "err": f"{type(e).__name__}: {e}"[:500]}),
                  flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
