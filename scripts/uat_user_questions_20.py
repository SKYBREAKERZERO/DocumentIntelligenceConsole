"""Run twenty user-perspective chat checks against the local OpenAI API.

The token is read from the per-install local file and is never written to the
report.  Results are checkpointed after every question so CPU-only runs remain
inspectable if interrupted.
"""
from __future__ import annotations

import json
import argparse
from pathlib import Path
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "output" / "UAT_USER_QUESTIONS_20_20261102.json"
TOKEN = (Path.home() / "AppData/Local/DocumentIntelligenceConsole/local_api_token.txt").read_text(encoding="utf-8").strip()
URL = "http://127.0.0.1:18080/v1/chat/completions"


SCENARIOS = (
    ("spec-508", "查一下21MM的508式样书"),
    ("spec-508", "展开总结一下"),
    ("spec-508", "508有哪些性能要求？"),
    ("spec-508", "第一章总结一下"),
    ("spec-508", "508不同版本主要改了什么？"),
    ("spec-508", "508明确引用了哪些式样书？"),
    ("spec-508", "508和542具体哪些内容有关？"),
    ("spec-508", "把508中引用542的那部分原文和位置给我"),
    ("ticket-708", "查一下MM21PROBL-708"),
    ("ticket-708", "总结一下这个bug，不要复读整张票"),
    ("ticket-708", "为什么当时判断为仕様通り？"),
    ("ticket-708", "它关联哪些式样书和具体内容？"),
    ("ticket-708", "结合票据和式样，给我一些回归测试观点"),
    ("ticket-708", "按现在的证据，它现在还能直接认定为仕様通り吗？"),
    ("ticket-138", "查一下MM21PROBL-138"),
    ("ticket-138", "这个MiraCast问题关联哪些式样和哪些内容？"),
    ("ticket-138", "分析一下现象，再整理一份回归测试手顺"),
    ("symptom-profile", "蓝牙连接后切换用户，Profile没有恢复。历史QA/BUG里有没有类似现象？"),
    ("symptom-profile", "如果历史票不足以判断，应该优先查哪些式样的哪些部分？"),
    ("spec-150", "24MM的150是什么？请用业务语言总结，不要大段复制原文。"),
)

ROUND2_SCENARIOS = (
    ("r2-spec-508", "21MM的508式样书主要解决什么业务问题？"),
    ("r2-spec-508", "其中哪些章节和Bluetooth有关？请给位置，不要把术语表当成要求。"),
    ("r2-spec-508", "你刚才列的是明确要求，还是语义相关候选？分开说明。"),
    ("r2-rel-100", "508在哪些内容里引用了100式样书？"),
    ("r2-rel-100", "把508侧的原文、章节和content_id给我。"),
    ("r2-rel-100", "100式样中与这段最对应的是哪一段？没有目标正文就直说。"),
    ("r2-rel-542", "508和542的关系是明确章节引用、文档级引用，还是语义候选？"),
    ("r2-version", "508最新版本相对旧版本的变化，会影响Bluetooth相关流程吗？区分事实和待确认项。"),
    ("r2-ticket-708", "用5行总结MM21PROBL-708，不要复读字段。"),
    ("r2-ticket-708", "这张票明确引用了哪本式样、哪个版本、哪一页和哪一节？"),
    ("r2-ticket-708", "为什么票据当时写仕様通り？只解释历史记录，不替它背书。"),
    ("r2-ticket-708", "按当前库里的证据，能够证明这个结论吗？"),
    ("r2-ticket-708", "如果不能证明，还缺什么材料？"),
    ("r2-ticket-708", "给我回归测试建议，并把票据事实、已核验式样、候选推测分开。"),
    ("r2-ticket-138", "MM21PROBL-138的根因和对策分别是什么？"),
    ("r2-ticket-138", "这个问题明确关联和可能关联的式样分别有哪些？"),
    ("r2-ticket-138", "围绕第二次连接失败设计回归测试，覆盖正常、异常和连续操作。"),
    ("r2-symptom", "用户切换后蓝牙设备还连着，但Profile设置没有恢复。先查历史QA/BUG。"),
    ("r2-symptom", "再查相关式样正文，告诉我优先核对哪本、哪一节和哪个content_id。"),
    ("r2-symptom", "根据目前结果能判断一定是BUG吗？给出下一步最小确认步骤。"),
)


def ask(messages: list[dict[str, str]]) -> tuple[str, dict, float, str]:
    payload = json.dumps({
        "model": "document-intelligence-rag", "messages": messages,
        "stream": False, "scope": "AUTO",
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(URL, data=payload, method="POST", headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json; charset=utf-8",
    })
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            result = json.loads(response.read().decode("utf-8"))
        answer = str(result.get("choices", [{}])[0].get("message", {}).get("content") or "")
        return answer, dict(result.get("rag") or {}), time.monotonic() - started, ""
    except Exception as exc:
        detail = ""
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail = exc.read().decode("utf-8")[:1000]
            except Exception:
                detail = ""
        return "", {}, time.monotonic() - started, f"{type(exc).__name__}: {exc}; {detail}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--round2", action="store_true")
    parser.add_argument("--only", default="", help="Comma-separated 1-based question numbers")
    args = parser.parse_args()
    global REPORT
    scenarios = ROUND2_SCENARIOS if args.round2 else SCENARIOS
    if args.round2:
        REPORT = ROOT / "output" / "UAT_USER_QUESTIONS_20_ROUND2_20261102.json"
    selected={int(x) for x in args.only.split(",") if x.strip().isdigit()} if args.only else set()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    histories: dict[str, list[dict[str, str]]] = {}
    previous = {}
    if args.retry_errors and REPORT.exists():
        previous = {int(x["number"]): x for x in json.loads(REPORT.read_text(encoding="utf-8")).get("results", [])}
    results = []
    for number, (scenario, question) in enumerate(scenarios, 1):
        if selected and number not in selected:
            continue
        history = histories.setdefault(scenario, [])
        history.append({"role": "user", "content": question})
        old = previous.get(number)
        if old and not old.get("error") and old.get("answer"):
            results.append(old)
            history.append({"role": "assistant", "content": str(old["answer"])})
            print(f"[{number:02d}/{len(scenarios)}] {scenario} REUSED", flush=True)
            continue
        answer, rag, elapsed, error = ask(history)
        results.append({
            "number": number, "scenario": scenario, "question": question,
            "answer": answer, "elapsed_seconds": round(elapsed, 2), "rag": rag,
            "error": error,
        })
        if answer:
            history.append({"role": "assistant", "content": answer})
        REPORT.write_text(json.dumps({"completed": number, "total": len(scenarios), "results": results},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{number:02d}/{len(scenarios)}] {scenario} {elapsed:.1f}s {'ERROR' if error else 'OK'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
