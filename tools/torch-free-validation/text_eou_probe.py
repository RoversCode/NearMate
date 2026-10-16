"""在单独的 ONNX Runtime 环境中采集 Text EOU 概率。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from companion.audio.providers.text_eou_provider import RealTextEouProvider, TextEouProviderConfig, clean_text_for_eou


def collect_records(
    *,
    model_dir: Path,
    texts: list[str],
    provider: str,
    lang: str,
) -> list[dict[str, Any]]:
    """在当前环境采集固定 Text EOU 输入的概率和 token 摘要。

    Args:
        model_dir: FireRedChat turn detector 模型目录。
        texts: 待比较的原始文本列表。
        provider: 当前环境唯一允许的 ONNX provider。
        lang: chinese 或 multilingual。

    Returns:
        可序列化的 Text EOU 记录列表。
    """

    detector = RealTextEouProvider(
        config=TextEouProviderConfig(model_dir=str(model_dir), lang=lang, providers=(provider,)),
    )
    session = detector._session
    tokenizer = detector._tokenizer
    if session is None or tokenizer is None:
        raise RuntimeError("Text EOU 探针未创建 ONNX session")

    records: list[dict[str, Any]] = []
    for text in texts:
        cleaned = clean_text_for_eou(text)
        encoding = tokenizer.encode(cleaned, add_special_tokens=False)
        records.append(
            {
                "text": text,
                "cleaned_text": cleaned,
                "input_ids": list(encoding.ids),
                "probability": detector.predict_end_probability(text),
                "actual_providers": list(session.get_providers()),
            }
        )
    return records


def main() -> None:
    """执行单 provider Text EOU 探针。"""

    parser = argparse.ArgumentParser(description="Probe Text EOU with one ONNX Runtime provider")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--texts", type=Path, required=True, help="每行一个固定输入文本")
    parser.add_argument("--provider", choices=["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"], required=True)
    parser.add_argument("--lang", choices=["chinese", "multilingual"], default="chinese")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    texts = args.texts.read_text(encoding="utf-8").splitlines()
    payload = {
        "provider": args.provider,
        "model_dir": str(args.model_dir),
        "records": collect_records(
            model_dir=args.model_dir,
            texts=texts,
            provider=args.provider,
            lang=args.lang,
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
