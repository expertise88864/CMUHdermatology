# -*- coding: utf-8 -*-
"""排班程式 Tkinter UI（scheduler.py 用）。

分層：
- common.py  純函式（月曆佈局/月份導覽/循環切換/成員配色）+ 共用小元件
- settings.py 設定分頁（R/VS 名單、年度假日表、參數、手動週色、帳本）
- duty.py    R/VS 排班分頁（CalendarDutyTab）+ 請假/指定編輯器（LeaveEditor）

所有讀寫一律經 cmuh_common.roster.service.RosterService；UI 不直接碰
solver/storage。ortools 為重依賴，按「自動排班」時才 lazy 安裝/import。
"""
