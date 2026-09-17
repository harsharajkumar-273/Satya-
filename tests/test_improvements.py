"""Regression coverage for strict verification and new CI features."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from models import ActionTrace, Claim, ClaimKind, Finding, FlowResult, NetworkCall, UISnapshot, Verdict
from reconcile.backend import BackendSnapshot, diff_backend
from reconcile.reconciler import reconcile
from agent.loop import poll_for_agreement, validate_polling, verify_action
from audit.middleware import VeritasAuditor
from cli import build_do, main
from report.json_report import render_json_report


def trace(method='PUT'):
    return ActionTrace('save', network_calls=[NetworkCall(method, '/api/items/1', {}, 200, {})])


def test_save_without_identifiable_effect_is_inconclusive():
    claim = Claim(ClaimKind.MUTATION, True, None, None, 'Saved!')
    assert reconcile(claim, diff_backend(BackendSnapshot({}), BackendSnapshot({})), trace()).verdict == Verdict.NO_CLAIM


def test_creation_does_not_accept_unrelated_record():
    claim = Claim(ClaimKind.CREATION, True, '1', None, 'Added!')
    diff = diff_backend(BackendSnapshot({}), BackendSnapshot.from_list([{'id': 2}]))
    assert reconcile(claim, diff, trace('POST')).verdict == Verdict.UI_LIED


def test_field_mapping_rejects_value_persisted_in_wrong_field():
    claim = Claim(ClaimKind.MUTATION, True, '1', 'new', 'Saved!', field_name='title')
    before = BackendSnapshot.from_list([{'id': 1, 'title': 'old', 'notes': 'old'}])
    wrong = BackendSnapshot.from_list([{'id': 1, 'title': 'old', 'notes': 'new'}])
    right = BackendSnapshot.from_list([{'id': 1, 'title': 'new', 'notes': 'old'}])
    assert reconcile(claim, diff_backend(before, wrong), trace()).verdict == Verdict.UI_LIED
    assert reconcile(claim, diff_backend(before, right), trace()).verdict == Verdict.AGREE


def test_snapshot_does_not_alias_mutable_backend():
    rows = [{'id': 1, 'data': {'value': 'old'}}]
    before = BackendSnapshot.from_list(rows)
    rows[0]['data']['value'] = 'new'
    assert diff_backend(before, BackendSnapshot.from_list(rows)).any_change


def test_polling_supports_websocket_and_custom_ids():
    before = BackendSnapshot.from_list([{'uuid': '1', 'title': 'old'}], id_key='uuid')
    after = BackendSnapshot.from_list([{'uuid': '1', 'title': 'new'}], id_key='uuid')
    claim = Claim(ClaimKind.MUTATION, True, '1', 'new', 'Saved!')
    initial = reconcile(claim, diff_backend(before, before), trace('WS_SEND'))
    result = poll_for_agreement(claim, trace('WS_SEND'), before, lambda: after, initial,
                                poll_timeout=.05, poll_interval=.001)
    assert result.verdict == Verdict.AGREE


@pytest.mark.parametrize('timeout,interval', [(-1, .1), (float('nan'), .1), (1, 0), (1, float('inf'))])
def test_invalid_polling_rejected(timeout, interval):
    with pytest.raises(ValueError):
        validate_polling(timeout, interval)


def test_auditor_without_reader_is_inconclusive():
    auditor = VeritasAuditor(MagicMock())
    with patch.object(auditor, 'capture_ui_snapshot', return_value=UISnapshot(None, 0, {})):
        with auditor.audit('unknown'):
            pass
    assert auditor.results[0].findings[0].verdict == Verdict.NO_CLAIM
    auditor.assert_truthful()
    with pytest.raises(AssertionError):
        auditor.assert_truthful(fail_on_inconclusive=True)


def test_auditor_retries_backend_reads():
    page = MagicMock()
    auditor = VeritasAuditor(page, backend_fetch_fn=MagicMock(side_effect=[
        [{'id': 1, 'title': 'old'}], [{'id': 1, 'title': 'old'}], [{'id': 1, 'title': 'new'}]
    ]), poll_timeout=.05, poll_interval=.001, field_name='title')
    with patch.object(auditor, 'capture_ui_snapshot', side_effect=[
        UISnapshot(None, 1, {'1': 'old'}), UISnapshot('Saved!', 1, {'1': 'new'})
    ]):
        with auditor.audit('save'):
            auditor._action_calls.append(NetworkCall('PUT', '/api/items/1', {'title': 'new'}, 200, {}))
    assert auditor.results[0].findings[0].verdict == Verdict.AGREE


def test_new_browser_actions():
    page = MagicMock()
    build_do([{'uncheck': '#done'}, {'select_option': {'selector': '#color', 'value': 'blue'}},
              {'wait_for': {'selector': '#saved', 'state': 'visible', 'timeout_ms': 1500}}])(page)
    page.uncheck.assert_called_once_with('#done')
    page.select_option.assert_called_once_with('#color', 'blue')
    page.locator.return_value.wait_for.assert_called_once_with(state='visible', timeout=1500)


def test_strict_ci_and_json_report(tmp_path):
    cfg = tmp_path / 'flows.json'
    cfg.write_text('{}')
    result = FlowResult('save', trace(), 'e', [Finding(Verdict.NO_CLAIM, 'unknown', 'e')])
    output = tmp_path / 'reports' / 'report.json'
    with patch('cli.run_flows', return_value=[result]):
        assert main(['--config', str(cfg), '--quiet']) == 0
        assert main(['--config', str(cfg), '--fail-on-inconclusive', '--json-report', str(output)]) == 1
    payload = json.loads(output.read_text())
    assert payload['schema_version'] == 1
    assert payload['summary']['inconclusive_found'] == 1
    assert payload['flows'][0]['findings'][0]['verdict'] == 'NO_CLAIM'
    assert 'network_calls' not in payload['flows'][0]


def test_bulk_change_is_inconclusive_instead_of_checking_arbitrary_row():
    from agent.claim import infer_claim
    claim = infer_claim(UISnapshot(None, 2, {'1': 'a', '2': 'b'}), UISnapshot('Deleted!', 0, {}))
    finding = reconcile(claim, diff_backend(BackendSnapshot({}), BackendSnapshot({})), trace())
    assert finding.verdict == Verdict.NO_CLAIM


def test_custom_backend_id_and_field_reach_verification():
    agent = MagicMock()
    agent.act.return_value = ActionTrace('save', network_calls=trace().network_calls,
        ui_before=UISnapshot(None, 1, {'1': 'old'}), ui_after=UISnapshot('Saved!', 1, {'1': 'new'}))
    fetch = MagicMock(side_effect=[[{'uuid': '1', 'title': 'old', 'notes': 'old'}],
                                  [{'uuid': '1', 'title': 'old', 'notes': 'new'}]])
    result = verify_action(agent, 'save', lambda p: None, backend_fetch_fn=fetch,
                           backend_id_key='uuid', field_name='title')
    assert result.findings[0].verdict == Verdict.UI_LIED
