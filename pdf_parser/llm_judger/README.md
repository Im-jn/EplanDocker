# Diagram LLM Judger

This package classifies an image as `electrical`, `mechanical`, `table`, or
`others` with any multimodal model that exposes an OpenAI-compatible
`/chat/completions` endpoint. The same HTTP client is used for hosted APIs and
local inference servers; no vendor SDK is required.

Configure it with environment variables:

```dotenv
LLM_PROVIDER=api
LLM_BASE_URL=https://your-provider.example/v1
LLM_API_KEY=your-api-key
LLM_MODEL=your-multimodal-model
LLM_TIMEOUT_SECONDS=30
LLM_MAX_TOKENS=512
```

For an unauthenticated local server, set `LLM_PROVIDER=local`, leave
`LLM_API_KEY` empty, and point `LLM_BASE_URL` at the local endpoint. From a
Docker Desktop container, a server on the host is normally reached through
`http://host.docker.internal:<port>/v1` rather than `localhost`.

Command-line example:

```powershell
python -m pdf_parser.llm_judger .\page.png `
    --provider api `
    --model "your-multimodal-model" `
    --base-url "https://your-provider.example/v1" `
    --api-key "your-api-key"
```

Python example:

```python
from pdf_parser.llm_judger import DiagramClassifier, LLMConfig

classifier = DiagramClassifier(LLMConfig.from_env())
result = classifier.classify("page.png")
print(result.diagram_type, result.confidence, result.reasoning)
```

The PDF pipeline uses `PersistentDiagramClassifier`. It scopes cached results
by PDF SHA-256 and validates every page/entity entry with a geometry and text
fingerprint. Cache misses call the configured LLM and are written atomically
under `storage/cache/entity`; cache hits do not render an entity image or make
a network request.
