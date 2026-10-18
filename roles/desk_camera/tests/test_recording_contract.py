"""Wire compatibility, metadata bounds and timestamp semantics; no camera I/O."""
import copy
import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'files'))
import recording_contract as contract

NOW = datetime.datetime(2026, 10, 17, 12, tzinfo=datetime.timezone.utc).timestamp()


def query(operation='dates', ident='a', generation='b'):
    return {'version': 1, 'operation': operation, 'workload': 'tapo_c222_163',
            'generation': generation*32, 'request_id': ident*32,
            'query': dict(cursor=None, **({'date': '20261017'} if operation == 'index' else {})),
            'deadline': NOW+30}


def result(operation='dates'):
    value = {'items': ['20261016', '20261017'], 'snapshot': 'c'*32,
             'next_cursor': None, 'observed_at': NOW, 'freshness': 'fresh',
             'complete': True, 'storage': 'ready', 'time_basis': 'camera_epoch_seconds',
             'camera_offset_seconds': 25200, 'clock_correction_seconds': 1}
    if operation == 'index':
        value.update(date='20261017', items=[{'start': int(NOW), 'end': int(NOW)+109}])
    return value


class ContractTests(unittest.TestCase):
    def test_reads_poll_and_cancel_have_fixed_versioned_bindings(self):
        for operation in ('dates', 'index'):
            value = query(operation)
            self.assertEqual(contract.request(value, NOW), value)
            for action in ('result', 'cancel'):
                command = {k: v for k, v in value.items() if k not in ('query', 'deadline')}
                command['operation'] = action
                self.assertEqual(contract.request(command, NOW), command)

    def test_request_does_not_admit_sdk_methods_handles_or_feature_toggles(self):
        for key in ('session', 'user_id', 'password', 'owner_authorized', 'method'):
            with self.subTest(key=key), self.assertRaises(contract.ContractError):
                contract.request(dict(query(), **{key: 'private'}), NOW)
        for operation in ('getUserID', 'executeFunction', 'setPrivacyMode', 'prepare'):
            with self.subTest(operation=operation), self.assertRaises(contract.ContractError):
                contract.request(dict(query(), operation=operation), NOW)

    def test_bad_identity_version_deadline_and_types_are_rejected(self):
        changes = [('version', True), ('version', 2), ('version', 1.0),
                   ('generation', '../camera'), ('request_id', 'not-hex'),
                   ('workload', 'all'), ('operation', []), ('deadline', True),
                   ('deadline', float('nan')), ('deadline', float('inf')),
                   ('deadline', NOW+30.001)]
        for key, value in changes:
            with self.subTest(key=key, value=value), self.assertRaises(contract.ContractError):
                contract.request(dict(query(), **{key: value}), NOW)

    def test_dates_cursors_and_request_bytes_are_bounded(self):
        for date in ('20260230', '2026-10-17', 20261017, '20261017'*500):
            value = query('index');value['query']['date'] = date
            with self.subTest(date=str(date)[:20]), self.assertRaises(contract.ContractError):
                contract.request(value, NOW)
        for cursor in ('next', 1, {'offset': 0}):
            value = query();value['query']['cursor'] = cursor
            with self.assertRaises(contract.ContractError):contract.request(value, NOW)

    def test_dates_and_raw_camera_seconds_preserve_offset_and_clip_identity(self):
        value = result('index');saved = copy.deepcopy(value)
        self.assertEqual(contract.page(query('index'), value, NOW), saved)
        self.assertEqual(contract.page(query(), result(), NOW)['items'], ['20261016', '20261017'])
        # Midnight camera ranges are not shifted to the host's current timezone.
        day = int(datetime.datetime(2026, 10, 17, tzinfo=datetime.timezone.utc).timestamp())
        value['items'] = [{'start': day-7*3600, 'end': day-7*3600+60}]
        self.assertEqual(contract.page(query('index'), value, NOW)['items'], value['items'])

    def test_milliseconds_bool_epochs_and_mismatched_day_are_not_silently_converted(self):
        cases = [dict(start=int(NOW)*1000, end=int(NOW)*1000+1000),
                 dict(start=True, end=int(NOW)), dict(start=int(NOW), end=int(NOW)),
                 dict(start=int(NOW), end=int(NOW)+86401)]
        for clip in cases:
            value = result('index');value['items'] = [clip]
            with self.subTest(clip=clip), self.assertRaises(contract.ContractError):
                contract.page(query('index'), value, NOW)
        value = result('index');value['date'] = '20261016'
        with self.assertRaises(contract.ContractError):contract.page(query('index'), value, NOW)

    def test_pages_require_ordered_unique_items_and_bounded_size(self):
        for items in (['20261017', '20261016'], ['20261017']*2, ['20261017']*129):
            value = result();value['items'] = items
            with self.assertRaises(contract.ContractError):contract.page(query(), value, NOW)
        value = result('index');value['items'] *= 2
        with self.assertRaises(contract.ContractError):contract.page(query('index'), value, NOW)

    def test_partial_stale_and_no_sd_results_remain_distinct_from_failure(self):
        value = result();value.update(complete=False, next_cursor='d'*32, freshness='stale', observed_at=NOW-60)
        self.assertEqual(contract.page(query(), value, NOW), value)
        value.update(complete=True)
        with self.assertRaises(contract.ContractError):contract.page(query(), value, NOW)
        value = result();value.update(items=[], storage='absent')
        self.assertEqual(contract.page(query(), value, NOW)['storage'], 'absent')
        for storage in ('unavailable', 'busy'):
            with self.assertRaises(contract.ContractError):contract.page(query(), dict(value, storage=storage), NOW)
        with self.assertRaises(contract.ContractError):contract.page(query(), dict(result(), storage='absent'), NOW)

    def test_old_observations_cannot_be_called_fresh_or_retained_indefinitely(self):
        for freshness, age in [('fresh', 31), ('stale', 301), ('fresh', -4)]:
            with self.subTest(freshness=freshness, age=age), self.assertRaises(contract.ContractError):
                contract.page(query(), dict(result(), freshness=freshness, observed_at=NOW-age), NOW)

    def test_response_is_detached_bounded_and_has_no_handle_escape_hatch(self):
        data = result();request = query()
        reply = contract.response(request, 'done', data=data, now=NOW)
        data['items'].clear();self.assertEqual(len(reply['data']['items']), 2)
        for key in ('session', 'user_id', 'player_id', 'authorization', 'url'):
            with self.assertRaises(contract.ContractError):contract.page(request, dict(result(), **{key: 'private'}), NOW)
            with self.assertRaises(contract.ContractError):contract.response(request, 'done', data=dict(result(), **{key: 'private'}), now=NOW)
        for state, kwargs in [('done', {}), ('running', {'reason': 'cancelled'}),
                              ('failed', {'reason': 'private SDK exception'}),
                              ('done', {'data': {'bytes': 'x'*contract.MAX_RESPONSE}})]:
            with self.assertRaises(contract.ContractError):contract.response(request, state, now=NOW, **kwargs)

    def test_page_and_existing_signed_nats_envelope_budgets_are_compatible(self):
        core = pathlib.Path(__file__).resolve().parents[2]/'desk_homeassistant/files'
        import importlib.util,time
        spec = importlib.util.spec_from_file_location('recording_test_protocol', core/'protocol.py')
        protocol = importlib.util.module_from_spec(spec);spec.loader.exec_module(protocol)
        value = result('index');value['items'] = [{'start': int(NOW)+i*60, 'end': int(NOW)+(i+1)*60} for i in range(128)]
        body = contract.response(query('index'), 'done', data=value, now=NOW)
        envelope = {'sender': 'deskb1x', 'recipient': 'deskb3x', 'id': 'e'*32, 'at': time.time(), 'ok': True, 'value': body}
        signed = protocol.pack(envelope, 'offline-signing-key')
        self.assertEqual(protocol.unpack(signed, 'offline-signing-key'), envelope)
        self.assertLess(len(signed), protocol.MAX)


if __name__ == '__main__':
    unittest.main()
