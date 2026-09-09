from datetime import date

import pytest

from cmuh_common import uvb_dose as u

TODAY = date(2026, 9, 9)
FIRST = 'UVB: 900 mj/cm2 (12) on\u00a0 add 40 mj/cm2, MAX: 1400 mj/cm2 w3N\u00a0'
SECOND = '局部 UVB: 600 mj/cm2 (7) on (2026/9/5) add 30 mj/cm2, MAX: 1000 mj/cm2'


def test_undated_first_line_continues_to_dated_local_uvb_and_preserves_other_text():
    other = 'mtx 2# w3 on (2026/8/1)\nLab note: 135\nRemove stitches'
    text = FIRST + '\r\n' + SECOND + '\r\n' + other
    result = u.update_uvb_in_text(text, TODAY)
    assert result.action == u.UvbAction.UPDATED
    assert result.new_text == (FIRST.replace('900 mj', '940 mj').replace('(12)', '(13)') + '\r\n'
                               + SECOND.replace('600 mj', '630 mj').replace('(7)', '(8)').replace('2026/9/5', '2026/09/09')
                               + '\r\n' + other)
    assert result.last_date is None
    assert result.additional_lines_updated == 1
    assert u.uvb_updated_lines_written_back_ok(result.new_text, result.updated_uvb_lines)
    assert not u.uvb_updated_lines_written_back_ok(result.new_text.replace('630 mj', '600 mj'), result.updated_uvb_lines)
    assert not u.uvb_updated_lines_written_back_ok(result.new_text.splitlines()[0], result.updated_uvb_lines)


@pytest.mark.parametrize('last_date, expected', [('2026/9/2', 600), ('2026/8/31', 450), ('2026/8/20', 300)])
def test_secondary_uses_its_own_date_bucket(last_date, expected):
    result = u.update_uvb_in_text(FIRST + '\n' + SECOND.replace('2026/9/5', last_date), TODAY)
    assert result.action == u.UvbAction.UPDATED
    assert f'局部 UVB: {expected} mj/cm2 (8)' in result.new_text


@pytest.mark.parametrize('last_date, action', [('2026/9/9', u.UvbAction.TOO_CLOSE),
                                              ('2026/9/8', u.UvbAction.TOO_CLOSE),
                                              ('2026/9/10', u.UvbAction.SANITY_FAIL)])
def test_secondary_date_guard_prevents_combined_write(last_date, action):
    result = u.update_uvb_in_text(FIRST + '\n' + SECOND.replace('2026/9/5', last_date), TODAY)
    assert result.action == action
    assert result.new_text is None


def test_secondary_confirmation_does_not_publish_partial_first_line():
    text = FIRST + '\n' + SECOND.replace('600 mj', '1600 mj').replace('1000 mj', '1800 mj')
    result = u.update_uvb_in_text(text, TODAY)
    assert result.action == u.UvbAction.CONFIRM_NEEDED
    assert result.new_text is None
    confirmed = u.update_uvb_in_text(text, TODAY, skip_dose_sanity=True)
    assert confirmed.action == u.UvbAction.UPDATED
    assert '1630 mj' in confirmed.new_text


def test_secondary_old_history_stays_unchanged_and_does_not_hide_next_active_line():
    stale = SECOND.replace('2026/9/5', '2026/6/1')
    result = u.update_uvb_in_text(FIRST + '\n' + stale + '\n' + SECOND, TODAY)
    assert result.action == u.UvbAction.UPDATED
    assert result.new_text.splitlines()[1] == stale
    assert '630 mj' in result.new_text.splitlines()[2]
    assert result.additional_lines_updated == 1


def test_multiple_undated_lines_keep_dates_absent_and_use_their_own_caps():
    second = SECOND.replace(' on (2026/9/5)', '').replace('1000 mj', '610 mj')
    result = u.update_uvb_in_text(FIRST + '\n' + second, TODAY)
    assert result.action == u.UvbAction.UPDATED
    assert '610 mj/cm2 (8)' in result.new_text
    assert '2026/' not in result.new_text


def test_invalid_secondary_uvb_does_not_silently_leave_partial_update():
    result = u.update_uvb_in_text(FIRST + '\nUVB: 600 mj/cm2 (7) on (2026/9/5)', TODAY)
    assert result.action == u.UvbAction.PARSE_FAIL
    assert result.new_text is None


