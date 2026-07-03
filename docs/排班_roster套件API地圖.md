# 排班 roster 套件 — 對外 API 地圖(Session B 用)

> 用途:寫 `src/cmuh_common/roster/service.py`(引擎↔UI 黏合層)時的 API 速查,省去逐行重讀 1301 行 roster 原始碼。
> 權威文件仍是:`docs/排班程式設計文件.md`(規格) → `docs/排班程式審查與施工指南.md` §3(service.py 函式簽名)。本文件只列「已存在的公開簽名與回傳結構」,與 §3 的「要實裝什麼」互補。
> 盤點基準:HEAD `9e40098`。roster 引擎 40 測試全綠(`test_roster_core.py`+`test_roster_solve.py`)。

## 0. 開工前驗證結論(基準 9e40098)
- roster 引擎(model/storage/ledger/rules/solve_rvs/report,共 1301 行)已完成、40 測試全綠。
- `service.py` 尚未存在 → **Session B 第一件事**。`scheduler.py` 仍是 `_build_placeholder_ui` 占位。
- 所有簽名與施工指南 §3 描述一致,無飄移(逐項對照見 §5)。

## 1. ⚠️ ortools 釘選版(決定性鐵律)
`roster/__init__.py`:`ORTOOLS_PINNED_VERSION = "9.15.6755"`。CP-SAT 不同版本可能給「不同但仍合法」的解,破壞「同輸入同輸出」。跑自動排班/115-07 重現驗收前,確認實際載入的 ortools 是 **9.15.6755**;lazy 安裝走 `ensure_dependencies([("ortools==9.15.6755","ortools")])`(`cmuh_common/deps_runtime.py`)。若機器預裝別版,先對齊版本。

---

## 2. 模組 API

### model.py
- `Member(id:str, name:str, level:str="", fixed_weekday:int|None=None)` — dataclass;`from_dict(d)->Member`(**@staticmethod**);`to_dict()->dict`
- `RosterParams(weekday_point=1, weekend_point=2, holiday_point=1, duty_min=9, duty_max=11, room_capacity=2)`;`from_config(cfg)->RosterParams`
- `SolveContext` 欄位:
  - `scope:"r"|"vs"`、`year:int`、`month:int`、`members:list[Member]`
  - `holidays:set[date]`、`leaves:dict[mid,set[date]]`、`must_duty:dict[mid,set[date]]`
  - `annual_holiday:dict[date,mid]`、`locks:dict[date,mid]`、`ledger:dict[mid,float]`
  - `week_colors:dict["2026-W31","pink"|"green"]`、`prev_last_weekend:tuple(sat_date,mid)|None`
  - `boundary_fix:dict[date,mid]`、`params:RosterParams`
  - **`.prepare()` 後新增**:`days:list[date]`(升冪)、`blocks:list[DutyBlock]`;`prepare()->self`
  - 輔助:`member_ids()`、`member_by_id(mid)`、`on_leave(mid,d)->bool`、`total_points()->int`、`color_of_block(b)->str|None`
- `DutyBlock(days:list[date], kind="weekend")`,kind∈{"weekend","weekend_orphan"};property `saturday`;`color_anchor()->date`;`points(holidays,params)->int`
- 純函式:`month_dates(y,m)`、`is_weekend(d)`、`week_key(d)->"2026-W31"`、`day_point(d,holidays,params)`、`build_duty_blocks(y,m,holidays)`、`block_of_day(blocks,d)`
- 常數:`SCHEMA_VERSION=1`、`SESSIONS=("上午","下午","晚上")`、`STUDENT_SESSIONS=("上午","下午")`

