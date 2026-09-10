"""Document-scoped persistent wrapper around the multimodal classifier."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pdf_parser.pages_manager import TextBase, VectorBase
from pdf_parser.storage import EntityStore, entity_fingerprint

from .diagram_judger import (
    DIAGRAM_TYPES,
    DiagramClassification,
    DiagramClassificationError,
    DiagramClassifier,
    LLMConfig,
    Transport,
)


ImageFactory = Callable[[], str | Path | bytes]


@dataclass(frozen=True)
class EntityClassificationDecision:
    classification: DiagramClassification
    cache_hit: bool


class PersistentDiagramClassifier:
    """Read an entity result from storage or call the LLM and persist the result."""

    def __init__(
        self,
        config: LLMConfig,
        pdf_path: str | Path,
        *,
        transport: Transport | None = None,
    ):
        self.config = config
        self.classifier = DiagramClassifier(config, transport=transport)
        self.store = EntityStore(
            pdf_path,
            storage_directory=config.storage_directory,
        )

    def classify_entity(
        self,
        *,
        page_number: int,
        entity_index: int,
        vectors: VectorBase,
        texts: TextBase,
        image_factory: ImageFactory,
    ) -> EntityClassificationDecision:
        fingerprint = entity_fingerprint(vectors, texts)
        cached = self.store.get(page_number, entity_index, fingerprint)
        if cached is not None:
            return EntityClassificationDecision(
                classification=_classification_from_mapping(cached),
                cache_hit=True,
            )

        classification = self.classifier.classify(image_factory())
        self.store.put(
            page_number,
            entity_index,
            fingerprint,
            classification.to_dict(),
            source="llm",
            model=self.config.model,
        )
        return EntityClassificationDecision(
            classification=classification,
            cache_hit=False,
        )


def _classification_from_mapping(value: dict[str, object]) -> DiagramClassification:
    try:
        diagram_type = str(value["diagram_type"])
        confidence = float(value["confidence"])  # type: ignore[arg-type]
        reasoning = str(value["reasoning"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise DiagramClassificationError("Cached classification is invalid") from exc
    if diagram_type not in DIAGRAM_TYPES:
        raise DiagramClassificationError(
            f"Cached classification has unsupported diagram_type: {diagram_type}"
        )
    if not 0.0 <= confidence <= 1.0 or not reasoning:
        raise DiagramClassificationError("Cached classification values are invalid")
    return DiagramClassification(diagram_type, confidence, reasoning)  # type: ignore[arg-type]
