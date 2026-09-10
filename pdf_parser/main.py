from __future__ import annotations

import argparse
from pathlib import Path

from pdf_parser.diagram_pdf_parser import parse_diagram_pdf, save_pdf_info
from pdf_parser.llm_judger import LLMConfig
from pdf_parser.utils import resolve_repo_relative


DEFAULT_LLM_CONFIG = LLMConfig()
DEFAULT_LOCAL_LLM_CONFIG = LLMConfig.local()


def _resolve_llm_config(args: argparse.Namespace) -> LLMConfig:
    base_config = (
        DEFAULT_LOCAL_LLM_CONFIG
        if args.llm_provider == "local"
        else DEFAULT_LLM_CONFIG
    )
    return LLMConfig(
        provider=args.llm_provider,
        model=args.llm_model or base_config.model,
        base_url=args.llm_base_url or base_config.base_url,
        api_key=args.llm_api_key if args.llm_api_key is not None else base_config.api_key,
        timeout_seconds=args.llm_timeout,
        storage_directory=args.llm_storage_directory,
        reasoning_effort=(
            None
            if args.llm_provider == "local"
            else base_config.reasoning_effort
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Develop diagram extraction against one PDF page.")
    parser.add_argument(
        "--pdf-file-path",
        dest="pdf_file_path",
        default="./storage/data/eplan_pdf/example.pdf",
        help="Input PDF path.",
    )
    parser.add_argument(
        "--output-file",
        default="./storage/output/pdf_parsing_result/example.json",
        help="JSON file used to save the complete extraction result.",
    )
    parser.add_argument(
        "--llm-provider",
        choices=("api", "local"),
        default=DEFAULT_LLM_CONFIG.provider,
        help="Use hosted API defaults or a local OpenAI-compatible LLM server.",
    )
    parser.add_argument(
        "--llm-model",
        help=(
            "Multimodal model used for page classification. Defaults to the "
            "selected provider's model."
        ),
    )
    parser.add_argument(
        "--llm-base-url",
        help=(
            "OpenAI-compatible API base URL. Defaults to Groq for api mode and "
            "http://localhost:8000/v1 for local mode."
        ),
    )
    parser.add_argument(
        "--llm-api-key",
        help="Optional API key; omit it for an unauthenticated local vLLM server.",
    )
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=DEFAULT_LLM_CONFIG.timeout_seconds,
    )
    parser.add_argument(
        "--llm-storage-directory",
        default=DEFAULT_LLM_CONFIG.storage_directory,
        help="Directory used to persist document entity classifications.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore any page checkpoint and process every diagram page again.",
    )
    args = parser.parse_args()
    output_path = Path(args.output_file)
    checkpoint_file = output_path.with_name(f"{output_path.name}.resume.json")
    llm_config = _resolve_llm_config(args)

    try:
        pdf_info = parse_diagram_pdf(
            args.pdf_file_path,
            llm_config=llm_config,
            progress_callback=lambda message: print(
                f"[pdf_parser] {message}",
                flush=True,
            ),
            checkpoint_file=checkpoint_file,
            resume=not args.no_resume,
            show_page_progress=True,
        )
        output_path = save_pdf_info(pdf_info, args.output_file)
        checkpoint_path = resolve_repo_relative(str(checkpoint_file))
        if checkpoint_path.is_file():
            checkpoint_path.unlink()
        print(f"[pdf_parser] Saved extraction result to {output_path}", flush=True)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
