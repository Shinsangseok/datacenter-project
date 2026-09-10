# LLM Comparison

## Test Environment

- Runtime: llama.cpp Docker server
- Hardware: EC2, 2 vCPU, 8 GiB RAM
- Context size: 2048
- Threads: 2
- Parallel requests: 1
- Models were stored outside the Git repository.

## Compared Models

| Model | Quantization | Result |
|---|---|---|
| Qwen3-0.6B | Q8_0 | Fast, but generated an inappropriate check |
| Qwen3-1.7B | Q4_K_M | More natural, but inferred an unsupported trend |
| EXAONE-3.5-2.4B | Q4_K_M | Natural Korean, but made unsupported normal-range judgments and was slow |

## Decision

The LLM is used only as an optional explanation assistant.

- The classification model determines NORMAL or ANOMALY.
- Application code calculates threshold comparisons and trends.
- The LLM summarizes supplied measurements and predefined check items.
- The LLM does not determine root causes or change server settings.
- LLM requests are triggered manually.