### storage.py
- 例外:`FinalizedMonthError`、`NewerSchemaError`
- `RosterStorage(base_dir:str)` —— `base_dir` **必填、無預設**(建構子不做路徑查找);service/UI 呼叫端應傳 `os.path.join(get_settings_dir(),"roster")`(`cmuh_common.paths.get_settings_dir`)。自動建 `months/`
- 載入:`load_config()->dict`、`load_ledger()->{r:{mid:float}, vs:{...}, history:[...]}`、`load_week_colors()->dict`、`load_holiday_duty()->{r:{date:mid}, vs:{...}}`、`load_month(ym)->dict`(**新月預設鍵**:month/finalized/r_duty/vs_duty/leaves/must_duty/day_slots/grid_overrides/audit;`report_r`/`report_vs`/`last_weekend`/`saved_at` 由 **save 端**寫入,讀既有月檔才會有)
- 存檔:`save_config(cfg)`、`save_ledger(ledger)`、`save_week_colors(year,weeks,source="manual")`、`save_holiday_duty(table)`、`save_month(ym,data,force=False)`(檢查**磁碟上既有月檔**的 finalized:既有為 True 且非 force→拋 `FinalizedMonthError`,**非**看傳入 data 的 finalized;自動快照[時間戳含微秒+序號]+schema_version+saved_at)
- 輔助:`holidays_set()->set[date]`(r/vs 假日鍵聯集)、`prev_month_last_weekend(ym,scope)->tuple(sat,mid)|None`(讀上月 `data["last_weekend"][scope]`)
- **日期鍵**:月檔 leaves/must_duty/duty 存 ISO 字串(service 層負責 `date.fromisoformat`↔`.isoformat` 轉換);holiday_duty 由 storage 自動轉

### ledger.py
- `fair_share(total_points, n_members)->float`
- `settle_month(ledger, scope, month, points_by_person)->dict` —— 自動回滾同月同 scope 舊分錄→算新帳本→append history;就地改並回傳;公式 `new[p]=old[p]+(points_p - total/n)`
- `rollback_month(ledger,scope,month)->bool`、`reset_member(ledger,scope,member_id)`(餘額歸零、history 留)、`sync_members(ledger,scope,member_ids)`(新人補 0、移除者刪)
- history 元素:`{month, scope, deltas:{mid:delta}}`

### rules.py
- 放寬階梯常數:`L0_FULL=0`、`L1_NO_RANGE=1`(放寬 9-11)、`L2_RESERVED=2`(同 L1)、`L3_NO_COLOR=3`(停色塊,需使用者確認)
- `Precheck(severity:"error"|"warn"|"info", rule_id:str, msg:str)` — dataclass
- Rule 基底介面:`active_at(level)->bool`、`precheck(ctx)->list[Precheck]`、`apply(mc,ctx)`(硬約束)、`objective_terms(mc,ctx)->[(expr,weight)]`(soft)
- 已註冊規則(RULE_REGISTRY):`leave`(hard,both)、`directives`(hard,both,鎖定/指定/年度/跨月→x==1)、`weekend_pair`(hard,both,區塊同人)、`fixed_weekday`(hard,r)、`weekend_color`(hard,both,relax=L3)、`duty_range`(hard,r,9-11,relax=L1)、`point_balance`(soft,both)
- `run_prechecks(ctx,scope)->list[Precheck]`(core_feasibility + 各規則 precheck)
- `collect_directives(ctx)->({date:(mid,label)}, [Precheck])`,label∈{鎖定/指定/年度指定/跨月銜接}

### solve_rvs.py
- `SolveResult`:`status:"ok"|"precheck_failed"|"need_confirm_color"|"infeasible"|"error"`、`scope`、`level_used:int|None`、`level_name`、`assignments:{date:mid}`、`reasons:{date:標籤}`、`points_by_person:{mid:pts}`、`duty_counts`/`weekday_counts`/`weekend_counts`、`targets:{mid:float}`、`prechecks:[Precheck]`、`diagnosis:[人話]`、`last_weekend:{"saturday":"2026-07-04","person":"Y"}|None`(saturday 取自實際區塊的週六日期)
- `solve_duty(ctx, allow_disable_color=False)->SolveResult` —— **內部會自動 prepare**(當 `ctx.days` 為空時);流程 precheck→L0→L1→L2 逐級;全無解且非 allow→測 L3 可解則回 `need_confirm_color`;allow=True→納 L3 試解;成功自動填 last_weekend
- **決定性設定(勿改)**:`random_seed=20260702`、`num_search_workers=1`、`ortools==9.15.6755`
- `apply_boundary_from_prev(ctx)`(跨月銜接,solve_duty 內自動呼叫)

### report.py
- `build_report(ctx, result, scope_label)->str` —— **只回字串、無副作用**;monospace 四段式([輸入]→[預檢]→[過程]→[結算]/[警告])。存進 `month["report_r"/"report_vs"]` 是 `service.accept_solution` 的責任,非本函式

