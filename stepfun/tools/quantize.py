#!/usr/bin/env python3
"""对 bge-small-zh-v1.5 做 ONNX 动态 INT8 量化，并输出体积对比。

用法（在含 onnxruntime 的环境里执行，或直接用 tools/quantize.sh 走 Docker）：
    python tools/quantize.py \
        --input  models/bge-small-zh-v1.5/onnx/model.onnx \
        --output models/bge-small-zh-v1.5/onnx/model_int8.onnx

量化方式：ONNX Runtime `quantize_dynamic`，权重 QInt8、per-channel、reduce_range，
与模型仓库 quantize_config.json 中的配置一致：
- `MatMul`：把线性层权重压成 INT8，激活值按 batch 动态求 scale；
- `Gather`：把词表 embedding 表压成 INT8（bge 的词表占 21MB+，不量化会明显拖累压缩比）。
"""

import argparse
import json
import os

from onnxruntime.quantization import QuantType, quantize_dynamic


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.2f} MiB"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="models/bge-small-zh-v1.5/onnx/model.onnx")
    ap.add_argument("--output", default="models/bge-small-zh-v1.5/onnx/model_int8.onnx")
    ap.add_argument("--report", default="tools/quantize-report.json")
    args = ap.parse_args()

    quantize_dynamic(
        model_input=args.input,
        model_output=args.output,
        per_channel=True,
        reduce_range=True,
        weight_type=QuantType.QInt8,
        op_types_to_quantize=["MatMul", "Gather"],
    )

    before = os.path.getsize(args.input)
    after = os.path.getsize(args.output)
    report = {
        "source_model": "Xenova/bge-small-zh-v1.5 (base: BAAI/bge-small-zh-v1.5)",
        "method": "onnxruntime.quantization.quantize_dynamic",
        "weight_type": "QInt8",
        "per_channel": True,
        "reduce_range": True,
        "op_types_to_quantize": ["MatMul", "Gather"],
        "fp32_bytes": before,
        "int8_bytes": after,
        "fp32_mib": round(before / 1024 / 1024, 2),
        "int8_mib": round(after / 1024 / 1024, 2),
        "compression_ratio": round(before / after, 2),
    }
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
