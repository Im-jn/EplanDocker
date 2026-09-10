"""Tests for the OpenAI-compatible diagram classifier."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from tempfile import TemporaryDirectory

from pdf_parser.llm_judger.diagram_judger import (
    DiagramClassificationError,
    DiagramClassifier,
    LLMConfig,
)
from pdf_parser.llm_judger import PersistentDiagramClassifier
from pdf_parser.pages_manager import PathBase, TextBase, VectorBase


def test_classifies_image_and_builds_openai_compatible_request() -> None:
    captured = {}

    def transport(url, payload, headers, timeout):
        captured.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return {
            "choices": [{
                "message": {
                    "content": '{"diagram_type":"electrical","confidence":0.94,'
                    '"reasoning":"Connected circuit symbols and wires."}'
                }
            }]
        }

    classifier = DiagramClassifier(
        LLMConfig(
            model="vision-model",
            base_url="http://localhost:8000/v1",
            api_key="secret",
            timeout_seconds=7,
        ),
        transport=transport,
    )
    result = classifier.classify(b"fake-png", mime_type="image/png")

    assert result.diagram_type == "electrical"
    assert result.confidence == 0.94
    assert captured["url"] == "http://localhost:8000/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["User-Agent"] == "EplanMaster-LLM-Judger/1.0"
    image_url = captured["payload"]["messages"][1]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    assert "reasoning_effort" not in captured["payload"]
    assert captured["payload"]["max_tokens"] == 512
    assert captured["payload"]["_max_retries"] == 1


def test_rejects_unknown_model_label() -> None:
    def transport(url, payload, headers, timeout):
        return {
            "choices": [{
                "message": {
                    "content": '{"diagram_type":"architecture","confidence":0.8,'
                    '"reasoning":"A floor plan."}'
                }
            }]
        }

    classifier = DiagramClassifier(
        LLMConfig(model="vision-model", base_url="http://llm.test/v1"),
        transport=transport,
    )
    try:
        classifier.classify(b"image", mime_type="image/jpeg")
    except DiagramClassificationError as exc:
        assert "Unsupported diagram_type" in str(exc)
    else:
        raise AssertionError("Expected invalid diagram type to be rejected")


def test_remote_openai_compatible_settings_load_from_environment() -> None:
    with patch.dict(
        "os.environ",
        {
            "LLM_PROVIDER": "api",
            "LLM_MODEL": "vendor/vision-model",
            "LLM_BASE_URL": "https://llm.example.com/v1",
            "LLM_API_KEY": "test-key",
        },
        clear=False,
    ):
        config = LLMConfig.from_env()

    assert config.provider == "api"
    assert config.model == "vendor/vision-model"
    assert config.base_url == "https://llm.example.com/v1"
    assert config.api_key == "test-key"


def test_local_provider_uses_openai_compatible_payload_without_auth() -> None:
    captured = {}

    def transport(url, payload, headers, timeout):
        captured.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return {
            "choices": [{
                "message": {
                    "content": '{"diagram_type":"mechanical","confidence":0.87,'
                    '"reasoning":"Dimensioned physical layout."}'
                }
            }]
        }

    classifier = DiagramClassifier(
        LLMConfig.local(model="local-vision", timeout_seconds=9),
        transport=transport,
    )
    result = classifier.classify(b"fake-png", mime_type="image/png")

    assert result.diagram_type == "mechanical"
    assert captured["url"] == "http://localhost:8000/v1/chat/completions"
    assert "Authorization" not in captured["headers"]
    assert captured["payload"]["model"] == "local-vision"
    assert captured["payload"]["max_tokens"] == 512
    assert "max_completion_tokens" not in captured["payload"]
    assert "reasoning_effort" not in captured["payload"]
    assert captured["payload"]["_max_retries"] == 1
    assert captured["timeout"] == 9


def test_persistent_classifier_calls_llm_once_then_restores_from_storage() -> None:
    calls = []

    def transport(url, payload, headers, timeout):
        calls.append(payload)
        return {
            "choices": [{
                "message": {
                    "content": '{"diagram_type":"electrical","confidence":0.91,'
                    '"reasoning":"Circuit symbols and wires."}'
                }
            }]
        }

    vectors = VectorBase([PathBase(type="line", points=[(0, 0), (10, 0)])])
    texts = TextBase([{"text": "-Q1", "location": (1, 1, 5, 3)}])
    with TemporaryDirectory() as directory:
        pdf_path = Path(directory) / "document.pdf"
        pdf_path.write_bytes(b"stable-document-identity")
        storage_directory = Path(directory) / "storage"
        config = LLMConfig(
            model="vision-model",
            base_url="http://llm.test/v1",
            storage_directory=str(storage_directory),
        )

        first = PersistentDiagramClassifier(
            config,
            pdf_path,
            transport=transport,
        ).classify_entity(
            page_number=12,
            entity_index=1,
            vectors=vectors,
            texts=texts,
            image_factory=lambda: b"fake-png",
        )
        second = PersistentDiagramClassifier(
            config,
            pdf_path,
            transport=lambda *args: (_ for _ in ()).throw(
                AssertionError("LLM must not be called on a cache hit")
            ),
        ).classify_entity(
            page_number=12,
            entity_index=1,
            vectors=vectors,
            texts=texts,
            image_factory=lambda: (_ for _ in ()).throw(
                AssertionError("Image must not be rendered on a cache hit")
            ),
        )

        assert first.cache_hit is False
        assert second.cache_hit is True
        assert second.classification == first.classification
        assert len(calls) == 1
        assert len(list(storage_directory.glob("*.json"))) == 1
