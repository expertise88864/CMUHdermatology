# -*- coding: utf-8 -*-
"""求解輸入的識別(指紋)。★這條規則只有這一份實作★

日排班(`DaySolveInput`)與 R/VS 排班(`SolveContext`)都要回答同一個問題:
「預覽視窗裡這份結果,現在還配得上磁碟上的輸入嗎?」——
兩邊各寫一套正規化的話,遲早只有一邊被修好(而漂移的那一刻,守衛就開始
放行它本來該擋下的東西)。
"""
import dataclasses
import hashlib
import json
from datetime import date


def _norm(v):
    """把輸入正規化成可穩定序列化的形狀。

    ★不認得的型別要出聲★:靜默塞個 `repr()` 會讓指紋要嘛不穩定(每次不同 →
    永遠說過期),要嘛盲目(看不到那一欄的變化 → 這條守衛等於不存在)。
    """
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _norm(x)
                for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (set, frozenset)):
        return sorted(json.dumps(_norm(x), ensure_ascii=False, sort_keys=True)
                      for x in v)
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: _norm(getattr(v, f.name))
                for f in dataclasses.fields(v)}
    raise TypeError(
        f"求解輸入含無法正規化的型別 {type(v).__name__} —— 指紋不可以"
        f"靜默略過它(那等於這一欄的變動看不見)")


def input_fingerprint(inp) -> str:
    """這一次求解【吃到的全部輸入】的識別。內容一樣就一樣,任一項變了就不同。

    ★逐欄列舉會腐爛,所以走 dataclass 的欄位本身★:日後有人加一個新的輸入
    欄位,它自動被涵蓋,不必記得回來改這裡。
    """
    payload = json.dumps(_norm(inp), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
