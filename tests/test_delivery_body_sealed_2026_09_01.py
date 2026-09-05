# -*- coding: utf-8 -*-
"""[外審第二輪 R2-P2-05] DeliveryLedger 內文靜態加密(DPAPI)。

body_text 是臨床信件內文(PHI:病人清單/會診內容),此前以明文躺在
settings/ 的 SQLite 裡最長 3 天。本批:

* 落地前 `dpapi1:<base64(CryptProtectData)>` 封存(machine 範圍 ——
  帳本由主程式/會診程式跨 process 共用,不保證同一 Windows 帳號);
* ★空/非空語意不可變★:`body_text == ""` 在帳本裡是「補寄鏈已關」的
  狀態訊號,好幾個 SQL 判準直接看它 —— 空字串直通、非空必為非空密文;
* 讀取邊界(`get`/`_select` 系列)還原成明文;★內部寫回走原始列★
  讓密文原樣往返(在讀取邊界解密會讓寫回把明文洗回磁碟);
* 解不開 → `body_text=""` + `body_unreadable=True`:★「讀不出來」不是
  「本來就是空的」★,reconcile 把它走「明確放棄+告警請人工轉寄」的
  既有出口(否則是一條永遠卡開、無人聞問的鏈);
* 封不進 → ★不落地內文★+error(信照寄;絕不靜默退回明文 ——
  防護不可以恰好在出事時無聲消失)。
"""
from contextlib import closing
import importlib
import os
import sqlite3
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

dl = importlib.import_module("cmuh_common.delivery_ledger")
dr = importlib.import_module("cmuh_common.delivery_reconcile")
ds = importlib.import_module("cmuh_common.dpapi_seal")

_BODY = "會診清單:3F 王O明 皮膚科照會(測試內文)"


def _ledger(tmp_path):
    return dl.DeliveryLedger(path=str(tmp_path / "ledger.json"))


def _raw_body(led, did: str) -> str:
    with closing(sqlite3.connect(led.path)) as c, c:
        row = c.execute("SELECT body_text FROM deliveries WHERE delivery_id=?",
                        (did,)).fetchone()
    return "" if row is None else str(row[0])


class TestTheBodyAtRestIsSealed:
    def test_the_body_on_disk_is_not_plaintext(self, tmp_path):
        led = _ledger(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw"], subject="s",
                        message_id="<m@x>", body_text=_BODY)
        raw = _raw_body(led, did)
        assert raw.startswith("dpapi1:"), "★內文以明文落地★"
        assert "王O明" not in raw and _BODY not in raw
        # 讀取邊界還原成明文
        assert led.get(did)["body_text"] == _BODY
        assert "body_unreadable" not in led.get(did)

    def test_an_empty_body_stays_empty(self, tmp_path):
        """空/非空是狀態訊號(鏈已關的 SQL 判準直接看它),不可被加密改變。"""
        led = _ledger(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw"], subject="s",
                        message_id="<m@x>", body_text="")
        assert _raw_body(led, did) == ""
        assert led.get(did)["body_text"] == ""

    def test_a_legacy_plaintext_row_still_reads(self, tmp_path):
        """加密上線前寫入的舊列(無前綴)原樣通過,不強制遷移
        (舊列最長 3 天就被 scrub,自然汰換)。"""
        led = _ledger(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw"], subject="s",
                        message_id="<m@x>", body_text=_BODY)
        with closing(sqlite3.connect(led.path)) as c, c:
            c.execute("UPDATE deliveries SET body_text=? WHERE delivery_id=?",
                      (_BODY, did))
        rec = led.get(did)
        assert rec["body_text"] == _BODY
        assert "body_unreadable" not in rec

    def test_truncation_happens_before_sealing(self, tmp_path):
        led = _ledger(tmp_path)
        long_body = "訊" * (dl._BODY_TEXT_MAX + 500)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw"], subject="s",
                        message_id="<m@x>", body_text=long_body)
        assert led.get(did)["body_text"] == long_body[:dl._BODY_TEXT_MAX]

    def test_ciphertext_roundtrips_through_state_writes(self, tmp_path):
        """★內部寫回不解不重封★:settle(暫時被拒)會把 body 寫回 ——
        寫回後磁碟上仍是同一份可解的密文,不是明文、也不是雙重封裝。"""
        led = _ledger(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw"], subject="s",
                        message_id="<m@x>", body_text=_BODY)
        led.settle(did, refused={"a@x.tw": (421, "busy")})   # 生產寫回路徑
        raw = _raw_body(led, did)
        assert raw.startswith("dpapi1:"), "★寫回把明文洗回磁碟★"
        text, ok = ds.unseal_text(raw)
        assert ok and text == _BODY, "★寫回讓密文毀損/雙重封裝★"
        assert led.get(did)["body_text"] == _BODY

    def test_list_reads_return_plaintext_too(self, tmp_path):
        """`resends_owed`/`resend_children` 走 `_select` —— 同一個讀取邊界。"""
        led = _ledger(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw"], subject="s",
                        message_id="<m@x>", body_text=_BODY)
        led.settle(did, refused={"a@x.tw": (421, "busy")})   # → FAILED
        owed = led.resends_owed(min_age_sec=-5)
        assert [r["delivery_id"] for r in owed] == [did]
        assert owed[0]["body_text"] == _BODY


