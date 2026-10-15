"""Curated multilingual retrieval expansions: related terms != synonyms.

Expansion never rewrites identifiers or scope, and never changes source facts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EngineeringTerm:
    canonical: str
    variants: tuple[str, ...]
    related: tuple[str, ...] = ()


TERMS = (
    EngineeringTerm("Memory ID", ("memory id", "记忆id", "记忆 id", "メモリーid", "メモリid"), ("User ID", "BT Device ID")),
    EngineeringTerm("BT Device ID", ("bt device id", "蓝牙设备id", "bluetooth device id"), ("Bluetooth identification",)),
    EngineeringTerm("Bluetooth identification", ("bt识别", "蓝牙识别", "bluetooth identification", "bluetooth recognition"), ("HFP", "BLE")),
    EngineeringTerm("User Profile", ("user profile", "用户配置", "用户档案", "ユーザープロファイル"), ("Memory ID",)),
    EngineeringTerm("HFP", ("hands-free profile", "hfp"), ("Bluetooth",)),
    EngineeringTerm("BLE", ("ble", "bluetooth low energy"), ("Bluetooth",)),
)


def expand_engineering_query(question: str, *, max_queries: int = 3) -> tuple[str, ...]:
    if not question or max_queries < 1:
        return ()
    output = [question.strip()]
    for term in TERMS:
        # Guard word boundaries for latin abbreviations such as BLE and HFP.
        matched = False
        for variant in term.variants:
            pattern = re.escape(variant)
            if re.fullmatch(r"[a-zA-Z0-9 ]+", variant):
                pattern = r"(?<![A-Za-z0-9])" + pattern + r"(?![A-Za-z0-9])"
            if re.search(pattern, question, flags=re.I):
                matched = True
                break
        if matched and term.canonical.casefold() not in question.casefold():
            output.append(f"{question.strip()} {term.canonical}")
            if len(output) >= max_queries:
                break
    return tuple(dict.fromkeys(output))[:max_queries]
