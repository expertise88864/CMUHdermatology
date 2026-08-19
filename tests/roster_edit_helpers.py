# -*- coding: utf-8 -*-
"""測試用的編輯輔助:以【現在盤上的那一份】當基準送出編輯。

[批次RS-8] 生產的呼叫端送的是「開窗時畫面上顯示的那一份」當基準
(`baseline=`),以及鎖定類動作「想要的狀態」。測試通常是自己剛佈好資料就
接著改,所以「現在盤上的」就是它看到的那一份 —— 語意相同,只是把每個呼叫
點的樣板收在這裡。

★真正要驗「基準與盤上不一致」的情境★(他機在視窗開著時改了同一個欄位),
在 tests/test_roster_user_intent_delta_2026_08_19.py 裡直接呼叫服務層 API,
不走這些輔助 —— 否則基準永遠等於盤上,那個反例就什麼都量不到。
"""


def edit_leaves(svc, scope, ym, mid, dates):
    svc.set_leaves(scope, ym, mid, dates,
                   baseline=set(svc.get_leaves(scope, ym, mid)))


def edit_must(svc, scope, ym, mid, dates):
    ctx = svc.build_context(scope, ym)
    svc.set_must(scope, ym, mid, dates,
                 baseline=set(ctx.must_duty.get(mid) or set()))


def _session_now(svc, ym, d, session) -> dict:
    return dict((((svc.storage.load_month(ym).get("day_slots") or {})
                  .get(d.isoformat()) or {}).get(session)) or {})


def edit_day_session(svc, ym, d, session, slots) -> int:
    return svc.set_day_session(ym, d, session, slots,
                               baseline=_session_now(svc, ym, d, session))


def edit_pgy_roster(svc, ym, codes) -> None:
    """★基準要與對話框顯示的那一份一致★:`pgy_month_roster` 是 None 時,
    畫面上顯示的是 config 的 PGY 名單(「沿用」的語意),不是空清單 ——
    拿空清單當基準的話,使用者的「刪掉某人」會被讀成「沒有刪過任何人」。"""
    cur = svc.storage.load_month(ym).get("pgy_month_roster")
    if cur is None:
        cur = [str(m.get("id"))
               for m in (svc.storage.load_config().get("pgy_members") or [])]
    svc.set_pgy_month_roster(ym, codes, baseline=[str(c) for c in cur])


def edit_apply_pref(svc, ym, codes) -> None:
    svc.set_pgy_apply_pref(
        ym, codes,
        baseline=list(svc.storage.load_month(ym).get("pgy_apply_pref") or []))


def flip_day_lock(svc, ym, d, session) -> bool:
    """UI 那一側做的事:依畫面上的狀態算出【想要的狀態】再送出去。"""
    return svc.set_day_lock(ym, d, session,
                            not svc.is_day_locked(ym, d, session))


def flip_lock(svc, scope, ym, d) -> bool:
    cell = ((svc.storage.load_month(ym).get(f"{scope}_duty") or {})
            .get(d.isoformat())) or {}
    return svc.set_lock(scope, ym, d, not bool(cell.get("locked")))


def ui_flip_lock(tab, d, scope):
    """UI 右鍵選單做的事:★意圖取自畫面上顯示的那一格★,再送出目標狀態。

    選單標籤(「鎖定」/「解鎖」)與送出的目標由【同一次判讀】決定 —— 直接呼叫
    `tab._set_lock(...)` 而自己另外去讀磁碟的話,測到的就不是選單的行為。
    """
    shown = bool(tab._shown_locked.get((d.isoformat(), scope)))
    return tab._set_lock(d, scope, not shown)
