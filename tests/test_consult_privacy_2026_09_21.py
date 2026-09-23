from cmuh_common.consult_privacy import scrub, safe_entries, UNPARSED


def test_names_numbers_spaces_and_labeled_identifiers_are_hidden():
    p = {"name": "測試甲", "chart": "9876543210"}
    result = scrub("測 試甲，9876 543210；主治：醫師乙；MRN: AB12345\n皮疹三天", [p])
    assert "測" not in result and "9876" not in result and "AB12345" not in result
    assert "主治：醫師乙" in result and "皮疹三天" in result


def test_unknown_identity_never_falls_back_to_raw_clinical_text():
    assert safe_entries([[("原因", "Unidentified Smith 9876543210")]], ["unknown"],
                        lambda _: None) == [[("會診內容", UNPARSED)]]


def test_chart_padding_variants_are_redacted():
    result = scrub("0012345678 / 12345678 / 12 345678; dose 42", [{"chart": "0012345678"}])
    assert "12345678" not in result and "345678" not in result
    assert "dose 42" in result


def test_cross_patient_mentions_are_redacted_without_mutating_input():
    identities = {"a": {"name": "測試甲", "chart": "9876543210"},
                  "b": {"name": "測試乙", "chart": "9876543211"}}
    entries = [[("原因", "測試甲與測試乙 9876543211")], [("摘要", "皮疹")]]
    result = safe_entries(entries, ["a", "b"], identities.get)
    assert "測試" not in result[0][0][1] and "9876543211" not in result[0][0][1]
    assert entries[0][0][1] == "測試甲與測試乙 9876543211"


def test_unparseable_roster_row_fail_closes_every_note_body():
    entries = [[("原因", "測試甲與未知病人 9876543212")]]
    result = safe_entries(
        entries,
        ["測試甲A1(101)9876543210"],
        lambda _raw: {"name": "測試甲", "chart": "9876543210"},
        identities=[{"name": "測試甲", "chart": "9876543210"}],
        roster_complete=False,
    )
    assert result == [[("會診內容", UNPARSED)]]


def test_html_scrubs_cross_patient_identity_before_line_break_rendering():
    import consult_query as cq

    html = cq._format_extracted_entries_html(
        [[("摘要", "曾與王\n小明共同就醫")]],
        labels=["測試甲A1(101)9876543210"],
        privacy_identities=[
            {"name": "測試甲", "chart": "9876543210"},
            {"name": "王小明", "chart": "9876543211"},
        ],
    )
    assert "王小明" not in html
    assert "王<br>小明" not in html
    assert "已隱藏" in html