### __init__.py
- `ORTOOLS_PINNED_VERSION="9.15.6755"`

---

## 3. Session B 動手前的缺口 / 轉換點(API 盤點實查)

1. **`quick_validate` 的「週末成對被手動改破」檢查 → roster 套件無現成,service.py 要自己補**。
   現有 `WeekendBlockRule.precheck` 只擋「同區塊多人指定」的衝突,**不檢查「已排好的區塊被人工把某天改成別人/清空」**。service.py 的 `quick_validate` 需另加:對每個 `build_duty_blocks` 產生的 block,檢查 `month[scope+"_duty"]` 內該 block 各日期是否「同一人且無遺漏」,破了回 `warn`(不阻止儲存,設計文件 §16.4)。
2. **日期鍵轉換統一在 service 層**:`load_month` 回來的 leaves/must_duty/duty 日期鍵是 ISO 字串 → `build_context` 做 `date.fromisoformat(k)`;`accept_solution`/`set_cell` 寫回時做 `.isoformat()`。UI 一律不碰日期字串。
3. **base_dir**:`RosterStorage(os.path.join(get_settings_dir(),"roster"))`;`get_settings_dir` 在 `cmuh_common.paths`。
4. **prev_month_last_weekend 已存在**:跨月銜接靠上月檔 `last_weekend[scope]`,由 `save_month`/`accept_solution` 端寫入 → 第一次跑某月前,上月要先 accept 過才有銜接資料(否則 None,`solve_duty` 容忍)。
5. **`atomic_write_json` 回 None、失敗拋例外**(勿當 bool 用)。`solve_duty` 會自動 prepare;但 **UI 若要先讀 `ctx.days`/`ctx.blocks` 來顯示區塊,需自行先 `.prepare()`**(`apply_boundary_from_prev` 也要求已 prepare 的 ctx)。

---

## 4. service.py 方法清單(細節見施工指南 §3)
`RosterService(storage)`:`build_context(scope,ym)->SolveContext`、`run_solve(scope,ym,allow_disable_color=False)->SolveResult`、`accept_solution(scope,ym,ctx,result)`(月檔 duty→last_weekend→report→settle_month→save_ledger→save_month,鎖定格不覆蓋)、`set_cell(scope,ym,d,person,via="manual")`(改完跑 quick_validate)、`toggle_lock`、`set_leaves`、`set_must`、`finalize(ym,on)`、`quick_validate(scope,ym)->list[Precheck]`。
測試 `tests/test_roster_service.py`(全用 `tmp_path` storage):build_context 欄位齊全+ISO 轉換、accept 後月檔+ledger+last_weekend 正確、鎖定格不被覆蓋、set_cell 審計+警告、finalize 擋 save、同月二次 accept 帳本不重複累計。

---

## 5. 簽名 vs 施工指南 §3 對齊檢查(逐項)
| 項目 | §3 說法 | 實裝 | 狀態 |
|---|---|---|---|
| `build_context` | 讀 config/ledger/holiday_duty/week_colors/month → SolveContext | service.py 實裝 | ✅ 規格清晰 |
| `solve_duty` | `solve_duty(ctx, allow_disable_color?)` | 簽名一致 | ✅ |
| `accept_solution` | 5 步落地 | service.py 實裝 | ✅ 規格清晰 |
| `settle_month` | 回滾副作用 | `settle_month(ledger,scope,month,points_by_person)->dict` | ✅ |
| `load/save_ledger` | RosterStorage | 皆有 | ✅ |
| `prev_month_last_weekend` | 存否 | 有 | ✅ |
| `run_prechecks` | `run_prechecks(ctx,scope)` | 有 | ✅ |
| `quick_validate` | 需週末成對檢查 | run_prechecks 不含 block 完整性 | ⚠️ service.py 補(見 §3.1) |
| `SolveContext` 欄位 | §3 載入項 | 全有 | ✅ |
| `SolveResult.status` | ok/need_confirm/infeasible… | 5 值齊全 | ✅ |
| `Member.from_dict` | dict→Member | **@staticmethod** 有 | ✅ |
| `Precheck` | {severity,msg} | `Precheck(severity,rule_id,msg)` | ✅ |
