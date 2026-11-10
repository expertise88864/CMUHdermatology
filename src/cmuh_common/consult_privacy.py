"""Fail-closed identity handling for consultation email renderers."""
import re

PRIVACY_MARKER = "[會診通知：姓名與病歷號已隱藏]"
UNPARSED = "識別資料無法可靠解析，請至 HIS 查看此筆會診。"


def scrub(text, identities):
    text = str(text or "")
    values = {str(p.get(k) or "") for p in identities for k in ("name", "chart")}
    # Some HIS panes omit the roster's zero padding. Do not turn very short
    # numeric identifiers into a global substitution of doses or dates.
    values.update(chart.lstrip("0") for p in identities
                  if (chart := str(p.get("chart") or "")).isdigit()
                  and len(chart.lstrip("0")) >= 6)
    for value in sorted(values - {""}, key=len, reverse=True):
        # HIS may insert spaces or line breaks between name/number characters.
        pattern = r"\s*".join(re.escape(c) for c in value if not c.isspace())
        if pattern:
            text = re.sub(pattern, "[已隱藏]", text, flags=re.IGNORECASE)
    # 羅馬拼音姓名的每個字(在完整姓名之後才做,否則會把完整姓名拆散):以
    #   【完整單字】遮蔽(前後不是英文字母)—— 「LI, WEI」或倒序也遮得到,又不會
    #   把 LIVER 之類的字遮掉。字母之間同樣容許 HIS 插入的空白/換行。
    words = {str(a) for p in identities for a in (p.get("aliases") or ()) if a}
    for word in sorted(words, key=len, reverse=True):
        body = r"\s*".join(re.escape(c) for c in word if not c.isspace())
        if body:
            text = re.sub(rf"(?<![A-Za-z]){body}(?![A-Za-z])", "[已隱藏]",
                          text, flags=re.IGNORECASE)
    text = re.sub(r"((?:病人姓名|患者姓名|病歷號碼?|病歷編號|MRN|Chart\s*(?:No\.?|Number))\s*[:：=]\s*)[^\s，,；;\n]+",
                  r"\1[已隱藏]", text, flags=re.IGNORECASE)
    return text


def safe_entries(entries, labels, parser, *, identities=None,
                 roster_complete=True):
    """逐筆去識別化。

    ★[2026-11-10 使用者定案] 只遮解析不出的那一筆★:每一筆只看【它自己的】
    標題能不能解析出姓名與病歷號 —— 能 → 以所有已知身分遮蔽後照常顯示;
    不能(或根本沒有標題)→ 只有那一筆顯示 UNPARSED。舊規則是「任何一列
    解析不出就整封遮蔽」:一位外籍病人就讓七筆會診內容全部看不到。
    已接受的殘餘風險(使用者知情):解析不出的那位病人若在【別筆】內文被提到,
    他的姓名無從得知、遮不到。`roster_complete` 因此不再影響顯示,保留參數
    只為呼叫端相容。"""
    del roster_complete
    parsed = [parser(raw) for raw in (labels or [])]
    privacy_identities = [p for p in parsed if p]
    for identity in identities or ():
        if identity and identity not in privacy_identities:
            privacy_identities.append(identity)
    out = []
    for index, panes in enumerate(entries):
        own = parsed[index] if index < len(parsed) else None
        if not any(str(text or "").strip() for _, text in panes):
            out.append([])
        elif not (own and own.get("name") and own.get("chart")):
            out.append([("會診內容", UNPARSED)])
        else:
            out.append([(scrub(label, privacy_identities),
                         scrub(text, privacy_identities))
                        for label, text in panes])
    return out


def safe_head(raw, parser):
    p = parser(raw)
    if not p:
        return "會診", UNPARSED
    return p["ward_bed"] or "床位未提供", "　".join(
        x for x in (p["vs"], " ".join(x for x in (p["date"], p["time"]) if x)) if x)