@pytest.mark.parametrize('label', ['F1', 'F2', 'F3'])
@pytest.mark.parametrize('partial_readback', [False, True])
def test_hotkey_core_writes_both_lines_and_rejects_partial_readback(monkeypatch, label, partial_readback):
    import main as m

    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(u, 'date', FixedDate)
    state = {'text': FIRST + '\n' + SECOND, 'writes': [], 'warnings': []}
    monkeypatch.setattr(m, '_find_hospital_main_window', lambda: 1)
    monkeypatch.setattr(m, '_resolve_phototherapy_disposition', lambda hwnd: (2, 'uvb'))
    monkeypatch.setattr(m, '_read_tmemo_text', lambda hwnd: state['text'])
    monkeypatch.setattr(m, 'check_stop', lambda: None)
    monkeypatch.setattr(m, '_record_his_action', lambda *args, **kwargs: None)
    monkeypatch.setattr(m, '_show_uvb_warning', lambda *args: state['warnings'].append(args))

    def write(hwnd, text):
        state['writes'].append(text)
        state['text'] = text.splitlines()[0] + '\n' + SECOND if partial_readback else text
        return True

    monkeypatch.setattr(m, '_write_tmemo_text', write)
    if label == 'F1':
        m._f1_update_uvb_dose_if_present(label)
    else:
        assert m._f23_update_uvb_dose(label) is (not partial_readback)
    assert len(state['writes']) == 1
    assert '940 mj' in state['writes'][0] and '630 mj' in state['writes'][0]
    assert bool(state['warnings']) is partial_readback
    if partial_readback:
        assert m._last_uvb_write is None
    else:
        assert m._last_uvb_write['extra_lines'] == 1


@pytest.mark.parametrize('marker', ['UVB', '局部 UVB', 'Phototherapy', 'UV'])
def test_malformed_secondary_marker_blocks_all_updates(marker):
    line = f'{marker}:已打折 700 mj/cm2 (7) on (2026/9/5) add 30 MAX: 1000'
    result = u.update_uvb_in_text(FIRST + '\n' + line, TODAY)
    assert result.action == u.UvbAction.PARSE_FAIL
    assert result.new_text is None


@pytest.mark.parametrize('position', ['before', 'after', 'both'])
def test_medication_triplets_sharing_secondary_physical_line_are_preserved(position):
    med = 'MTX (2) on (2026/9/5); '
    second = (med if position in ('before', 'both') else '') + SECOND
    second += ('; ' + med) if position in ('after', 'both') else ''
    result = u.update_uvb_in_text(FIRST + '\n' + second, TODAY)
    assert result.action == u.UvbAction.UPDATED
    updated = result.new_text.splitlines()[1]
    assert updated.count(med) == second.count(med)
    assert '局部 UVB: 630 mj/cm2 (8) on (2026/09/09)' in updated


def test_all_secondary_dose_confirmations_are_disclosed_before_global_skip():
    second = SECOND.replace('600 mj', '1600 mj').replace('1000 mj', '1900 mj')
    third = SECOND.replace('600 mj', '1700 mj').replace('1000 mj', '1900 mj')
    text = FIRST + '\n' + second + '\n' + third
    result = u.update_uvb_in_text(text, TODAY)
    assert result.action == u.UvbAction.CONFIRM_NEEDED
    assert result.new_text is None
    assert '1630' in result.confirm_reason and '1730' in result.confirm_reason
    accepted = u.update_uvb_in_text(text, TODAY, skip_dose_sanity=True)
    assert accepted.action == u.UvbAction.UPDATED
    assert accepted.additional_lines_updated == 2


def test_multiline_readback_rejects_malformed_date_in_undated_row():
    result = u.update_uvb_in_text(FIRST + '\n' + SECOND, TODAY)
    corrupted = result.new_text.replace('on\u00a0 add', 'on (2026/99/99) add')
    assert not u.uvb_updated_lines_written_back_ok(corrupted, result.updated_uvb_lines)


def test_chinese_secondary_uvb_marker_blocks_malformed_active_record():
    result = u.update_uvb_in_text(FIRST + '\n紫外線:已打折 700 mj/cm2 (7) on (2026/9/5) add 30 MAX:1000', TODAY)
    assert result.action == u.UvbAction.PARSE_FAIL
    assert result.new_text is None


@pytest.mark.parametrize('suffix', [
    '; UVB: 400 mj/cm2 (4) on (2026/9/5) add 20 MAX:800',
    '; excimer light 400 mj/cm2 (4) on (2026/9/5) add 20 MAX:800',
    ' / new for lower legs 1000mj/cm2 (4) on (2026/9/5)',
])
def test_secondary_preserves_supported_same_line_photo_updates(suffix):
    line = SECOND + suffix
    standalone = u.update_uvb_in_text(line, TODAY)
    assert standalone.action == u.UvbAction.UPDATED
    result = u.update_uvb_in_text(FIRST + '\n' + line, TODAY)
    assert result.action == u.UvbAction.UPDATED
    assert result.new_text.splitlines()[1] == standalone.new_text
    assert '(4)' not in result.new_text.splitlines()[1]
    # Restoring only the continuation must fail read-back even when primary dose is right.
    corrupted = result.new_text.rsplit('(5)', 1)
    assert len(corrupted) == 2
    assert not u.uvb_updated_lines_written_back_ok('(4)'.join(corrupted), result.updated_uvb_lines)


def test_secondary_other_date_medication_is_not_offered_as_uncertain_photo():
    result = u.update_uvb_in_text(FIRST + '\n' + SECOND + '; MTX (2) on (2026/9/4)', TODAY)
    assert result.action == u.UvbAction.UPDATED
    assert 'MTX (2) on (2026/9/4)' in result.new_text
    assert not result.uncertain_other_triplets
