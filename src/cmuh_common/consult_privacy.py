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
    text = re.sub(r"((?:病人姓名|患者姓名|病歷號碼?|病歷編號|MRN|Chart\s*(?:No\.?|Number))\s*[:：=]\s*)[^\s，,；;\n]+",
                  r"\1[已隱藏]", text, flags=re.IGNORECASE)
    return text


def safe_entries(entries, labels, parser, *, identities=None,
                 roster_complete=True):
    parsed = [parser(raw) for raw in (labels or [])]
    reliable = (roster_complete
                and len(parsed) >= len(entries)
                and all(p and p.get("name") and p.get("chart") for p in parsed))
    privacy_identities = [p for p in parsed if p]
    for identity in identities or ():
        if identity and identity not in privacy_identities:
            privacy_identities.append(identity)
    reliable = reliable and all(
        p.get("name") and p.get("chart") for p in privacy_identities)
    out = []
    for panes in entries:
        if not any(str(text or "").strip() for _, text in panes):
            out.append([])
        elif not reliable:
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
