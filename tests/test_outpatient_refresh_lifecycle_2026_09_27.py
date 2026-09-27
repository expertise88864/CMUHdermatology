"""Lifecycle behavior with a fake clock and no Tk or hospital data source."""

from cmuh_common.outpatient_refresh_lifecycle import OutpatientRefreshLifecycle


def _doctor(name):
    return {"name": name, "doc_no": name}


def _lifecycle():
    now = [100.0]
    return OutpatientRefreshLifecycle(max_age_seconds=900, clock=lambda: now[0]), now


def test_duplicate_requests_and_full_partial_handoff_are_bounded():
    lifecycle, _ = _lifecycle()
    first = lifecycle.request(False)
    assert first.kind == "started"
    assert lifecycle.take_start() == first.run
    assert lifecycle.request(False).kind == "duplicate"
    assert lifecycle.request(True).kind == "queued"
    assert lifecycle.request(False, [_doctor("A")]).kind == "queued"
    assert lifecycle.request(False, [_doctor("A")]).kind == "duplicate"
    assert lifecycle.request(False, [_doctor("B")]).kind == "merged"
    assert lifecycle.request(False, [_doctor("A")]).kind == "duplicate"
    assert lifecycle.request(True, [_doctor("C")]).kind == "queued"
    assert lifecycle.queue_size == 3

    assert lifecycle.mark_finished(first.run.generation)
    assert lifecycle.pending_callback_count == 1
    # Completion waits for the UI to consume the prior generation's messages.
    assert lifecycle.accepts_message(first.run.generation)
    completed = lifecycle.finish_on_ui()
    assert completed is not None and completed.next_run is not None
    assert completed.next_run.request.manual is True
    assert completed.next_run.request.doctors is None
    assert lifecycle.take_start() == completed.next_run
    assert lifecycle.queue_size == 2


def test_stale_takeover_revokes_old_results_and_old_completion():
    lifecycle, clock = _lifecycle()
    old = lifecycle.request(False, [_doctor("A")]).run
    assert old is not None
    lifecycle.take_start()
    assert lifecycle.request(False, [_doctor("B")]).kind == "queued"
    clock[0] += 901
    new = lifecycle.request(False, [_doctor("B")]).run
    assert new is not None and new.took_over
    assert new.generation == old.generation + 1
    assert lifecycle.queue_size == 0  # takeover absorbs matching queued work
    assert not lifecycle.accepts_message(old.generation)
    assert not lifecycle.mark_finished(old.generation)
    assert lifecycle.owns(new.generation)
    assert lifecycle.mark_finished(new.generation)
    assert lifecycle.finish_on_ui().run == new


def test_a_young_worker_queues_instead_of_being_taken_over():
    lifecycle, clock = _lifecycle()
    first = lifecycle.request(False).run
    clock[0] += 30
    assert lifecycle.request(True).kind == "queued"
    assert lifecycle.generation == first.generation


def test_takeover_subtracts_overlap_from_merged_partial_queue():
    lifecycle, clock = _lifecycle()
    old = lifecycle.request(False, [_doctor("A")]).run
    assert old is not None
    lifecycle.take_start()
    assert lifecycle.request(False, [_doctor("B")]).kind == "queued"
    assert lifecycle.request(False, [_doctor("C")]).kind == "merged"
    clock[0] += 901
    taken = lifecycle.request(False, [_doctor("B")]).run
    assert taken is not None and taken.took_over
    assert lifecycle.queue_size == 1
    assert lifecycle.mark_finished(taken.generation)
    following = lifecycle.finish_on_ui().next_run
    assert following is not None
    assert [row["name"] for row in following.request.doctors] == ["C"]


def test_network_failure_or_rejected_submit_still_hands_off_once():
    lifecycle, _ = _lifecycle()
    run = lifecycle.request(False).run
    lifecycle.take_start()
    lifecycle.request(True, [_doctor("A")])
    assert lifecycle.mark_finished(run.generation, rejected=True)
    assert not lifecycle.mark_finished(run.generation, rejected=True)
    completed = lifecycle.finish_on_ui()
    assert completed.rejected
    assert completed.next_run.request.manual
    assert lifecycle.take_start() == completed.next_run


def test_stop_invalidates_result_and_clears_queue_and_callback():
    lifecycle, _ = _lifecycle()
    run = lifecycle.request(False).run
    lifecycle.take_start()
    lifecycle.request(True)
    lifecycle.mark_finished(run.generation)
    lifecycle.stop()
    assert lifecycle.stopped
    assert lifecycle.queue_size == 0
    assert lifecycle.pending_callback_count == 0
    assert not lifecycle.accepts_message(run.generation)
    assert not lifecycle.mark_finished(run.generation)
    assert lifecycle.finish_on_ui() is None
    assert lifecycle.take_start() is None
    assert lifecycle.request(True).kind == "stopped"
    submitted = []
    assert lifecycle.submit_if_current(
        run.generation, lambda: submitted.append(1)) is None
    assert submitted == []


def test_repeated_mixed_requests_do_not_accumulate_queue_or_callbacks():
    lifecycle, _ = _lifecycle()
    for _cycle in range(100):
        decision = lifecycle.request(False)
        assert decision.kind == "started"
        run = lifecycle.take_start()
        for index in range(100):
            lifecycle.request(True)
            lifecycle.request(False, [_doctor(str(index))])
            lifecycle.request(True, [_doctor(str(index))])
            lifecycle.request(False)
            assert lifecycle.queue_size <= lifecycle.MAX_QUEUED
        assert lifecycle.mark_finished(run.generation)
        assert lifecycle.pending_callback_count == 1
        completed = lifecycle.finish_on_ui()
        while completed.next_run is not None:
            run = lifecycle.take_start()
            assert run == completed.next_run
            assert lifecycle.mark_finished(run.generation)
            completed = lifecycle.finish_on_ui()
        assert lifecycle.queue_size == 0
        assert lifecycle.pending_callback_count == 0


def test_partial_payload_bound_rejects_oversized_and_merged_requests():
    lifecycle, _ = _lifecycle()
    first = lifecycle.request(False).run
    assert first is not None
    limit = lifecycle.MAX_PARTIAL_DOCTORS
    assert lifecycle.request(False, (_doctor(str(n)) for n in range(limit + 1000))).kind == "too_many_doctors"
    assert lifecycle.queue_size == 0
    assert lifecycle.request(False, [_doctor(str(n)) for n in range(limit)]).kind == "queued"
    assert lifecycle.request(False, [_doctor("extra")]).kind == "too_many_doctors"
    assert lifecycle.queue_size == 1
    assert lifecycle.mark_finished(first.generation)
    queued = lifecycle.finish_on_ui().next_run
    assert queued is not None and len(queued.request.doctors) == limit
