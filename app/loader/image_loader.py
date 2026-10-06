from __future__ import annotations

import math
import statistics
import tempfile
from pathlib import Path
from typing import Any

from PIL import (
    Image,
    ImageEnhance,
    ImageFilter,
    ImageOps,
)

from app.loader.base_loader import BaseLoader
from app.model.block import BlockType, DocumentBlock
from app.model.document import Document
from app.model.page import Page


class ImageLoaderError(RuntimeError):
    """PNG / JPG / JPEG OCR loading error."""


class ImageLoader(BaseLoader):
    """
    Enterprise image OCR loader.

    OCR engine:
        RapidOCR + ONNX Runtime

    Supported:
        .png
        .jpg
        .jpeg

    Design:
        1. Read original image metadata.
        2. Run OCR on the original image.
        3. Optionally run a second OCR pass on a conservative enhanced image.
        4. Select the better OCR pass by coverage/confidence/fragmentation.
        5. Normalize OCR boxes back to original-image coordinates.
        6. Reconstruct reading order with line clustering rather than plain
           top/left sorting.
        7. Build one logical Page plus OCR line blocks.

    The RapidOCR dependency is imported lazily so PDF/DOCX/PPTX/XLSX/TXT
    processing is unaffected when OCR is not used.

    Important:
        - No dictionary-based OCR "correction" is performed here.
        - No Chapter / Section inference is performed here.
        - Low-confidence text is filtered only by minimum_score.
        - Original image bytes are never modified.
    """

    SUPPORTED_EXTENSIONS = {
        ".png",
        ".jpg",
        ".jpeg",
    }

    def __init__(
        self,
        *,
        minimum_score: float = 0.50,
        enable_preprocessing: bool = True,
        enable_multi_pass: bool = True,
        auto_upscale: bool = True,
        small_image_short_side: int = 720,
        small_image_scale: float = 2.0,
        medium_image_short_side: int = 1100,
        medium_image_scale: float = 1.35,
        maximum_upscaled_side: int = 3200,
        contrast_factor: float = 1.15,
        unsharp_radius: float = 1.0,
        unsharp_percent: int = 140,
        unsharp_threshold: int = 3,
        reading_order_y_tolerance_ratio: float = 0.60,
        enhanced_min_character_coverage: float = 0.90,
        enhanced_confidence_gain_threshold: float = 0.03,
        enhanced_strong_confidence_gain_threshold: float = 0.08,
        enhanced_strong_gain_min_coverage: float = 0.85,
        enhanced_fragmentation_density_ratio: float = 1.10,
    ) -> None:

        if not (
            0.0
            <= minimum_score
            <= 1.0
        ):
            raise ValueError(
                "minimum_score must be between 0 and 1."
            )

        if small_image_short_side <= 0:
            raise ValueError(
                "small_image_short_side must be greater than 0."
            )

        if medium_image_short_side <= 0:
            raise ValueError(
                "medium_image_short_side must be greater than 0."
            )

        if small_image_scale < 1.0:
            raise ValueError(
                "small_image_scale must be at least 1.0."
            )

        if medium_image_scale < 1.0:
            raise ValueError(
                "medium_image_scale must be at least 1.0."
            )

        if maximum_upscaled_side <= 0:
            raise ValueError(
                "maximum_upscaled_side must be greater than 0."
            )

        if contrast_factor <= 0:
            raise ValueError(
                "contrast_factor must be greater than 0."
            )

        if unsharp_radius < 0:
            raise ValueError(
                "unsharp_radius cannot be negative."
            )

        if unsharp_percent < 0:
            raise ValueError(
                "unsharp_percent cannot be negative."
            )

        if unsharp_threshold < 0:
            raise ValueError(
                "unsharp_threshold cannot be negative."
            )

        if not (
            0.0
            < reading_order_y_tolerance_ratio
            <= 2.0
        ):
            raise ValueError(
                "reading_order_y_tolerance_ratio "
                "must be greater than 0 and at most 2.0."
            )

        if not (
            0.0
            < enhanced_min_character_coverage
            <= 1.0
        ):
            raise ValueError(
                "enhanced_min_character_coverage "
                "must be greater than 0 and at most 1.0."
            )

        if enhanced_confidence_gain_threshold < 0:
            raise ValueError(
                "enhanced_confidence_gain_threshold cannot be negative."
            )

        if enhanced_strong_confidence_gain_threshold < 0:
            raise ValueError(
                "enhanced_strong_confidence_gain_threshold "
                "cannot be negative."
            )

        if (
            enhanced_strong_confidence_gain_threshold
            < enhanced_confidence_gain_threshold
        ):
            raise ValueError(
                "enhanced_strong_confidence_gain_threshold "
                "must be greater than or equal to "
                "enhanced_confidence_gain_threshold."
            )

        if not (
            0.0
            < enhanced_strong_gain_min_coverage
            <= enhanced_min_character_coverage
        ):
            raise ValueError(
                "enhanced_strong_gain_min_coverage must be greater "
                "than 0 and less than or equal to "
                "enhanced_min_character_coverage."
            )

        if enhanced_fragmentation_density_ratio < 1.0:
            raise ValueError(
                "enhanced_fragmentation_density_ratio "
                "must be at least 1.0."
            )

        self.minimum_score = float(
            minimum_score
        )

        self.enable_preprocessing = bool(
            enable_preprocessing
        )

        self.enable_multi_pass = bool(
            enable_multi_pass
        )

        self.auto_upscale = bool(
            auto_upscale
        )

        self.small_image_short_side = int(
            small_image_short_side
        )

        self.small_image_scale = float(
            small_image_scale
        )

        self.medium_image_short_side = int(
            medium_image_short_side
        )

        self.medium_image_scale = float(
            medium_image_scale
        )

        self.maximum_upscaled_side = int(
            maximum_upscaled_side
        )

        self.contrast_factor = float(
            contrast_factor
        )

        self.unsharp_radius = float(
            unsharp_radius
        )

        self.unsharp_percent = int(
            unsharp_percent
        )

        self.unsharp_threshold = int(
            unsharp_threshold
        )

        self.reading_order_y_tolerance_ratio = float(
            reading_order_y_tolerance_ratio
        )

        self.enhanced_min_character_coverage = float(
            enhanced_min_character_coverage
        )

        self.enhanced_confidence_gain_threshold = float(
            enhanced_confidence_gain_threshold
        )

        self.enhanced_strong_confidence_gain_threshold = float(
            enhanced_strong_confidence_gain_threshold
        )

        self.enhanced_strong_gain_min_coverage = float(
            enhanced_strong_gain_min_coverage
        )

        self.enhanced_fragmentation_density_ratio = float(
            enhanced_fragmentation_density_ratio
        )

        self._engine = None

    # ==================================================
    # Public API
    # ==================================================

    def load(
        self,
        file_path: str | Path,
    ) -> Document:

        path = self._validate_input_path(
            file_path
        )

        try:

            try:

                (
                    width,
                    height,
                    mode,
                ) = self._read_image_metadata(
                    path
                )

            except ImageLoaderError as exc:

                if self._is_decompression_bomb_error(
                    exc
                ):

                    return self._build_oversized_image_document(
                        path=path,
                        error=exc,
                    )

                raise

            engine = (
                self._get_ocr_engine()
            )

            pass_results: list[
                dict[str, Any]
            ] = []

            # ==========================================
            # Pass 1: Original
            # ==========================================

            original_result = engine(
                str(
                    path
                )
            )

            original_pass = (
                self._extract_ocr_pass(
                    result=(
                        original_result
                    ),
                    pass_name=(
                        "original"
                    ),
                    coordinate_scale=1.0,
                )
            )

            pass_results.append(
                original_pass
            )

            # ==========================================
            # Pass 2: Enhanced
            # ==========================================
            #
            # Conservative enhancement:
            #
            #   EXIF transpose
            #   alpha -> white background
            #   optional upscale
            #   modest contrast
            #   modest unsharp mask
            #
            # No hard binarization is used by default because screenshots
            # often contain colored/anti-aliased text and diagrams.

            if (
                self.enable_preprocessing
                and self.enable_multi_pass
            ):

                with Image.open(
                    path
                ) as original_image:

                    (
                        enhanced_image,
                        enhanced_scale,
                    ) = self._build_enhanced_image(
                        original_image
                    )

                    with tempfile.TemporaryDirectory(
                        prefix="ingestion-image-ocr-"
                    ) as temp_dir:

                        enhanced_path = (
                            Path(
                                temp_dir
                            )
                            / "enhanced.png"
                        )

                        enhanced_image.save(
                            enhanced_path,
                            format="PNG",
                            optimize=False,
                        )

                        enhanced_result = engine(
                            str(
                                enhanced_path
                            )
                        )

                        enhanced_pass = (
                            self._extract_ocr_pass(
                                result=(
                                    enhanced_result
                                ),
                                pass_name=(
                                    "enhanced"
                                ),
                                coordinate_scale=(
                                    enhanced_scale
                                ),
                            )
                        )

                        pass_results.append(
                            enhanced_pass
                        )

            # ==========================================
            # Select Pass
            # ==========================================

            selected_pass = (
                self._select_best_pass(
                    pass_results
                )
            )

            records = list(
                selected_pass[
                    "records"
                ]
            )

            # No readable OCR text is an auditable content condition, not a
            # technical loader failure.  Keep the source image as an empty
            # Document; ImageParser/ImportResiliencePolicy will persist it as
            # METADATA_ONLY (or SUPPORTING when the filename policy matches)
            # and exclude it from Current Qdrant.
            no_readable_text = not records

            # ==========================================
            # Reading Order
            # ==========================================

            records = (
                self._sort_records_reading_order(
                    records
                )
            )

            # ==========================================
            # Blocks
            # ==========================================

            blocks: list[
                DocumentBlock
            ] = []

            for order, record in enumerate(
                records
            ):

                blocks.append(
                    DocumentBlock(
                        id=(
                            f"image-ocr-line-"
                            f"{order:08d}"
                        ),
                        block_type=(
                            BlockType.PARAGRAPH
                        ),
                        text=record[
                            "text"
                        ],
                        order=order,
                        page_number=1,
                        source="image_ocr",
                        metadata={
                            "ocr_engine": (
                                "RapidOCR"
                            ),
                            "ocr_score": (
                                record[
                                    "score"
                                ]
                            ),
                            "ocr_box": (
                                record[
                                    "box"
                                ]
                            ),
                            "ocr_source_index": (
                                record[
                                    "source_index"
                                ]
                            ),
                            "ocr_pass": (
                                selected_pass[
                                    "pass_name"
                                ]
                            ),
                            "ocr_coordinate_scale": (
                                selected_pass[
                                    "coordinate_scale"
                                ]
                            ),
                            "ocr_reading_order_line_index": (
                                record.get(
                                    "reading_order_line_index"
                                )
                            ),
                        },
                    )
                )

            page_text = "\n".join(
                block.text
                for block in blocks
            ).strip()

            file_type = (
                path.suffix.lower()
                .lstrip(".")
            )

            pass_diagnostics = [
                {
                    "pass_name": (
                        item[
                            "pass_name"
                        ]
                    ),
                    "coordinate_scale": (
                        item[
                            "coordinate_scale"
                        ]
                    ),
                    "raw_text_count": (
                        item[
                            "raw_text_count"
                        ]
                    ),
                    "retained_text_count": (
                        item[
                            "retained_text_count"
                        ]
                    ),
                    "filtered_low_score_count": (
                        item[
                            "filtered_low_score_count"
                        ]
                    ),
                    "character_count": (
                        item[
                            "character_count"
                        ]
                    ),
                    "average_score": (
                        item[
                            "average_score"
                        ]
                    ),
                    "minimum_retained_score": (
                        item[
                            "minimum_retained_score"
                        ]
                    ),
                    "maximum_retained_score": (
                        item[
                            "maximum_retained_score"
                        ]
                    ),
                    "quality_score": (
                        item[
                            "quality_score"
                        ]
                    ),
                    "characters_per_line": (
                        item[
                            "characters_per_line"
                        ]
                    ),
                    "single_character_line_ratio": (
                        item[
                            "single_character_line_ratio"
                        ]
                    ),
                    "short_line_ratio": (
                        item[
                            "short_line_ratio"
                        ]
                    ),
                    "low_confidence_line_ratio": (
                        item[
                            "low_confidence_line_ratio"
                        ]
                    ),
                    "engine_elapse_seconds": (
                        item[
                            "engine_elapse_seconds"
                        ]
                    ),
                }
                for item in pass_results
            ]

            return Document(
                file_name=path.name,
                file_type=file_type,
                pages=[
                    Page(
                        page_number=1,
                        text=page_text,
                    )
                ],
                blocks=blocks,
                chapters=[],
                sections=[],
                contents=[],
                metadata={
                    "source_format": (
                        file_type
                    ),
                    "loader": (
                        "ImageLoader"
                    ),
                    "loader_status": (
                        "WARNING_NO_EXTRACTABLE_TEXT"
                        if no_readable_text
                        else "SUCCESS"
                    ),
                    "loader_warning_code": (
                        "IMAGE_OCR_NO_READABLE_TEXT"
                        if no_readable_text
                        else None
                    ),
                    "ocr_engine": (
                        "RapidOCR"
                    ),
                    "ocr_minimum_score": (
                        self.minimum_score
                    ),
                    "ocr_preprocessing_enabled": (
                        self.enable_preprocessing
                    ),
                    "ocr_multi_pass_enabled": (
                        self.enable_multi_pass
                    ),
                    "ocr_pass_count": len(
                        pass_results
                    ),
                    "ocr_selected_pass": (
                        selected_pass[
                            "pass_name"
                        ]
                    ),
                    "ocr_selected_coordinate_scale": (
                        selected_pass[
                            "coordinate_scale"
                        ]
                    ),
                    "ocr_pass_selection_strategy": (
                        "coverage_confidence_fragmentation_pairwise_v2"
                    ),
                    "ocr_selection_reason": (
                        selected_pass.get(
                            "selection_reason"
                        )
                    ),
                    "ocr_selection_metrics": (
                        selected_pass.get(
                            "selection_metrics",
                            {},
                        )
                    ),
                    "ocr_pass_diagnostics": (
                        pass_diagnostics
                    ),
                    "ocr_reading_order_strategy": (
                        "box_line_clustering_then_left_to_right"
                    ),
                    "ocr_line_count": len(
                        blocks
                    ),
                    "ocr_character_count": len(
                        page_text
                    ),
                    # Compatibility with previous metadata name.
                    "character_count": len(
                        page_text
                    ),
                    "ocr_average_score": (
                        selected_pass[
                            "average_score"
                        ]
                    ),
                    "ocr_minimum_retained_score": (
                        selected_pass[
                            "minimum_retained_score"
                        ]
                    ),
                    "ocr_maximum_retained_score": (
                        selected_pass[
                            "maximum_retained_score"
                        ]
                    ),
                    "ocr_filtered_low_score_count": (
                        selected_pass[
                            "filtered_low_score_count"
                        ]
                    ),
                    "ocr_quality_score": (
                        selected_pass[
                            "quality_score"
                        ]
                    ),
                    "image_width": (
                        width
                    ),
                    "image_height": (
                        height
                    ),
                    "image_mode": (
                        mode
                    ),
                    "image_size_bytes": (
                        path.stat().st_size
                    ),
                    "ocr_elapse_seconds": (
                        sum(
                            float(
                                item[
                                    "engine_elapse_seconds"
                                ]
                            )
                            for item
                            in pass_results
                        )
                    ),
                },
            )

        except ImageLoaderError:
            raise

        except Exception as exc:
            raise ImageLoaderError(
                f"Failed to OCR image "
                f"'{path.name}': {exc}"
            ) from exc

    # ==================================================
    # OCR Pass Extraction
    # ==================================================

    def _extract_ocr_pass(
        self,
        *,
        result: Any,
        pass_name: str,
        coordinate_scale: float,
    ) -> dict[
        str,
        Any,
    ]:

        texts = list(
            getattr(
                result,
                "txts",
                (),
            )
            or ()
        )

        scores = list(
            getattr(
                result,
                "scores",
                (),
            )
            or ()
        )

        boxes = getattr(
            result,
            "boxes",
            None,
        )

        box_list: list[
            Any
        ] = []

        if boxes is not None:

            try:
                box_list = (
                    boxes.tolist()
                )

            except AttributeError:
                box_list = list(
                    boxes
                )

        records: list[
            dict[str, Any]
        ] = []

        filtered_low_score_count = 0

        for index, raw_text in enumerate(
            texts
        ):

            text = str(
                raw_text
                or ""
            ).strip()

            if not text:
                continue

            score = (
                float(
                    scores[
                        index
                    ]
                )
                if index
                < len(
                    scores
                )
                else 1.0
            )

            if (
                score
                < self.minimum_score
            ):

                filtered_low_score_count += 1

                continue

            raw_box = (
                box_list[
                    index
                ]
                if index
                < len(
                    box_list
                )
                else None
            )

            normalized_box = (
                self._normalize_box_to_original_coordinates(
                    raw_box,
                    scale=(
                        coordinate_scale
                    ),
                )
            )

            geometry = (
                self._box_geometry(
                    normalized_box
                )
            )

            records.append(
                {
                    "source_index": (
                        index
                    ),
                    "text": (
                        text
                    ),
                    "score": (
                        score
                    ),
                    "box": (
                        normalized_box
                    ),
                    **geometry,
                }
            )

        retained_scores = [
            float(
                record[
                    "score"
                ]
            )
            for record in records
        ]

        character_count = sum(
            len(
                record[
                    "text"
                ]
            )
            for record in records
        )

        average_score = (
            statistics.fmean(
                retained_scores
            )
            if retained_scores
            else 0.0
        )

        minimum_retained_score = (
            min(
                retained_scores
            )
            if retained_scores
            else 0.0
        )

        maximum_retained_score = (
            max(
                retained_scores
            )
            if retained_scores
            else 0.0
        )

        (
            characters_per_line,
            single_character_line_ratio,
            short_line_ratio,
            low_confidence_line_ratio,
        ) = self._calculate_fragmentation_metrics(
            records
        )

        quality_score = (
            self._calculate_pass_quality_score(
                records
            )
        )

        return {
            "pass_name": (
                pass_name
            ),
            "coordinate_scale": (
                float(
                    coordinate_scale
                )
            ),
            "records": (
                records
            ),
            "raw_text_count": len(
                texts
            ),
            "retained_text_count": len(
                records
            ),
            "filtered_low_score_count": (
                filtered_low_score_count
            ),
            "character_count": (
                character_count
            ),
            "average_score": round(
                average_score,
                6,
            ),
            "minimum_retained_score": round(
                minimum_retained_score,
                6,
            ),
            "maximum_retained_score": round(
                maximum_retained_score,
                6,
            ),
            "quality_score": round(
                quality_score,
                6,
            ),
            "characters_per_line": round(
                characters_per_line,
                6,
            ),
            "single_character_line_ratio": round(
                single_character_line_ratio,
                6,
            ),
            "short_line_ratio": round(
                short_line_ratio,
                6,
            ),
            "low_confidence_line_ratio": round(
                low_confidence_line_ratio,
                6,
            ),
            "engine_elapse_seconds": float(
                getattr(
                    result,
                    "elapse",
                    0.0,
                )
                or 0.0
            ),
        }

    # ==================================================
    # Pass Selection
    # ==================================================

    @staticmethod
    def _calculate_fragmentation_metrics(
        records: list[
            dict[str, Any]
        ],
    ) -> tuple[
        float,
        float,
        float,
        float,
    ]:
        """
        Calculate OCR fragmentation indicators.

        Returns:
            characters_per_line:
                Larger is generally better when comparing two OCR passes of
                the same source image.

            single_character_line_ratio:
                Ratio of retained OCR lines containing exactly one visible
                character.

            short_line_ratio:
                Ratio of retained OCR lines containing at most two visible
                characters.

            low_confidence_line_ratio:
                Ratio of retained lines with OCR score < 0.65.

        These metrics are diagnostics and tie-break signals. They are not used
        to delete OCR content.
        """

        if not records:

            return (
                0.0,
                0.0,
                0.0,
                0.0,
            )

        lengths = [
            len(
                str(
                    record.get(
                        "text",
                        "",
                    )
                    or ""
                ).strip()
            )
            for record in records
        ]

        character_count = sum(
            lengths
        )

        line_count = len(
            records
        )

        single_character_line_count = sum(
            1
            for length in lengths
            if length == 1
        )

        short_line_count = sum(
            1
            for length in lengths
            if 0 < length <= 2
        )

        low_confidence_line_count = sum(
            1
            for record in records
            if float(
                record.get(
                    "score",
                    0.0,
                )
                or 0.0
            )
            < 0.65
        )

        return (
            (
                character_count
                / line_count
            ),
            (
                single_character_line_count
                / line_count
            ),
            (
                short_line_count
                / line_count
            ),
            (
                low_confidence_line_count
                / line_count
            ),
        )

    @classmethod
    def _calculate_pass_quality_score(
        cls,
        records: list[
            dict[str, Any]
        ],
    ) -> float:
        """
        Standalone diagnostic score for one OCR pass.

        Unlike the previous accumulated-line score, this metric does not
        reward a pass merely for splitting text into more OCR boxes.

        The authoritative best-pass decision is pairwise and is implemented in
        _select_best_pass(), where text coverage is measured against the
        original OCR pass.
        """

        if not records:

            return 0.0

        scores = [
            float(
                record.get(
                    "score",
                    0.0,
                )
                or 0.0
            )
            for record in records
        ]

        average_score = statistics.fmean(
            scores
        )

        minimum_score = min(
            scores
        )

        (
            characters_per_line,
            single_character_line_ratio,
            short_line_ratio,
            low_confidence_line_ratio,
        ) = cls._calculate_fragmentation_metrics(
            records
        )

        character_count = sum(
            len(
                str(
                    record.get(
                        "text",
                        "",
                    )
                    or ""
                ).strip()
            )
            for record in records
        )

        confidence_component = (
            average_score
            * 100.0
        )

        minimum_confidence_component = (
            minimum_score
            * 8.0
        )

        useful_text_component = (
            math.log1p(
                character_count
            )
            * 2.0
        )

        density_component = (
            min(
                characters_per_line,
                40.0,
            )
            / 40.0
            * 6.0
        )

        fragmentation_penalty = (
            single_character_line_ratio
            * 12.0
            + short_line_ratio
            * 8.0
            + low_confidence_line_ratio
            * 10.0
        )

        return (
            confidence_component
            + minimum_confidence_component
            + useful_text_component
            + density_component
            - fragmentation_penalty
        )

    def _select_best_pass(
        self,
        pass_results: list[
            dict[str, Any]
        ],
    ) -> dict[
        str,
        Any,
    ]:
        """
        Conservative pairwise OCR-pass selector.

        The original pass is the baseline because it has no preprocessing
        risk. Enhanced OCR must demonstrate measurable benefit.

        Signals:
            character_coverage_ratio
            average_confidence_gain
            minimum_confidence_gain
            characters_per_line_ratio
            short-line fragmentation
            single-character-line fragmentation

        Default policy:
            1. Normal enhanced win:
               coverage >= 90%
               AND avg confidence gain >= +0.03
               AND no serious minimum-confidence collapse
               AND fragmentation/density evidence is acceptable.

            2. Strong enhanced win:
               coverage >= 85%
               AND avg confidence gain >= +0.08.

            3. Tiny confidence gains remain original.

            4. Exact ties remain original.
        """

        if not pass_results:

            raise ImageLoaderError(
                "No OCR pass result is available."
            )

        if len(
            pass_results
        ) == 1:

            selected = dict(
                pass_results[
                    0
                ]
            )

            selected[
                "selection_reason"
            ] = (
                "single_ocr_pass"
            )

            selected[
                "selection_metrics"
            ] = {}

            return (
                selected
            )

        original = next(
            (
                item
                for item
                in pass_results
                if item.get(
                    "pass_name"
                )
                == "original"
            ),
            None,
        )

        if original is None:

            selected = max(
                pass_results,
                key=lambda item: (
                    float(
                        item.get(
                            "quality_score",
                            0.0,
                        )
                        or 0.0
                    ),
                    float(
                        item.get(
                            "average_score",
                            0.0,
                        )
                        or 0.0
                    ),
                    int(
                        item.get(
                            "character_count",
                            0,
                        )
                        or 0
                    ),
                ),
            )

            result = dict(
                selected
            )

            result[
                "selection_reason"
            ] = (
                "no_original_pass_quality_fallback"
            )

            result[
                "selection_metrics"
            ] = {}

            return (
                result
            )

        best = dict(
            original
        )

        best[
            "selection_reason"
        ] = (
            "original_baseline"
        )

        best[
            "selection_metrics"
        ] = {}

        original_character_count = max(
            int(
                original.get(
                    "character_count",
                    0,
                )
                or 0
            ),
            1,
        )

        original_average = float(
            original.get(
                "average_score",
                0.0,
            )
            or 0.0
        )

        original_minimum = float(
            original.get(
                "minimum_retained_score",
                0.0,
            )
            or 0.0
        )

        original_density = max(
            float(
                original.get(
                    "characters_per_line",
                    0.0,
                )
                or 0.0
            ),
            1e-9,
        )

        original_short_ratio = float(
            original.get(
                "short_line_ratio",
                0.0,
            )
            or 0.0
        )

        original_single_ratio = float(
            original.get(
                "single_character_line_ratio",
                0.0,
            )
            or 0.0
        )

        qualifying_candidates: list[
            dict[str, Any]
        ] = []

        for candidate in pass_results:

            if candidate is original:
                continue

            candidate_character_count = int(
                candidate.get(
                    "character_count",
                    0,
                )
                or 0
            )

            candidate_average = float(
                candidate.get(
                    "average_score",
                    0.0,
                )
                or 0.0
            )

            candidate_minimum = float(
                candidate.get(
                    "minimum_retained_score",
                    0.0,
                )
                or 0.0
            )

            candidate_density = float(
                candidate.get(
                    "characters_per_line",
                    0.0,
                )
                or 0.0
            )

            candidate_short_ratio = float(
                candidate.get(
                    "short_line_ratio",
                    0.0,
                )
                or 0.0
            )

            candidate_single_ratio = float(
                candidate.get(
                    "single_character_line_ratio",
                    0.0,
                )
                or 0.0
            )

            coverage = (
                candidate_character_count
                / original_character_count
            )

            average_gain = (
                candidate_average
                - original_average
            )

            minimum_gain = (
                candidate_minimum
                - original_minimum
            )

            density_ratio = (
                candidate_density
                / original_density
            )

            short_ratio_improvement = (
                original_short_ratio
                - candidate_short_ratio
            )

            single_ratio_improvement = (
                original_single_ratio
                - candidate_single_ratio
            )

            metrics = {
                "baseline_pass": (
                    "original"
                ),
                "candidate_pass": (
                    candidate.get(
                        "pass_name"
                    )
                ),
                "character_coverage_ratio": round(
                    coverage,
                    6,
                ),
                "average_confidence_gain": round(
                    average_gain,
                    6,
                ),
                "minimum_confidence_gain": round(
                    minimum_gain,
                    6,
                ),
                "characters_per_line_ratio": round(
                    density_ratio,
                    6,
                ),
                "short_line_ratio_improvement": round(
                    short_ratio_improvement,
                    6,
                ),
                "single_character_line_ratio_improvement": round(
                    single_ratio_improvement,
                    6,
                ),
            }

            regular_coverage = (
                coverage
                >= self.enhanced_min_character_coverage
            )

            strong_gain_coverage = (
                coverage
                >= self.enhanced_strong_gain_min_coverage
            )

            meaningful_confidence_gain = (
                average_gain
                >= self.enhanced_confidence_gain_threshold
            )

            strong_confidence_gain = (
                average_gain
                >= self.enhanced_strong_confidence_gain_threshold
            )

            fragmentation_not_worse = (
                candidate_short_ratio
                <= original_short_ratio
                + 0.05
                and candidate_single_ratio
                <= original_single_ratio
                + 0.05
            )

            fragmentation_not_severely_worse = (
                candidate_short_ratio
                <= original_short_ratio
                + 0.15
                and candidate_single_ratio
                <= original_single_ratio
                + 0.15
            )

            density_improved = (
                density_ratio
                >= self.enhanced_fragmentation_density_ratio
            )

            minimum_confidence_not_collapsed = (
                minimum_gain
                >= -0.03
            )

            reason: (
                str
                | None
            ) = None

            if (
                regular_coverage
                and meaningful_confidence_gain
                and minimum_confidence_not_collapsed
                and (
                    fragmentation_not_worse
                    or density_improved
                    or minimum_gain
                    >= 0.05
                )
            ):

                reason = (
                    "enhanced_confidence_gain_with_coverage"
                )

            elif (
                strong_gain_coverage
                and strong_confidence_gain
                and minimum_confidence_not_collapsed
                and (
                    fragmentation_not_severely_worse
                    or density_improved
                    or minimum_gain
                    >= 0.10
                )
            ):

                reason = (
                    "enhanced_strong_confidence_gain"
                )

            if reason is not None:

                qualified = dict(
                    candidate
                )

                qualified[
                    "selection_reason"
                ] = (
                    reason
                )

                qualified[
                    "selection_metrics"
                ] = (
                    metrics
                )

                qualifying_candidates.append(
                    qualified
                )

                continue

            best[
                "selection_reason"
            ] = (
                "original_preserved_by_pairwise_gate"
            )

            best[
                "selection_metrics"
            ] = (
                metrics
            )

        if qualifying_candidates:

            return max(
                qualifying_candidates,
                key=lambda item: (
                    float(
                        item.get(
                            "average_score",
                            0.0,
                        )
                        or 0.0
                    ),
                    float(
                        item.get(
                            "quality_score",
                            0.0,
                        )
                        or 0.0
                    ),
                    int(
                        item.get(
                            "character_count",
                            0,
                        )
                        or 0
                    ),
                ),
            )

        return (
            best
        )

    # ==================================================
    # Preprocessing
    # ==================================================

    def _build_enhanced_image(
        self,
        image: Image.Image,
    ) -> tuple[
        Image.Image,
        float,
    ]:

        working = (
            ImageOps.exif_transpose(
                image
            )
        )

        working = (
            self._flatten_alpha_to_white(
                working
            )
        )

        scale = (
            self._resolve_upscale_factor(
                width=(
                    working.width
                ),
                height=(
                    working.height
                ),
            )
        )

        if scale > 1.0:

            target_width = max(
                1,
                int(
                    round(
                        working.width
                        * scale
                    )
                ),
            )

            target_height = max(
                1,
                int(
                    round(
                        working.height
                        * scale
                    )
                ),
            )

            working = working.resize(
                (
                    target_width,
                    target_height,
                ),
                resample=(
                    Image.Resampling.LANCZOS
                ),
            )

        if (
            abs(
                self.contrast_factor
                - 1.0
            )
            > 1e-9
        ):

            working = (
                ImageEnhance.Contrast(
                    working
                ).enhance(
                    self.contrast_factor
                )
            )

        if (
            self.unsharp_percent
            > 0
            and self.unsharp_radius
            > 0
        ):

            working = working.filter(
                ImageFilter.UnsharpMask(
                    radius=(
                        self.unsharp_radius
                    ),
                    percent=(
                        self.unsharp_percent
                    ),
                    threshold=(
                        self.unsharp_threshold
                    ),
                )
            )

        return (
            working,
            scale,
        )

    @staticmethod
    def _flatten_alpha_to_white(
        image: Image.Image,
    ) -> Image.Image:

        if image.mode in {
            "RGBA",
            "LA",
        }:

            rgba = image.convert(
                "RGBA"
            )

            background = Image.new(
                "RGBA",
                rgba.size,
                (
                    255,
                    255,
                    255,
                    255,
                ),
            )

            composed = Image.alpha_composite(
                background,
                rgba,
            )

            return composed.convert(
                "RGB"
            )

        if (
            image.mode
            == "P"
            and "transparency"
            in image.info
        ):

            rgba = image.convert(
                "RGBA"
            )

            background = Image.new(
                "RGBA",
                rgba.size,
                (
                    255,
                    255,
                    255,
                    255,
                ),
            )

            return Image.alpha_composite(
                background,
                rgba,
            ).convert(
                "RGB"
            )

        return image.convert(
            "RGB"
        )

    def _resolve_upscale_factor(
        self,
        *,
        width: int,
        height: int,
    ) -> float:

        if not self.auto_upscale:

            return 1.0

        short_side = min(
            width,
            height,
        )

        long_side = max(
            width,
            height,
        )

        if (
            short_side
            < self.small_image_short_side
        ):

            requested = (
                self.small_image_scale
            )

        elif (
            short_side
            < self.medium_image_short_side
        ):

            requested = (
                self.medium_image_scale
            )

        else:

            requested = 1.0

        if requested <= 1.0:

            return 1.0

        maximum_scale = (
            self.maximum_upscaled_side
            / long_side
            if long_side
            > 0
            else 1.0
        )

        return max(
            1.0,
            min(
                requested,
                maximum_scale,
            ),
        )

    # ==================================================
    # Reading Order
    # ==================================================

    def _sort_records_reading_order(
        self,
        records: list[
            dict[str, Any]
        ],
    ) -> list[
        dict[str, Any]
    ]:
        """
        Cluster OCR boxes into visual text lines.

        Plain:
            sort(top, left)

        is unstable when two boxes are visually on the same row but their
        top coordinates differ by a few pixels. This method uses median box
        height and center-Y clustering, then left-to-right ordering.
        """

        if not records:

            return []

        positioned = [
            record
            for record in records
            if math.isfinite(
                float(
                    record.get(
                        "center_y",
                        float(
                            "inf"
                        ),
                    )
                )
            )
            and math.isfinite(
                float(
                    record.get(
                        "left",
                        float(
                            "inf"
                        ),
                    )
                )
            )
        ]

        unpositioned = [
            record
            for record in records
            if record
            not in positioned
        ]

        if not positioned:

            return sorted(
                records,
                key=lambda item: (
                    item[
                        "source_index"
                    ],
                ),
            )

        heights = [
            max(
                float(
                    record.get(
                        "height",
                        0.0,
                    )
                    or 0.0
                ),
                1.0,
            )
            for record in positioned
        ]

        median_height = (
            statistics.median(
                heights
            )
        )

        tolerance = max(
            2.0,
            median_height
            * self.reading_order_y_tolerance_ratio,
        )

        positioned.sort(
            key=lambda item: (
                float(
                    item[
                        "center_y"
                    ]
                ),
                float(
                    item[
                        "left"
                    ]
                ),
                int(
                    item[
                        "source_index"
                    ]
                ),
            )
        )

        lines: list[
            dict[str, Any]
        ] = []

        for record in positioned:

            center_y = float(
                record[
                    "center_y"
                ]
            )

            best_line: (
                dict[str, Any]
                | None
            ) = None

            best_distance = (
                float(
                    "inf"
                )
            )

            # Only compare to recently created visual lines.
            for line in reversed(
                lines[
                    -4:
                ]
            ):

                distance = abs(
                    center_y
                    - float(
                        line[
                            "center_y"
                        ]
                    )
                )

                if (
                    distance
                    <= tolerance
                    and distance
                    < best_distance
                ):

                    best_line = line
                    best_distance = (
                        distance
                    )

            if best_line is None:

                lines.append(
                    {
                        "center_y": (
                            center_y
                        ),
                        "records": [
                            record
                        ],
                    }
                )

                continue

            best_line[
                "records"
            ].append(
                record
            )

            best_line[
                "center_y"
            ] = statistics.fmean(
                float(
                    item[
                        "center_y"
                    ]
                )
                for item
                in best_line[
                    "records"
                ]
            )

        lines.sort(
            key=lambda item: (
                float(
                    item[
                        "center_y"
                    ]
                ),
            )
        )

        ordered: list[
            dict[str, Any]
        ] = []

        for line_index, line in enumerate(
            lines
        ):

            line_records = sorted(
                line[
                    "records"
                ],
                key=lambda item: (
                    float(
                        item[
                            "left"
                        ]
                    ),
                    float(
                        item[
                            "top"
                        ]
                    ),
                    int(
                        item[
                            "source_index"
                        ]
                    ),
                ),
            )

            for record in line_records:

                record[
                    "reading_order_line_index"
                ] = line_index

                ordered.append(
                    record
                )

        for record in sorted(
            unpositioned,
            key=lambda item: (
                int(
                    item[
                        "source_index"
                    ]
                ),
            ),
        ):

            record[
                "reading_order_line_index"
            ] = None

            ordered.append(
                record
            )

        return ordered

    # ==================================================
    # OCR Box
    # ==================================================

    @staticmethod
    def _normalize_box_to_original_coordinates(
        box: Any,
        *,
        scale: float,
    ) -> Any:

        if not box:

            return box

        if (
            scale
            <= 0
            or abs(
                scale
                - 1.0
            )
            < 1e-9
        ):

            return box

        normalized: list[
            list[float]
        ] = []

        try:

            for point in box:

                if (
                    point is None
                    or len(
                        point
                    )
                    < 2
                ):

                    continue

                normalized.append(
                    [
                        float(
                            point[
                                0
                            ]
                        )
                        / scale,
                        float(
                            point[
                                1
                            ]
                        )
                        / scale,
                    ]
                )

        except Exception:

            return box

        return (
            normalized
            if normalized
            else box
        )

    @staticmethod
    def _box_geometry(
        box: Any,
    ) -> dict[
        str,
        float,
    ]:

        if not box:

            return {
                "left": (
                    float(
                        "inf"
                    )
                ),
                "top": (
                    float(
                        "inf"
                    )
                ),
                "right": (
                    float(
                        "inf"
                    )
                ),
                "bottom": (
                    float(
                        "inf"
                    )
                ),
                "width": 0.0,
                "height": 0.0,
                "center_y": (
                    float(
                        "inf"
                    )
                ),
            }

        try:

            points = [
                (
                    float(
                        point[
                            0
                        ]
                    ),
                    float(
                        point[
                            1
                        ]
                    ),
                )
                for point in box
                if (
                    point is not None
                    and len(
                        point
                    )
                    >= 2
                )
            ]

        except Exception:

            points = []

        if not points:

            return {
                "left": (
                    float(
                        "inf"
                    )
                ),
                "top": (
                    float(
                        "inf"
                    )
                ),
                "right": (
                    float(
                        "inf"
                    )
                ),
                "bottom": (
                    float(
                        "inf"
                    )
                ),
                "width": 0.0,
                "height": 0.0,
                "center_y": (
                    float(
                        "inf"
                    )
                ),
            }

        left = min(
            point[
                0
            ]
            for point in points
        )

        right = max(
            point[
                0
            ]
            for point in points
        )

        top = min(
            point[
                1
            ]
            for point in points
        )

        bottom = max(
            point[
                1
            ]
            for point in points
        )

        width = max(
            0.0,
            right
            - left,
        )

        height = max(
            0.0,
            bottom
            - top,
        )

        return {
            "left": (
                left
            ),
            "top": (
                top
            ),
            "right": (
                right
            ),
            "bottom": (
                bottom
            ),
            "width": (
                width
            ),
            "height": (
                height
            ),
            "center_y": (
                (
                    top
                    + bottom
                )
                / 2.0
            ),
        }

    # Backward-compatible private helper.
    @classmethod
    def _box_sort_position(
        cls,
        box: Any,
    ) -> tuple[
        float,
        float,
    ]:

        geometry = (
            cls._box_geometry(
                box
            )
        )

        return (
            geometry[
                "top"
            ],
            geometry[
                "left"
            ],
        )

    # ==================================================
    # OCR Engine
    # ==================================================

    def _get_ocr_engine(
        self,
    ):

        if self._engine is not None:

            return (
                self._engine
            )

        try:

            from rapidocr import RapidOCR

        except ImportError as exc:

            raise ImageLoaderError(
                "Image OCR dependency is not installed. "
                "Install it with: "
                "python -m pip install "
                "rapidocr==3.9.2 onnxruntime"
            ) from exc

        try:

            self._engine = (
                RapidOCR()
            )

        except Exception as exc:

            raise ImageLoaderError(
                "RapidOCR initialization failed: "
                f"{exc}"
            ) from exc

        return (
            self._engine
        )

    # ==================================================
    # Oversized Image Resilience
    # ==================================================

    @staticmethod
    def _is_decompression_bomb_error(
        exc: BaseException,
    ) -> bool:
        """Return True only for Pillow decompression-bomb protection.

        Pillow intentionally rejects extremely large raster images before full
        decode.  That security boundary must not be globally disabled by the
        ingestion process.  Instead, oversized images are retained as auditable
        metadata-only records and excluded from Current Qdrant.
        """

        current: BaseException | None = exc
        visited: set[int] = set()

        while current is not None and id(current) not in visited:
            visited.add(id(current))

            bomb_error_type = getattr(
                Image,
                "DecompressionBombError",
                None,
            )

            if (
                bomb_error_type is not None
                and isinstance(current, bomb_error_type)
            ):
                return True

            message = str(current).lower()
            if (
                "decompression bomb" in message
                or (
                    "image size (" in message
                    and "pixels) exceeds limit" in message
                )
            ):
                return True

            current = (
                current.__cause__
                or current.__context__
            )

        return False

    @staticmethod
    def _extract_reported_pixel_count(
        exc: BaseException,
    ) -> int | None:
        import re

        current: BaseException | None = exc
        visited: set[int] = set()

        while current is not None and id(current) not in visited:
            visited.add(id(current))
            match = re.search(
                r"Image size \((\d+) pixels\)",
                str(current),
                flags=re.IGNORECASE,
            )
            if match is not None:
                try:
                    return int(match.group(1))
                except (TypeError, ValueError):
                    return None

            current = (
                current.__cause__
                or current.__context__
            )

        return None

    def _build_oversized_image_document(
        self,
        *,
        path: Path,
        error: BaseException,
    ) -> Document:
        """Persist a Pillow-rejected raster as metadata-only, not Failed.

        This deliberately does *not* set ``Image.MAX_IMAGE_PIXELS = None``.
        The original file remains available for audit/review, while OCR is
        skipped so a very large or malicious raster cannot cause an unsafe
        allocation in Pillow/RapidOCR.
        """

        file_type = (
            path.suffix.lower()
            .lstrip(".")
        )
        pixel_count = self._extract_reported_pixel_count(
            error
        )

        warning_message = (
            "OCR skipped because Pillow rejected the raster as an oversized "
            "image under decompression-bomb protection. The source file was "
            "retained as metadata-only and excluded from Current Qdrant. "
            "Downsample the image explicitly before OCR if searchable text "
            "from this raster is required."
        )

        return Document(
            file_name=path.name,
            file_type=file_type,
            pages=[
                Page(
                    page_number=1,
                    text="",
                )
            ],
            blocks=[],
            chapters=[],
            sections=[],
            contents=[],
            metadata={
                "source_format": file_type,
                "loader": "ImageLoader",
                "loader_status": "WARNING_OCR_SKIPPED",
                "loader_warning_code": "IMAGE_OVERSIZED_OCR_SKIPPED",
                "loader_warning_message": warning_message,
                "image_oversized": True,
                "image_pixel_count": pixel_count,
                "image_size_bytes": path.stat().st_size,
                "ocr_engine": "RapidOCR",
                "ocr_attempted": False,
                "ocr_skip_reason": "PILLOW_DECOMPRESSION_BOMB_PROTECTION",
                "ocr_line_count": 0,
                "ocr_character_count": 0,
                "character_count": 0,
            },
        )

    # ==================================================
    # Image Metadata
    # ==================================================

    @staticmethod
    def _read_image_metadata(
        path: Path,
    ) -> tuple[
        int,
        int,
        str,
    ]:

        try:

            with Image.open(
                path
            ) as image:

                width = int(
                    image.width
                )

                height = int(
                    image.height
                )

                mode = str(
                    image.mode
                )

        except Exception as exc:

            raise ImageLoaderError(
                f"Invalid or unreadable image: "
                f"{exc}"
            ) from exc

        return (
            width,
            height,
            mode,
        )

    # ==================================================
    # Validation
    # ==================================================

    @classmethod
    def _validate_input_path(
        cls,
        file_path: str | Path,
    ) -> Path:

        path = Path(
            file_path
        ).expanduser()

        if not path.exists():

            raise FileNotFoundError(
                f"Image file not found: "
                f"{path}"
            )

        if not path.is_file():

            raise IsADirectoryError(
                f"Input path is not a file: "
                f"{path}"
            )

        if (
            path.suffix.lower()
            not in cls.SUPPORTED_EXTENSIONS
        ):

            raise ValueError(
                "ImageLoader only accepts "
                ".png/.jpg/.jpeg files. "
                f"Received: "
                f"{path.suffix or '<no extension>'}"
            )

        return (
            path
        )
