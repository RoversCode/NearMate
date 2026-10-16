"""用固定 token 历史验证 ASR 的 llama logits 与缓存复用。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from environment_fingerprint import environment_fingerprint, file_sha256


def run(model_path: Path, output: Path, baseline: bool, inputs: Path | None) -> None:
    """逐块输入保存的 token，保存采样前的完整词表 logits。

    Args:
        model_path: 冻结的 ASR GGUF 模型。
        output: 数组和配置报告的目录。
        baseline: 是否使用旧环境的 Torch 运行库。
        inputs: 基准保存的 token 文件；候选必须复用该文件。
    Returns:
        无；原生解码失败或后端回退时抛出异常。
    """
    if baseline:
        from baseline_runtime import initialize_baseline_runtime

        initialize_baseline_runtime()
    from companion.audio.qwen_asr_gguf.inference import llama

    output.mkdir(parents=True, exist_ok=False)
    model_hash = file_sha256(model_path)
    model = llama.LlamaModel(str(model_path), n_gpu_layers=-1, backend="cuda")
    context_config = {
        "n_ctx": 2048,
        "n_batch": 2048,
        "n_ubatch": 512,
        "embeddings": False,
        "n_threads": 4,
        "n_threads_batch": 4,
    }
    context = llama.LlamaContext(model, **context_config)
    if model.actual_backend != "cuda":
        raise RuntimeError(f"ASR llama 后端回退：{model.actual_backend}")
    if inputs is None:
        if not baseline:
            raise ValueError("候选必须读取基准 token，不能自行生成另一条历史")
        tokens = np.asarray(
            model.tokenize("你好，今天我想讨论语音识别。"), dtype=np.int32
        )
    else:
        tokens = np.load(inputs)
    if tokens.dtype != np.int32 or tokens.ndim != 1 or tokens.size < 8:
        raise ValueError("固定历史必须至少包含八个 int32 token")
    np.save(output / "tokens.npy", tokens)
    vocab_size = llama.llama_vocab_n_tokens(model.vocab)
    batch = llama.LlamaBatch(len(tokens), embd_dim=0, n_seq_max=1)
    arrays = {}
    # 第二轮先清空再重放同一历史，覆盖缓存复用；不做任何随机采样或自由生成。
    for repeat in range(2):
        context.clear_kv_cache()
        position = 0
        for step, stop in enumerate((4, 5, 6, len(tokens))):
            count = stop - position
            batch.n_tokens = count
            for index, token in enumerate(tokens[position:stop]):
                batch.token[index] = int(token)
                batch.pos[index] = position + index
                batch.n_seq_id[index] = 1
                batch.seq_id[index][0] = 0
                batch.logits[index] = int(index == count - 1)
            status = context.decode(batch)
            if status != 0:
                raise RuntimeError(f"固定历史解码失败：{status}")
            pointer = context.get_logits_ith(-1)
            if not pointer:
                raise RuntimeError("原生模型没有返回 logits")
            arrays[f"repeat-{repeat}-step-{step}"] = np.ctypeslib.as_array(
                pointer, shape=(vocab_size,)
            ).copy()
            position = stop
    np.savez(output / "logits.npz", **arrays)
    report = {
        "model": str(model_path.resolve()),
        "model_sha256": model_hash,
        "context": context_config,
        "backend": model.actual_backend,
        "input_sha256": file_sha256(output / "tokens.npy"),
        "environment": environment_fingerprint(),
        "scope": "固定文本 token 的四步 logits 与缓存清空复用；不代表音频 embedding 历史全覆盖",
    }
    (output / "run.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        {
            "steps": len(arrays),
            "vocab_size": vocab_size,
            "backend": model.actual_backend,
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--inputs", type=Path)
    args = parser.parse_args()
    run(args.model, args.output, args.baseline, args.inputs)
