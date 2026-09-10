"""Command-line entry point for diagram image classification."""

from __future__ import annotations

import argparse
import json

from pdf_parser.llm_judger import DiagramClassifier, LLMConfig


def _resolve_llm_config(args: argparse.Namespace) -> LLMConfig:
    base_config = LLMConfig.local() if args.provider == "local" else LLMConfig.from_env()
    return LLMConfig(
        provider=args.provider,
        model=args.model or base_config.model,
        base_url=args.base_url or base_config.base_url,
        api_key=args.api_key if args.api_key is not None else base_config.api_key,
        timeout_seconds=args.timeout,
        reasoning_effort=None if args.provider == "local" else base_config.reasoning_effort,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify an image as electrical, mechanical, table, or others.",
    )
    parser.add_argument("image", help="Path to a PNG/JPEG/WebP image")
    parser.add_argument(
        "--provider",
        choices=("api", "local"),
        default="api",
        help="Use hosted API defaults or a local OpenAI-compatible LLM server.",
    )
    parser.add_argument(
        "--model",
        help="Vision model name. Defaults to the selected provider's model.",
    )
    parser.add_argument(
        "--base-url",
        help="OpenAI-compatible API base URL or full chat/completions URL.",
    )
    parser.add_argument("--api-key", help="Optional bearer token for a remote API")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    classifier = DiagramClassifier(
        _resolve_llm_config(args)
    )
    result = classifier.classify(args.image)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