class TestSealFailureDropsTheBodyNotTheMail:
    def test_seal_failure_means_no_body_on_disk(self, tmp_path, monkeypatch,
                                                caplog):
        """封不進 → 本筆★不落地內文★+error;begin 照樣成功(信照寄)。
        ★絕不靜默存明文★ —— 那會讓這層防護在最需要時無聲消失。"""
        led = _ledger(tmp_path)
        monkeypatch.setattr(
            ds, "_protect",
            lambda b: (_ for _ in ()).throw(OSError("CryptProtectData 失敗")))
        with caplog.at_level("ERROR"):
            did = led.begin(business_key="bk", category="consult",
                            recipients=["a@x.tw"], subject="s",
                            message_id="<m@x>", body_text=_BODY)
        assert did, "加密失敗不可以擋掉臨床通知本身"
        assert _raw_body(led, did) == "", "★加密失敗卻仍把明文落地★"
        assert any("加密失敗" in r.message for r in caplog.records), (
            "★內文靜默消失★ 沒有任何人知道防護退化了")


class TestUnreadableBodyIsAStateNotAnEmpty:
    def _failed_with_corrupt_body(self, tmp_path):
        led = _ledger(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw"], subject="皮膚科會診通知",
                        message_id="<m@x>", body_text=_BODY)
        led.settle(did, refused={"a@x.tw": (421, "busy")})   # → FAILED 欠補寄
        # 生產失敗形狀:密文在、解不開(離機複製/金鑰換了/毀損)
        with closing(sqlite3.connect(led.path)) as c, c:
            c.execute("UPDATE deliveries SET body_text=?, updated_at=?"
                      " WHERE delivery_id=?",
                      ("dpapi1:QUFBQQ==", time.time() - 7200, did))
        return led, did

    def test_get_flags_it_instead_of_pretending_empty(self, tmp_path):
        led, did = self._failed_with_corrupt_body(tmp_path)
        rec = led.get(did)
        assert rec["body_text"] == ""
        assert rec.get("body_unreadable") is True, (
            "★『讀不出來』被說成『本來就是空的』★ 消費端會把欠著的"
            "臨床通知當成鏈已關,靜默結案")

    def test_reconcile_abandons_audibly_with_an_alert(self, tmp_path):
        """★出口★:reconcile 看到 body_unreadable → 明確放棄+告警請人工
        轉寄(與額度用盡同一條路);鏈在帳上關閉,不再每輪空轉。"""
        led, did = self._failed_with_corrupt_body(tmp_path)
        alerts = []
        rc = dr.Reconciler(lambda: led,
                           missed_alert=lambda who, subj, why:
                           alerts.append((who, subj, why)))
        rec = led.resends_owed(min_age_sec=-5)[0]
        assert rec.get("body_unreadable") is True   # 前提:掃描端也看得到
        out = rc._resend_owed_one(led, rec)
        assert out == ""
        assert alerts and alerts[0][0] == ["a@x.tw"], "★沒有告警★"
        assert "解密" in alerts[0][2]
        fresh = led.get(did)
        assert fresh["recipients"]["a@x.tw"] == dl.R_PERMANENT, (
            "★沒有明確結案★ 這條鏈會永遠卡開、每輪空轉")
        assert _raw_body(led, did) == "", "放棄後 body 應一併清掉"
        # 關帳之後不再列入欠補寄
        assert led.resends_owed(min_age_sec=-5) == []

    def test_unreadable_must_not_abandon_while_a_child_is_in_flight(
            self, tmp_path):
        """★deep R1 P1★:密文解不開的當下,若有子紀錄正在寄
        (SUBMITTING/UNKNOWN),先放棄+告警=誘導人工重寄,而那封信
        可能正被子紀錄送達 —— 重複的臨床通知。unreadable 要讓路給
        既有的 in-flight 互斥,本輪只能等。"""
        led, did = self._failed_with_corrupt_body(tmp_path)
        kid = led.claim_resend_child(
            did, business_key="bk", category="consult",
            recipients=["a@x.tw"], subject="皮膚科會診通知",
            message_id="<m2@x>")
        assert kid, "前提:子紀錄建得起來(SUBMITTING=in-flight)"
        alerts = []
        rc = dr.Reconciler(lambda: led,
                           missed_alert=lambda who, subj, why:
                           alerts.append((who, subj, why)))
        rec = led.resends_owed(min_age_sec=-5)[0]
        assert rc._resend_owed_one(led, rec) == ""
        assert alerts == [], "★in-flight 中就放棄告警 → 人工重寄=重複通知★"
        assert led.get(did)["recipients"]["a@x.tw"] == dl.R_TRANSIENT, (
            "★in-flight 中就被改成永久放棄★")

    def test_unreadable_must_let_a_newer_delivered_sibling_take_over(
            self, tmp_path):
        """★deep R1 P1★:較新同 key 紀錄已把信送到 → 接手邏輯要先跑
        (回寫已送達+supersede 結鏈),不可先放棄+告警 —— 那封信
        已經送達,告警反而誘導人工重寄。"""
        led, did = self._failed_with_corrupt_body(tmp_path)
        with closing(sqlite3.connect(led.path)) as c, c:      # 讓親紀錄確定比較舊
            c.execute("UPDATE deliveries SET created_at=? WHERE delivery_id=?",
                      (time.time() - 7200, did))
        newer = led.begin(business_key="bk", category="consult",
                          recipients=["a@x.tw"], subject="皮膚科會診通知",
                          message_id="<m3@x>", body_text=_BODY)
        led.settle(newer, refused={})             # 較新紀錄已全數送達
        alerts = []
        rc = dr.Reconciler(lambda: led,
                           missed_alert=lambda who, subj, why:
                           alerts.append((who, subj, why)))
        rec = led.resends_owed(min_age_sec=-5)[0]
        assert rec["delivery_id"] == did
        assert rc._resend_owed_one(led, rec) == ""
        assert alerts == [], "★信已由較新紀錄送達,卻告警請人工轉寄★"
        fresh = led.get(did)
        assert fresh["superseded_by"] == newer, (
            "★較新紀錄沒有接手★ 這條鏈會被當成始終沒收到而告警")
        assert led.resends_owed(min_age_sec=-5) == [], "接手後鏈要關閉"
