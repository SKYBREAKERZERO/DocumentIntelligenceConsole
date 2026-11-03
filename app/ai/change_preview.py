"""Bounded literal deltas of persisted SQL change rows; no LLM interpretation."""
from difflib import SequenceMatcher


def change_preview(old: str, new: str, *, groups: int = 3, chars: int = 320) -> list[str]:
    if len(old)+len(new)>2_000_000:
        return ["原文过大，在线差异预览未执行；请使用版本对比工具查看完整原文。"]
    before,after=old.splitlines(),new.splitlines()
    matcher=SequenceMatcher(None,before,after,autojunk=True)
    output=["局部字面差异预览（非完整技术结论；行号为已保存章节文本行，不是工作表行号）："]
    count=0
    def bounded(text):
        if not text:return "[此侧无文本]"
        return text if len(text)<=chars else text[:chars]+" […片段省略，需查看完整原文]"
    for kind,i,j,k,l in matcher.get_opcodes():
        if kind=="equal":continue
        if count>=groups:
            output.append("[其余差异组未展示；本预览不能作为全部变更清单]")
            break
        left,right="\n".join(before[i:j]),"\n".join(after[k:l])
        # Remove shared literal context so a large changed table row does not
        # hide its actual delta behind hundreds of unchanged characters.
        prefix=0
        while prefix<min(len(left),len(right)) and left[prefix]==right[prefix]:prefix+=1
        a,b=len(left),len(right)
        while a>prefix and b>prefix and left[a-1]==right[b-1]:a-=1;b-=1
        start=max(0,prefix-48)
        label={"insert":"新增","delete":"删除","replace":"修改"}[kind]
        output.extend((f"  {label} · 旧文本行 {i+1}-{max(i+1,j)} / 新文本行 {k+1}-{max(k+1,l)}",
                       "  旧差异片段："+bounded(left[start:min(len(left),a+48)]),
                       "  新差异片段："+bounded(right[start:min(len(right),b+48)])))
        count+=1
    if not count:output.append("两份保存文本字面相同；这不排除未记录的版面或图片差异。")
    return output
