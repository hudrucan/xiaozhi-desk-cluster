import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('https_dns',Path(__file__).parents[1]/'files/dns_records.py')
dns=importlib.util.module_from_spec(spec);spec.loader.exec_module(dns)


class Policy:
    def __init__(self):
        self.records=[{'domain':'my-ui-edit.test','answer':'192.168.1.55'}]
        self.writes=[];self.pending=0;self.ready=True
    def __call__(self,route,value=None):
        if route=='desk_policy':
            if self.pending:self.pending-=1;return {'revision':1,'operation':'running','ready':[]}
            return {'revision':1,'operation':'idle','ready':['deskb1x','deskb2x','deskb3x'] if self.ready else []}
        if route=='rewrite/list':return copy.deepcopy(self.records)
        self.writes.append((route,copy.deepcopy(value)))
        if route=='rewrite/add':self.records.append(copy.deepcopy(value))
        elif route=='rewrite/delete':self.records.remove(value)
        else:raise AssertionError(route)
        self.pending=1
        return None


class DNS(unittest.TestCase):
    def test_pending_readback_idempotence_and_rollback_preserve_ui_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'owned.json';api=Policy()
            self.assertTrue(dns.reconcile(state,api=api,sleep=lambda _:None))
            self.assertFalse(dns.reconcile(state,api=api,sleep=lambda _:None))
            api.records.append({'domain':'concurrent-ui.test','answer':'192.168.1.44'})
            self.assertTrue(dns.reconcile(state,True,api,sleep=lambda _:None))
            self.assertEqual(len(api.records),2)
            self.assertFalse(dns.reconcile(state,True,api,sleep=lambda _:None))

    def test_conflict_checked_before_any_write_or_no_quorum(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'owned.json';api=Policy()
            api.records.append({'domain':dns.NAMES[-1],'answer':'192.168.1.55'})
            with self.assertRaises(ValueError):dns.reconcile(state,api=api,sleep=lambda _:None)
            self.assertEqual(api.writes,[])
            api=Policy();api.ready=False
            with self.assertRaises(RuntimeError):dns.reconcile(state,api=api,sleep=lambda _:None)
            self.assertEqual(api.writes,[])

    def test_existing_matching_records_are_not_claimed_for_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'owned.json';api=Policy()
            row={'domain':dns.NAMES[0],'answer':'192.168.1.186','enabled':True};api.records.append(row)
            dns.reconcile(state,api=api,sleep=lambda _:None)
            dns.reconcile(state,True,api,sleep=lambda _:None)
            self.assertIn(row,api.records)

    def test_degraded_rollback_preserves_dns_when_ownership_or_policy_is_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'owned.json';api=Policy()
            self.assertEqual(dns.rollback(state,api)['status'],'pending')
            self.assertEqual(api.writes,[])
            row={'domain':dns.NAMES[0],'answer':'192.168.1.186','enabled':True}
            state.write_text(json.dumps([row]));api.records.append(row);api.ready=False
            result=dns.rollback(state,api,sleep=lambda _:None)
            self.assertEqual(result['status'],'pending');self.assertFalse(result['changed'])
            self.assertIn(row,api.records);self.assertEqual(json.loads(state.read_text()),[row])
            self.assertEqual(api.writes,[])
            api.ready=True
            self.assertEqual(dns.rollback(state,api,sleep=lambda _:None),{'status':'complete','changed':True})
            self.assertNotIn(row,api.records)
            state.write_text(json.dumps([{'domain':'ui-owned.test','answer':'192.168.1.55'}]))
            before=list(api.writes)
            self.assertEqual(dns.rollback(state,api)['status'],'pending')
            self.assertEqual(api.writes,before)


if __name__=='__main__':unittest.main()
