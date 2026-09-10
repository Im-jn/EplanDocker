# Diagram LLM Judger

Classifies one image into `electrical`, `mechanical`, `table`, or `others` with
an OpenAI-compatible multimodal model. The same client works with local vLLM
and hosted APIs.

The defaults are Groq's `https://api.groq.com/openai/v1` endpoint and the
multimodal `qwen/qwen3.6-27b` model. API keys are always passed explicitly and
are never stored by this package.

Hosted API requests are sent with Groq's official Python SDK. Local provider
requests are sent directly to an OpenAI-compatible `/chat/completions`
endpoint. Install dependencies before running the classifier:

```powershell
pip install -r requirements.txt
```

Local vLLM example:

```powershell
python -m vllm.entrypoints.openai.api_server --model Qwen/Qwen2.5-VL-7B-Instruct

python -m pdf_parser.llm_judger .\page.png `
    --provider local `
    --model "Qwen/Qwen2.5-VL-7B-Instruct" `
    --base-url "http://localhost:8000/v1"
```

Remote API example:

```powershell
python -m pdf_parser.llm_judger .\page.png `
    --provider api `
    --api-key "your-key"
```

PDF pipeline local example:

```powershell
python -m pdf_parser.main `
    --pdf-file-path .\storage\data\eplan_pdf\example.pdf `
    --llm-provider local `
    --llm-model "Qwen/Qwen2.5-VL-7B-Instruct" `
    --llm-base-url "http://localhost:8000/v1"
```

Python usage:

```python
from pdf_parser.llm_judger import DiagramClassifier, LLMConfig

classifier = DiagramClassifier(
    LLMConfig.groq(api_key="your-key")
)
result = classifier.classify("page.png")
print(result.diagram_type, result.confidence, result.reasoning)
```

The PDF pipeline uses `PersistentDiagramClassifier` instead. It scopes cached
results by the PDF SHA-256 and validates each page/entity entry with a geometry
and text fingerprint. Cache misses call the configured LLM and are written
atomically under `storage/cache/entity`; cache hits do not render an
entity image or make a network request.

All connection settings are passed explicitly through `LLMConfig` or CLI arguments;
the classifier does not read environment variables.
