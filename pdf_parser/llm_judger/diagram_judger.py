"""Judge diagram image types with a multimodal LLM."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DiagramType = Literal["electrical", "mechanical", "table", "others"]
LLMProvider = Literal["api", "local"]
DIAGRAM_TYPES: tuple[DiagramType, ...] = (
    "electrical",
    "mechanical",
    "table",
    "others",
)

SYSTEM_PROMPT = """You classify technical-document images into exactly one category.

Categories and visual evidence:
- electrical: electrical schematics, single-line/wiring/terminal/cable diagrams, or
  control circuits. Look for wires and junctions connecting standardized electrical
  symbols, device tags such as K/Q/F/X/M/PLC, terminal numbers, cross-references,
  voltage/current labels, coils, contacts, relays, breakers, motors, or transformers.
- mechanical: physical construction, assembly, layout, installation, section, front/
  side/top view, enclosure/panel arrangement, or dimensioned engineering drawing.
  Look for object outlines, dimensions and arrows, tolerances, scale, section hatching,
  callouts, mounting holes, parts and spatial views. A panel layout is mechanical even
  when electrical equipment is drawn as physical objects rather than circuit symbols.
- table: the main content is a row/column grid or strongly aligned tabular listing,
  such as a bill of materials, parts list, revision table, terminal list, cable list,
  device list, schedule, index, or specification matrix. A small title block or small
  embedded table does not make an otherwise electrical/mechanical drawing a table.
- others: title/cover pages, prose manuals, photos, charts, logos, legends/symbol
  overviews without a specific circuit, mixed or blank pages, and anything not clearly
  covered above.

Judge the dominant information-bearing content, not the border or title block. When a
page mixes types, choose the type occupying the largest meaningful area. Return JSON
only with keys: diagram_type, confidence, reasoning. diagram_type must be one of the
four labels, confidence must be between 0 and 1, and reasoning must be concise."""


class DiagramClassificationError(RuntimeError):
    """Raised when the model request or response cannot be classified safely."""


@dataclass(frozen=True)
class LLMConfig:
    """Classification behavior and connection settings for the diagram judge."""

    provider: LLMProvider = "api"
    model: str = ""
    base_url: str = ""
    api_key: str | None = None
    timeout_seconds: float = 30.0
    max_tokens: int = 512
    temperature: float = 0.0
    reasoning_effort: str | None = None
    max_retries: int = 1
    storage_directory: str = "./storage/cache/entity"

    @classmethod
    def local(cls, **overrides: Any) -> "LLMConfig":
        """Create a default local OpenAI-compatible vision configuration."""
        defaults: dict[str, Any] = {
            "provider": "local",
            "model": "Qwen/Qwen2.5-VL-7B-Instruct",
            "base_url": "http://localhost:8000/v1",
            "api_key": None,
            "reasoning_effort": None,
        }
        defaults.update(overrides)
        return cls(**defaults)

    @classmethod
    def from_env(cls, **overrides: Any) -> "LLMConfig":
        """Load a vendor-neutral OpenAI-compatible endpoint configuration."""
        values: dict[str, Any] = {
            "provider": os.getenv("LLM_PROVIDER", "api"),
            "model": os.getenv("LLM_MODEL", ""),
            "base_url": os.getenv("LLM_BASE_URL", ""),
            "api_key": os.getenv("LLM_API_KEY") or None,
            "timeout_seconds": float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
            "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "512")),
            "reasoning_effort": os.getenv("LLM_REASONING_EFFORT") or None,
        }
        values.update(overrides)
        return cls(**values)

    @property
    def chat_completions_url(self) -> str:
        url = self.base_url.rstrip("/")
        if not url:
            raise DiagramClassificationError(
                "No LLM endpoint is configured. Set LLM_BASE_URL and LLM_MODEL."
            )
        if url.endswith("/chat/completions"):
            return url
        return f"{url}/chat/completions"


@dataclass(frozen=True)
class DiagramClassification:
    diagram_type: DiagramType
    confidence: float
    reasoning: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagram_type": self.diagram_type,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


Transport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]


class DiagramClassifier:
    """Send an image to a multimodal LLM and validate its four-way result."""

    def __init__(self, config: LLMConfig, *, transport: Transport | None = None):
        self.config = config
        self._transport = transport or _post_with_openai_compatible_http

    def classify(self, image: str | Path | bytes, *, mime_type: str | None = None) -> DiagramClassification:
        if not self.config.model:
            raise DiagramClassificationError(
                "No multimodal model is configured. Set LLM_MODEL."
            )
        image_url = _image_data_url(image, mime_type=mime_type)
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Classify the dominant content in this image.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url},
                        },
                    ],
                },
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if self.config.reasoning_effort is not None:
            payload["reasoning_effort"] = self.config.reasoning_effort
        payload["_max_retries"] = self.config.max_retries
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Some hosted gateways reject urllib's default signature before
            # the request reaches their OpenAI-compatible endpoint.
            "User-Agent": "EplanMaster-LLM-Judger/1.0",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        response = self._transport(
            self.config.chat_completions_url,
            payload,
            headers,
            self.config.timeout_seconds,
        )
        return _parse_response(response)


def _image_data_url(image: str | Path | bytes, *, mime_type: str | None) -> str:
    if isinstance(image, bytes):
        content = image
        detected_type = mime_type or "image/png"
    else:
        path = Path(image)
        if not path.is_file():
            raise FileNotFoundError(f"Image not found: {path}")
        content = path.read_bytes()
        detected_type = mime_type or mimetypes.guess_type(path.name)[0] or "image/png"
    if not detected_type.startswith("image/"):
        raise ValueError(f"Expected an image MIME type, got: {detected_type}")
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{detected_type};base64,{encoded}"


def _post_with_openai_compatible_http(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Call any OpenAI-compatible chat completions endpoint with stdlib HTTP."""
    request_payload = dict(payload)
    request_payload.pop("_max_retries", None)
    data = json.dumps(request_payload).encode("utf-8")
    request = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise DiagramClassificationError(
            f"LLM request failed with HTTP {exc.code}: {details}"
        ) from exc
    except URLError as exc:
        raise DiagramClassificationError(f"LLM request failed: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise DiagramClassificationError("LLM response is not valid JSON") from exc


def _parse_response(response: dict[str, Any]) -> DiagramClassification:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DiagramClassificationError("LLM response has no assistant content") from exc

    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str):
        raise DiagramClassificationError("LLM assistant content is not text")
    match = re.search(r"\{.*\}", content.strip(), flags=re.DOTALL)
    if match is None:
        raise DiagramClassificationError("LLM response does not contain a JSON object")
    try:
        result = json.loads(match.group(0))
        diagram_type = str(result["diagram_type"]).strip().lower()
        confidence = float(result["confidence"])
        reasoning = str(result["reasoning"]).strip()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DiagramClassificationError("LLM classification JSON is invalid") from exc
    if diagram_type not in DIAGRAM_TYPES:
        raise DiagramClassificationError(f"Unsupported diagram_type: {diagram_type}")
    if not 0.0 <= confidence <= 1.0:
        raise DiagramClassificationError("confidence must be between 0 and 1")
    if not reasoning:
        raise DiagramClassificationError("reasoning must not be empty")
    return DiagramClassification(diagram_type, confidence, reasoning)  # type: ignore[arg-type]
