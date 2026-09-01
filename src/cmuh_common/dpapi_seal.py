# -*- coding: utf-8 -*-
"""DPAPI 靜態加密的小封套(Windows 專用)。

★[外審第二輪 R2-P2-05] DeliveryLedger 的 body_text 是臨床信件內文(PHI:
病人清單/會診內容),以明文躺在 settings/ 的 SQLite 裡最長 3 天★。
這個模組提供「封存字串 ↔ 還原字串」兩個函式,底層走 Windows DPAPI
(`CryptProtectData`),金鑰由作業系統管理、不落在 repo 或設定檔。

設計取捨(都是明寫的決定,不是巧合):

* ★machine 範圍(`CRYPTPROTECT_LOCAL_MACHINE`)★:這本帳由主程式與
  會診程式【跨 process 共用】,兩者不保證跑在同一個 Windows 帳號下
  (排程/服務情境);user 範圍會讓另一個帳號解不開 → 補寄鏈整條卡死。
  威脅模型是「settings/ 被複製離機」(備份/誤傳)——machine 範圍
  在離機情境下一樣解不開,涵蓋主要威脅;同機其他使用者可解是已知
  且接受的邊界(同機已能直接操作 HIS)。
* ★格式★:`dpapi1:<base64(blob)>`。前綴讓「還沒加密的舊列」與
  「已加密的新列」在同一張表裡共存:沒有前綴=舊明文,原樣返回
  (不強制遷移 —— 舊列最長 3 天就被 scrub,自然汰換)。
* ★空字串永遠是空字串★:`body_text == ""` 在帳本裡是狀態訊號
  (「補寄鏈已關」),加密不可以改變它的空/非空語意。
* ★只實作 Windows★(與 delivery_ledger 的鎖同一個理由):本產品只跑
  在 Windows,CI 也是;永遠不會執行的 POSIX 分支只是假的周全。

失敗方向(呼叫端據此決策,見 delivery_ledger._new_rec):
* `seal_text` 失敗 → 拋例外,呼叫端決定(帳本選「不落地內文」——
  信照寄、只是查無後的自動補寄退化成告警請人工轉寄;★絕不★
  靜默改存明文,那會讓這層防護在最需要它的時候消失)。
* `unseal_text` 失敗 → 回 (\"\", False),呼叫端必須把 False 當
  「讀不出來」處理,不可以當「本來就是空的」。
"""
from __future__ import annotations

import base64

_PREFIX = "dpapi1:"
# 綁定用途的固定 entropy:換個用途(如未來 #23 帳密)請用不同常數,
# 讓 A 用途的密文不能直接餵給 B 用途解。
_ENTROPY = b"CMUHdermatology.delivery_ledger.body.v1"


def _protect(data: bytes) -> bytes:
    """CryptProtectData(machine 範圍)。失敗拋例外。"""
    import win32crypt  # noqa: PLC0415  (Windows-only,見模組 docstring)
    # CRYPTPROTECT_LOCAL_MACHINE = 0x4
    return win32crypt.CryptProtectData(data, None, _ENTROPY, None, None, 0x4)


def _unprotect(blob: bytes) -> bytes:
    """CryptUnprotectData。失敗拋例外。回傳 (desc, data) 的 data。"""
    import win32crypt  # noqa: PLC0415
    _desc, data = win32crypt.CryptUnprotectData(blob, _ENTROPY, None, None, 0x4)
    return data


def is_sealed(text: str) -> bool:
    return str(text or "").startswith(_PREFIX)


def seal_text(text: str) -> str:
    """明文 → `dpapi1:<base64>`。空字串原樣回(空/非空語意不可變)。

    失敗★拋例外★ —— 呼叫端必須自己決定失敗方向,這裡不可以
    靜默退回明文(守衛不能有 no-op 的失效模式)。
    """
    s = str(text or "")
    if not s:
        return ""
    blob = _protect(s.encode("utf-8"))
    return _PREFIX + base64.b64encode(bytes(blob)).decode("ascii")


def unseal_text(stored: str) -> "tuple[str, bool]":
    """帳上存的值 → (明文, 讀得出來嗎)。

    * 空字串 → ("", True):本來就沒有內容,不是失敗。
    * 沒有前綴 → 原樣 (stored, True):加密上線前寫入的舊明文列。
    * 有前綴但解不開(離機複製/DPAPI 金鑰換了/密文毀損)→ ("", False):
      ★False 是「讀不出來」不是「沒有」★,呼叫端不可以把它當成
      鏈已關 —— 那會把一封欠著的臨床通知靜默丟掉。
    """
    s = str(stored or "")
    if not s:
        return "", True
    if not s.startswith(_PREFIX):
        return s, True
    try:
        blob = base64.b64decode(s[len(_PREFIX):].encode("ascii"),
                                validate=True)
        return _unprotect(blob).decode("utf-8"), True
    except Exception:
        return "", False
