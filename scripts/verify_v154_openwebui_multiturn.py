"""Live read-only PostgreSQL-backed chat acceptance for exact 21MM Bluetooth PDF.

No passwords, document body text, or local secrets are printed. Run on Windows.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MODEL = "document-intelligence-rag"
URL = "http://127.0.0.1:18080/v1/chat/completions"
TOKEN_PATH = Path(os.environ["LOCALAPPDATA"]) / "DocumentIntelligenceConsole" / "local_api_token.txt"
PDF = "Bluetooth Audio function spec v2.52.pdf"
CASES = (
    (f"查一下 21MM 的 {PDF}", "SPEC_LOOKUP"),
    ("Bluetooth Audio function spec v2.52", "SPEC_LOOKUP"),
    (f"{PDF}的内容按目录总结一下", "SPEC_OUTLINE"),
    ("按章节总结一下内容", "SPEC_CHAPTER_CONTENT"),
    ("总结一下", "SPEC_CHAPTER_CONTENT"),
)


def main() -> int:
    token = TOKEN_PATH.read_text(encoding="utf-8-sig").strip()
    history = []
    fails = 0
    for index, (question, expected) in enumerate(CASES, 1):
        history.append({"role": "user", "content": question})
        request = urllib.request.Request(
            URL,
            method="POST",
            data=json.dumps({"model": MODEL, "scope": "AUTO", "stream": False,
                             "messages": history}, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json; charset=utf-8"},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
            answer = str(result["choices"][0]["message"]["content"])
            route = result.get("rag", {}).get("route")
            warnings = result.get("rag", {}).get("warnings", [])
            excerpt_count = answer.count("content_id=")
            physical = PDF in answer
            blocked = any(marker in answer for marker in (
                "本次模型回答未通过企业引用完整性校验", "Local RAG is busy"
            ))
            passed = (route == expected and physical and not blocked and not warnings)
            if expected == "SPEC_CHAPTER_CONTENT" and not excerpt_count:
                # Do not misrepresent missing contents rows as a passing text summary.
                passed = False
            if not passed:
                fails += 1
            print(json.dumps({
                "case": index, "route_expected": expected, "route_actual": route,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "physical_body_matched": physical,
                "content_excerpts": excerpt_count, "answer_chars": len(answer),
                "warnings": warnings, "accepted": passed,
            }, ensure_ascii=True))
            history.append({"role": "assistant", "content": answer})
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            fails += 1
            print(json.dumps({"case": index, "accepted": False,
                              "error_type": type(exc).__name__,
                              "error": str(exc)[:120]}, ensure_ascii=True))
            history.append({"role": "assistant", "content": "[request failed]"})
    print(json.dumps({"summary": "PASS" if fails == 0 else "FAIL",
                      "failed_cases": fails, "total_cases": len(CASES)}, ensure_ascii=True))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
