from __future__ import annotations

import re
from collections.abc import Mapping

from app.model.document import Document


class TitleSentenceCorrector:
    """
    企业级 Chapter / Section 标题文本修正器。

    负责：
        - Unicode 标准化后的标题空格修正
        - 标点和括号周围空格规范化
        - 基于可配置词典执行确定性修正
        - 安全拆分普通 CamelCase
        - 保护技术缩写、协议名、产品名和标识符
        - 同时处理 Chapter / Section 标题
        - 输出修正统计与策略 Metadata

    不负责：
        - Heading 合并
        - 标题编号生成
        - Chapter / Section 层级判断
        - OCR 修复
        - 恢复缺失字符
        - 根据上下文猜测原词
        - 翻译标题

    设计原则：
        1. 保守修正。
        2. 只执行确定性高的转换。
        3. 技术 Token 优先保护。
        4. 显式 replacement 拥有最高优先级。
        5. 不尝试修复无法可靠推断的缺字文本。

    Example:

        g RPC Workflow
            ->
        gRPC Workflow

        AuthorityManagement
            ->
        Authority Management

    但不会把：

        Reet of profile to actory efault

    擅自猜成：

        Reset of profile to factory default

    因为那属于不可可靠推断的文本恢复。
    """

    # ==================================================
    # Deterministic Replacements
    # ==================================================

    DEFAULT_REPLACEMENTS: dict[
        str,
        str,
    ] = {
        "AuthorityManagement": (
            "Authority Management"
        ),

        "Function Overvieｗ": (
            "Function Overview"
        ),

        "Detaile Specification": (
            "Detailed Specification"
        ),

        "Car Play": (
            "CarPlay"
        ),

        "CenterComm.": (
            "Center Communication"
        ),

        "User profile Receive function": (
            "User Profile Receive Function"
        ),

        "User profile Management": (
            "User Profile Management"
        ),

        "User profile Upload/Download": (
            "User Profile Upload/Download"
        ),

        "MM Settings store and load function": (
            "MM Settings Store and Load Function"
        ),

        # ==============================================
        # Technical Token Repairs
        # ==============================================

        "g RPC": (
            "gRPC"
        ),

        "G RPC": (
            "gRPC"
        ),

        "Open ID Connect": (
            "OpenID Connect"
        ),

        "OAuth 2": (
            "OAuth2"
        ),

        "OAuth 2.0": (
            "OAuth2.0"
        ),

        "i AP": (
            "iAP"
        ),

        "i AP2": (
            "iAP2"
        ),

        "i OS": (
            "iOS"
        ),

        "i Beacon": (
            "iBeacon"
        ),
    }

    # ==================================================
    # Spacing / Punctuation
    # ==================================================

    _MULTIPLE_SPACES_PATTERN = re.compile(
        r"[ \t]+"
    )

    _SPACE_BEFORE_PUNCTUATION_PATTERN = re.compile(
        r"\s+([,.;:!?，。；：！？])"
    )

    _SPACE_AFTER_OPENING_BRACKET_PATTERN = re.compile(
        r"([\(\[\{（［｛])\s+"
    )

    _SPACE_BEFORE_CLOSING_BRACKET_PATTERN = re.compile(
        r"\s+([\)\]\}）］｝])"
    )

    _SLASH_SPACING_PATTERN = re.compile(
        r"\s*/\s*"
    )

    _HYPHEN_SPACING_PATTERN = re.compile(
        r"\s*-\s*"
    )

    # ==================================================
    # CamelCase
    # ==================================================

    # fooBar -> foo Bar
    _CAMEL_BOUNDARY_LOWER_UPPER_PATTERN = re.compile(
        r"(?<=[a-z])(?=[A-Z])"
    )

    # HTTPServer -> HTTP Server
    _CAMEL_BOUNDARY_ACRONYM_WORD_PATTERN = re.compile(
        r"(?<=[A-Z])(?=[A-Z][a-z])"
    )

    _LETTER_NUMBER_BOUNDARY_PATTERN = re.compile(
        r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])"
    )

    # ==================================================
    # Technical Token
    # ==================================================

    _TECHNICAL_TOKEN_PATTERN = re.compile(
        r"""
        ^
        (?:
            [A-Z]{2,}[A-Za-z0-9_-]*
            |
            [a-z][A-Z]{2,}[A-Za-z0-9_-]*
            |
            [a-z]{1,3}[A-Z][A-Za-z0-9_-]*\d*
            |
            [A-Za-z]+\d+[A-Za-z0-9_.-]*
        )
        $
        """,
        re.VERBOSE,
    )

    DEFAULT_PROTECTED_TOKENS: frozenset[
        str
    ] = frozenset(
        {
            "API",
            "BLE",
            "CAN",
            "DCM",
            "ECU",
            "HMI",
            "HTTP",
            "HTTP2",
            "HTTPS",
            "JSON",
            "JWT",
            "REST",
            "RPC",
            "SDK",
            "SPP",
            "TLS",
            "UUID",
            "XML",

            "gRPC",
            "OAuth",
            "OAuth2",
            "OAuth2.0",
            "OpenID",
            "OpenIDConnect",

            "iAP",
            "iAP2",
            "iOS",
            "iBeacon",

            "CarPlay",
            "AndroidAuto",

            "AppAuth",
            "protobuf",
        }
    )

    # ==================================================
    # Init
    # ==================================================

    def __init__(
        self,
        *,
        replacements: Mapping[
            str,
            str,
        ] | None = None,

        enable_default_replacements: bool = (
            True
        ),

        split_camel_case: bool = (
            True
        ),

        split_letter_number: bool = (
            False
        ),

        normalize_title_case: bool = (
            False
        ),

        protected_tokens: set[
            str
        ] | frozenset[
            str
        ] | None = None,
    ) -> None:

        self.split_camel_case = bool(
            split_camel_case
        )

        self.split_letter_number = bool(
            split_letter_number
        )

        self.normalize_title_case = bool(
            normalize_title_case
        )

        # ==============================================
        # Replacements
        # ==============================================

        merged_replacements: dict[
            str,
            str,
        ] = {}

        if enable_default_replacements:

            merged_replacements.update(
                self.DEFAULT_REPLACEMENTS
            )

        if replacements:

            merged_replacements.update(
                {
                    str(
                        source
                    ): str(
                        target
                    )
                    for (
                        source,
                        target,
                    )
                    in replacements.items()
                }
            )

        # 长字符串优先。
        #
        # Example:
        #
        #   OAuth 2.0
        #   OAuth 2
        #
        # 必须先匹配 OAuth 2.0。
        self.replacements = dict(
            sorted(
                merged_replacements.items(),
                key=lambda item: len(
                    item[
                        0
                    ]
                ),
                reverse=True,
            )
        )

        # ==============================================
        # Protected Tokens
        # ==============================================

        protected = set(
            self.DEFAULT_PROTECTED_TOKENS
        )

        if protected_tokens:

            protected.update(
                str(
                    token
                )
                for token
                in protected_tokens
                if str(
                    token
                ).strip()
            )

        self.protected_tokens = (
            frozenset(
                protected
            )
        )

    # ==================================================
    # Public API
    # ==================================================

    def process(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(
            document
        )

        corrected_chapter_count = 0
        corrected_section_count = 0

        replacement_count = 0

        protected_token_count = 0
        camel_split_count = 0

        # ==============================================
        # Chapter
        # ==============================================

        for chapter in (
            document.chapters
        ):

            original_jp = (
                chapter.title_jp
            )

            original_en = (
                chapter.title_en
            )

            (
                corrected_jp,
                jp_stats,
            ) = self._correct_title_with_stats(
                original_jp
                or ""
            )

            (
                corrected_en,
                en_stats,
            ) = self._correct_title_with_stats(
                original_en
                or ""
            )

            chapter.title_jp = (
                corrected_jp
                or None
            )

            chapter.title_en = (
                corrected_en
                or None
            )

            replacement_count += (
                jp_stats[
                    "replacement_count"
                ]
                + en_stats[
                    "replacement_count"
                ]
            )

            protected_token_count += (
                jp_stats[
                    "protected_token_count"
                ]
                + en_stats[
                    "protected_token_count"
                ]
            )

            camel_split_count += (
                jp_stats[
                    "camel_split_count"
                ]
                + en_stats[
                    "camel_split_count"
                ]
            )

            if (
                chapter.title_jp
                != original_jp
                or chapter.title_en
                != original_en
            ):

                corrected_chapter_count += 1

        # ==============================================
        # Section
        # ==============================================

        for section in (
            document.sections
        ):

            original_jp = (
                section.title_jp
            )

            original_en = (
                section.title_en
            )

            (
                corrected_jp,
                jp_stats,
            ) = self._correct_title_with_stats(
                original_jp
                or ""
            )

            (
                corrected_en,
                en_stats,
            ) = self._correct_title_with_stats(
                original_en
                or ""
            )

            section.title_jp = (
                corrected_jp
                or None
            )

            section.title_en = (
                corrected_en
                or None
            )

            replacement_count += (
                jp_stats[
                    "replacement_count"
                ]
                + en_stats[
                    "replacement_count"
                ]
            )

            protected_token_count += (
                jp_stats[
                    "protected_token_count"
                ]
                + en_stats[
                    "protected_token_count"
                ]
            )

            camel_split_count += (
                jp_stats[
                    "camel_split_count"
                ]
                + en_stats[
                    "camel_split_count"
                ]
            )

            if (
                section.title_jp
                != original_jp
                or section.title_en
                != original_en
            ):

                corrected_section_count += 1

        # ==============================================
        # Metadata
        # ==============================================

        document.metadata.update(
            {
                "title_sentence_corrector": (
                    "TitleSentenceCorrector"
                ),

                "title_sentence_corrector_status": (
                    "SUCCESS"
                ),

                "title_correction_strategy": (
                    "conservative_technical_token_safe"
                ),

                "corrected_chapter_count": (
                    corrected_chapter_count
                ),

                "corrected_section_count": (
                    corrected_section_count
                ),

                "title_replacement_count": (
                    replacement_count
                ),

                "title_protected_token_count": (
                    protected_token_count
                ),

                "title_camel_split_count": (
                    camel_split_count
                ),

                "title_split_camel_case": (
                    self.split_camel_case
                ),

                "title_split_letter_number": (
                    self.split_letter_number
                ),

                "title_normalize_title_case": (
                    self.normalize_title_case
                ),

                "title_protected_token_dictionary_size": (
                    len(
                        self.protected_tokens
                    )
                ),
            }
        )

        return document

    # ==================================================
    # Public Single-Title API
    # ==================================================

    def correct_title(
        self,
        text: str,
    ) -> tuple[
        str,
        int,
    ]:
        """
        保持现有公共接口兼容。

        Returns:
            corrected_text
            replacement_count
        """

        (
            corrected,
            stats,
        ) = self._correct_title_with_stats(
            text
        )

        return (
            corrected,
            stats[
                "replacement_count"
            ],
        )

    # ==================================================
    # Correct Title with Diagnostics
    # ==================================================

    def _correct_title_with_stats(
        self,
        text: str,
    ) -> tuple[
        str,
        dict[
            str,
            int,
        ],
    ]:

        if not text:

            return (
                "",
                {
                    "replacement_count": 0,
                    "protected_token_count": 0,
                    "camel_split_count": 0,
                },
            )

        if not isinstance(
            text,
            str,
        ):

            raise TypeError(
                "TitleSentenceCorrector "
                "expects title text to be a string."
            )

        corrected = (
            self._normalize_spacing(
                text
            )
        )

        # ==============================================
        # Explicit Replacement - First Pass
        # ==============================================

        (
            corrected,
            replacement_count,
        ) = self._apply_replacements(
            corrected,

            count_replacements=(
                True
            ),
        )

        # ==============================================
        # CamelCase
        # ==============================================

        protected_token_count = 0
        camel_split_count = 0

        if self.split_camel_case:

            (
                corrected,
                protected_token_count,
                camel_split_count,
            ) = self._split_camel_case_safe(
                corrected,

                split_letter_number=(
                    self.split_letter_number
                ),
            )

        corrected = (
            self._normalize_spacing(
                corrected
            )
        )

        # ==============================================
        # Optional Title Case
        # ==============================================

        if self.normalize_title_case:

            corrected = (
                self._apply_safe_title_case(
                    corrected
                )
            )

        # ==============================================
        # Explicit Replacement - Final Pass
        # ==============================================
        #
        # replacement 最终拥有最高优先级。
        #
        # 例如：
        #
        #   Car Play
        #       ↓ replacement
        #   CarPlay
        #
        # 即使中间 CamelCase 逻辑发生变化，
        # 最后一遍 replacement 仍保证目标词形。

        (
            corrected,
            _,
        ) = self._apply_replacements(
            corrected,

            count_replacements=(
                False
            ),
        )

        corrected = (
            self._normalize_spacing(
                corrected
            )
        )

        return (
            corrected,
            {
                "replacement_count": (
                    replacement_count
                ),

                "protected_token_count": (
                    protected_token_count
                ),

                "camel_split_count": (
                    camel_split_count
                ),
            },
        )

    # ==================================================
    # Apply Replacements
    # ==================================================

    def _apply_replacements(
        self,
        text: str,
        *,
        count_replacements: bool,
    ) -> tuple[
        str,
        int,
    ]:

        corrected = (
            text
        )

        replacement_count = 0

        for (
            source,
            target,
        ) in self.replacements.items():

            if source not in (
                corrected
            ):

                continue

            occurrences = (
                corrected.count(
                    source
                )
            )

            corrected = (
                corrected.replace(
                    source,
                    target,
                )
            )

            if count_replacements:

                replacement_count += (
                    occurrences
                )

        return (
            corrected,
            replacement_count,
        )

    # ==================================================
    # CamelCase Safe Split
    # ==================================================

    def _split_camel_case_safe(
        self,
        text: str,
        *,
        split_letter_number: bool = False,
    ) -> tuple[
        str,
        int,
        int,
    ]:
        """
        安全拆分普通 CamelCase。

        Example:

            AuthorityManagement
                ->
            Authority Management

            HTTPServer
                ->
            HTTP Server

        但保护：

            gRPC
            OAuth2
            OpenID
            iAP2
            iOS
            CAN1
            ECU2
        """

        pieces = re.split(
            r"(\s+)",
            text,
        )

        result: list[
            str
        ] = []

        protected_token_count = 0
        camel_split_count = 0

        for piece in pieces:

            if (
                not piece
                or piece.isspace()
            ):

                result.append(
                    piece
                )

                continue

            (
                prefix,
                core,
                suffix,
            ) = self._split_outer_punctuation(
                piece
            )

            if not core:

                result.append(
                    piece
                )

                continue

            if self._is_technical_token(
                core
            ):

                protected_token_count += 1

                transformed = (
                    core
                )

            else:

                transformed = (
                    self._CAMEL_BOUNDARY_ACRONYM_WORD_PATTERN.sub(
                        " ",
                        core,
                    )
                )

                transformed = (
                    self._CAMEL_BOUNDARY_LOWER_UPPER_PATTERN.sub(
                        " ",
                        transformed,
                    )
                )

                if split_letter_number:

                    transformed = (
                        self._LETTER_NUMBER_BOUNDARY_PATTERN.sub(
                            " ",
                            transformed,
                        )
                    )

                if transformed != core:

                    camel_split_count += 1

            result.append(
                prefix
                + transformed
                + suffix
            )

        return (
            "".join(
                result
            ),
            protected_token_count,
            camel_split_count,
        )

    # ==================================================
    # Technical Token
    # ==================================================

    def _is_technical_token(
        self,
        token: str,
    ) -> bool:

        if token in (
            self.protected_tokens
        ):

            return True

        # 全大写缩写：
        #
        #   ECU
        #   HMI
        #   API
        #
        if (
            token.isupper()
            and 2
            <= len(
                token
            )
            <= 16
        ):

            return True

        return bool(
            self._TECHNICAL_TOKEN_PATTERN.fullmatch(
                token
            )
        )

    # ==================================================
    # Split Outer Punctuation
    # ==================================================

    @staticmethod
    def _split_outer_punctuation(
        token: str,
    ) -> tuple[
        str,
        str,
        str,
    ]:
        """
        Example:

            "(AuthorityManagement)"
                ->
            "("
            "AuthorityManagement"
            ")"
        """

        match = re.match(
            r"""
            ^
            (?P<prefix>[^A-Za-z0-9]*)
            (?P<core>.*?)
            (?P<suffix>[^A-Za-z0-9]*)
            $
            """,
            token,
            re.VERBOSE,
        )

        if match is None:

            return (
                "",
                token,
                "",
            )

        return (
            match.group(
                "prefix"
            ),

            match.group(
                "core"
            ),

            match.group(
                "suffix"
            ),
        )

    # ==================================================
    # Normalize Spacing
    # ==================================================

    @classmethod
    def _normalize_spacing(
        cls,
        text: str,
    ) -> str:

        normalized = (
            text.replace(
                "\u3000",
                " ",
            )
        )

        normalized = (
            normalized.replace(
                "\xa0",
                " ",
            )
        )

        normalized = (
            cls._MULTIPLE_SPACES_PATTERN.sub(
                " ",
                normalized,
            )
        )

        normalized = (
            cls._SPACE_BEFORE_PUNCTUATION_PATTERN.sub(
                r"\1",
                normalized,
            )
        )

        normalized = (
            cls._SPACE_AFTER_OPENING_BRACKET_PATTERN.sub(
                r"\1",
                normalized,
            )
        )

        normalized = (
            cls._SPACE_BEFORE_CLOSING_BRACKET_PATTERN.sub(
                r"\1",
                normalized,
            )
        )

        normalized = (
            cls._SLASH_SPACING_PATTERN.sub(
                "/",
                normalized,
            )
        )

        normalized = (
            cls._HYPHEN_SPACING_PATTERN.sub(
                "-",
                normalized,
            )
        )

        return (
            normalized.strip()
        )

    # ==================================================
    # Safe Title Case
    # ==================================================

    def _apply_safe_title_case(
        self,
        text: str,
    ) -> str:
        """
        仅对纯英文标题执行有限 Title Case。

        技术 Token 保持原样。
        日文 / 中英文混合标题保持原样。
        """

        if not re.fullmatch(
            r"[A-Za-z0-9 /&()_\-.,:]+",
            text,
        ):

            return text

        lower_words = {
            "a",
            "an",
            "and",
            "as",
            "at",
            "by",
            "for",
            "from",
            "in",
            "of",
            "on",
            "or",
            "the",
            "to",
            "with",
        }

        words = (
            text.split()
        )

        result: list[
            str
        ] = []

        for (
            index,
            word,
        ) in enumerate(
            words
        ):

            if self._is_technical_token(
                word
            ):

                result.append(
                    word
                )

                continue

            lower_word = (
                word.lower()
            )

            if (
                index > 0
                and lower_word
                in lower_words
            ):

                result.append(
                    lower_word
                )

                continue

            result.append(
                lower_word[
                    :1
                ].upper()
                + lower_word[
                    1:
                ]
            )

        return (
            " ".join(
                result
            )
        )

    # ==================================================
    # Validation
    # ==================================================

    @staticmethod
    def _validate_document(
        document: Document,
    ) -> None:

        if document is None:

            raise ValueError(
                "Document cannot be None."
            )

        if not isinstance(
            document,
            Document,
        ):

            raise TypeError(
                "TitleSentenceCorrector expects an "
                "app.model.document.Document instance."
            )
