"""Durable shared AdGuard policy; no DNS packets, metrics, or idle disk writes."""
import base64
import copy
import hashlib
import importlib.util
from pathlib import Path
import secrets
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from cryptography.fernet import Fernet

path = Path(__file__).with_name('config-journal.py')
if not path.exists():
    path = Path(__file__).resolve().parents[2] / 'desk_monitoring/files/config_journal.py'
spec = importlib.util.spec_from_file_location('dns_configuration_journal', path)
journal_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal_module)
Journal, Failure, digest = journal_module.Journal, journal_module.Failure, journal_module.digest


class Policy:
    def __init__(self, config, api):
        self.config, self.api = config, api
        self.node = config['node']
        self.members = config['nodes']
        raw = hashlib.sha256(('desk-dns-policy-v1:' + config['password']).encode()).digest()
        self.journal = Journal(Path(config['state']).with_name('policy.fernet'),
                               Fernet(base64.urlsafe_b64encode(raw)), self.node, self.members, raw)
        self.apply_lock, self.write_lock, self.reconcile_lock = threading.Lock(), threading.Lock(), threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.error = None
        self.job_lock = threading.Lock()
        self.job = None

    def call(self, node, action, payload=None):
        if node == self.node:
            return self.peer(action, payload or {})
        public = self.api.public_nodes()
        context = ssl.create_default_context(cafile='/etc/desk-dns/sync/backend.crt')
        return self.api.fetch('https://' + public[node]['private'] + ':5443/desk-observe/policy/' + action,
            self.config, 'POST', payload or {}, context=context, return_mutation=True,
            timeout=960 if action == 'apply' else 5)

    def fanout(self, action, payload=None):
        jobs = {node:self.pool.submit(self.call, node, action, payload) for node in self.members}
        result = {}
        for node, job in jobs.items():
            try: result[node] = job.result()
            except Exception: pass
        return result

    def peer(self, action, payload):
        if action == 'jobs': return {'busy':self.api.pending_filter_job()}
        if action == 'status': return self.journal.status(True)
        if action == 'promise': return self.journal.promise(payload['revision'], payload['ballot'])
        if action == 'accept': return self.journal.accept(payload['revision'], payload['ballot'], payload['value'])
        if action == 'commit':
            self.journal.commit(payload['revision'], payload['value'], payload['certificate'])
            return self.journal.status()
        if action == 'apply':
            self.apply()
            return self.journal.status()
        raise Failure(404, 'Unknown policy action')

    def refresh(self):
        heads = self.fanout('status')
        valid = [head for head in heads.values() if head['revision'] >= 0]
        if valid:
            head = max(valid, key=lambda h:h['revision'])
            self.journal.commit(head['revision'], head['value'], head['certificate'])
        return heads

    def choose(self, wanted, expected=None):
        for _ in range(6):
            heads = self.refresh()
            if len(heads) < 2: raise Failure(503, 'Two DNS nodes are required to commit policy')
            state = self.journal.status(True)
            if expected is not None and state['revision'] != expected:
                raise Failure(409, 'Policy changed; retry against the recovered revision')
            revision = state['revision'] + 1
            ballot = [max(h['promised'][0] for h in heads.values()) + 1, self.node]
            promises = self.fanout('promise', {'revision':revision, 'ballot':ballot})
            if len(promises) < 2 or any('head' in p for p in promises.values()): continue
            accepted = [p['accepted'] for p in promises.values() if p.get('accepted')]
            value = max(accepted, key=lambda p:p['ballot'])['value'] if accepted else wanted
            receipts = self.fanout('accept', {'revision':revision, 'ballot':ballot, 'value':value})
            if len(receipts) < 2: continue
            commits = self.fanout('commit', {'revision':revision, 'value':value, 'certificate':list(receipts.values())})
            if len(commits) < 2: raise Failure(503, 'Policy chosen; commit recovery pending')
            if digest(value) != digest(wanted):
                raise Failure(409, 'Recovered an earlier accepted policy; retry save')
            return revision
        raise Failure(503, 'Policy quorum unavailable')

    def ensure(self):
        heads = self.refresh()
        if self.journal.state['revision'] < 0:
            if not self.api.owns(self.config['vip']):
                raise Failure(503, 'Initial policy must be seeded by the DNS VIP owner')
            self.choose({'policy':self.api.policy_snapshot(self.config['adguard'], self.config), 'refresh':None}, -1)
        self.apply()
        return heads

    def apply(self):
        with self.apply_lock:
            state = self.journal.status(True)
            revision, value = state['revision'], state.get('value')
            if revision < 0 or state['applied_revision'] == revision: return
            desired = value['policy']
            current = self.api.policy_snapshot(self.config['adguard'], self.config)
            def write(route, body):
                if route.startswith('filtering/'):
                    return self.api.filter_write(self.config, route, body)
                return self.api.fetch(self.config['adguard'] + '/control/' + route,
                                      self.config, self.api.WRITES[route], body, timeout=60)
            # Native APIs validate each operation. Keep partial application pending
            # and retry from actual native state; never restore stale owner snapshots.
            settings = {'querylog/config':'querylog/config/update', 'stats/config':'stats/config/update',
                        'dns_info':'dns_config', 'rewrite/settings':'rewrite/settings/update',
                        'blocked_services/get':'blocked_services/update', 'safesearch/status':'safesearch/settings', 'profile':'profile/update'}
            for read, route in settings.items():
                if current[read] != desired[read]: write(route, desired[read])
            for kind in ('parental', 'safebrowsing'):
                if current[kind+'/status'] != desired[kind+'/status']:
                    write(kind + ('/enable' if desired[kind+'/status']['enabled'] else '/disable'), {})
            if current['status'] != desired['status']:
                write('protection', {'enabled':desired['status']['protection_enabled'], 'duration':0})
            old = {self.api.encoded(x):x for x in current['rewrite/list']}
            new = {self.api.encoded(x):x for x in desired['rewrite/list']}
            for key in old.keys()-new.keys(): write('rewrite/delete', old[key])
            for key in new.keys()-old.keys(): write('rewrite/add', new[key])
            old = {x['name']:x for x in current['clients']['clients']}
            new = {x['name']:x for x in desired['clients']['clients']}
            for name in old.keys()-new.keys(): write('clients/delete', {'name':name})
            for name in new.keys()-old.keys(): write('clients/add', new[name])
            for name in new.keys() & old.keys():
                if new[name] != old[name]: write('clients/update', {'name':name, 'data':new[name]})
            old, new = current['filtering/status'], desired['filtering/status']
            for kind in ('filters', 'whitelist_filters'):
                whitelist = kind == 'whitelist_filters'
                before, after = {x['url']:x for x in old[kind]}, {x['url']:x for x in new[kind]}
                for url in before.keys()-after.keys(): write('filtering/remove_url', {'url':url, 'whitelist':whitelist})
                for url in after.keys()-before.keys():
                    write('filtering/add_url', {**after[url], 'whitelist':whitelist})
                    # add_url initializes enabled=true irrespective of caller.
                    if not after[url]['enabled']:
                        write('filtering/set_url', {'url':url, 'whitelist':whitelist, 'data':after[url]})
                for url in before.keys() & after.keys():
                    if before[url] != after[url]: write('filtering/set_url', {'url':url, 'whitelist':whitelist, 'data':after[url]})
            if old['user_rules'] != new['user_rules']: write('filtering/set_rules', {'rules':new['user_rules']})
            if (old['enabled'], old['interval']) != (new['enabled'], new['interval']):
                write('filtering/config', {'enabled':new['enabled'], 'interval':new['interval']})
            refresh = value.get('refresh')
            previous = self.journal.state.get('applied_refresh')
            if refresh and refresh['generation'] != previous:
                # The writer already refreshed its own native backend.
                if refresh['origin'] != self.node: write('filtering/refresh', refresh['body'])
            actual = self.api.policy_snapshot(self.config['adguard'], self.config)
            if digest(actual) != digest(desired): raise Failure(503, 'Native policy readback is still pending')
            if not self.api.native_ready(self.node): raise Failure(503, 'Native DNS health is pending')
            with self.journal.lock:
                self.journal.state.update(applied_revision=revision, applied_value=copy.deepcopy(value),
                                          applied_refresh=refresh['generation'] if refresh else None)
                self.journal.save()
            self.error = None

    def converge(self):
        # Exactly one backend rebuilds at a time; heartbeat leases protect slow jobs.
        missing = []
        for node in self.members:
            try: self.call(node, 'apply')
            except Exception: missing.append(node)
        return missing

    def reconcile(self):
        if not self.reconcile_lock.acquire(False): return
        try:
            self.ensure()
        except Exception:
            self.error = 'policy_apply_pending'
        finally:
            self.reconcile_lock.release()

    def perform_write(self, route, body):
        if not self.api.owns(self.config['vip']): raise Failure(409, 'DNS VIP owner changed')
        body = self.api.validate_write(route, body, serialized=True)
        if not self.write_lock.acquire(False): raise Failure(409, 'Policy synchronization is already running')
        try:
            self.ensure()
            heads = self.refresh()
            revision = self.journal.state['revision']
            # Do not accept another edit over a still unacknowledged native apply.
            if sum(revision>=0 and h['revision']==revision and h['applied_revision']==revision for h in heads.values()) < 2:
                raise Failure(409, 'Committed policy is still applying; retry after recovery')
            with self.apply_lock:
                # Before touching native state, durably mark this replica dirty.
                # If the writer disappears before quorum commit, recovery restores
                # the certified policy instead of treating orphan native edits as applied.
                with self.journal.lock:
                    self.journal.state['applied_revision']=-1
                    self.journal.save()
                result = (self.api.filter_write(self.config, route, body) if route.startswith('filtering/')
                          else self.api.fetch(self.config['adguard']+'/control/'+route, self.config,
                                              self.api.WRITES[route], body, timeout=60))
                value = {'policy':self.api.policy_snapshot(self.config['adguard'], self.config),
                         'refresh':copy.deepcopy(self.journal.state['value'].get('refresh'))}
                if route == 'filtering/refresh':
                    value['refresh'] = {'generation':secrets.token_hex(16), 'body':body, 'origin':self.node}
                if digest(value) != digest(self.journal.state['value']):
                    revision = self.choose(value, revision)
            self.apply()
            missing=self.converge()
            heads=self.fanout('status')
            ready=[n for n,h in heads.items() if revision>=0 and h['revision']==revision and h['applied_revision']==revision]
            return {'result':result,'status':200 if len(ready)==len(self.members) else 202,
                    'revision':revision,'ready':ready}

        finally:
            self.write_lock.release()


    def write(self,route,body):
        if not self.api.owns(self.config['vip']): raise Failure(409,'DNS VIP owner changed')
        body=self.api.validate_write(route,body,serialized=True)
        identity=digest({'route':route,'body':body})
        with self.job_lock:
            job=self.job
            if job and not job['done'].is_set():
                if job['identity']!=identity: raise Failure(409,'A policy operation is still running; retry after completion')
            else:
                job={'identity':identity,'done':threading.Event(),'result':None,'error':None}
                self.job=job
                def run():
                    try: job['result']=self.perform_write(route,body)
                    except Exception as error:
                        job['error']=error
                        self.error='policy_operation_failed'
                    finally: job['done'].set()
                threading.Thread(target=run,daemon=True).start()
        # Large downloads/rebuilds run to completion behind a renewable lease.
        # The UI gets an accepted/pending response rather than a gateway timeout.
        job['done'].wait(self.config.get('write_wait',20))
        if job['done'].is_set():
            if job['error']: raise job['error']
            return {**job['result'],'operation_id':identity}
        heads=self.fanout('status');revision=self.journal.state['revision']
        ready=[n for n,h in heads.items() if revision>=0 and h['revision']==revision and h['applied_revision']==revision]
        return {'result':{'updated':0} if route=='filtering/refresh' else None,
                'status':202,'revision':revision,'ready':[],'operation_id':identity}

    def public_status(self):
        heads=self.refresh();revision=self.journal.state['revision']
        job=self.job
        return {'revision':revision,
                'ready':[n for n,h in heads.items() if revision>=0 and h['revision']==revision and h['applied_revision']==revision],
                'operation':'running' if job and not job['done'].is_set() else 'failed' if job and job['error'] else 'idle',
                'error':self.error,
                'operation_id':job['identity'] if job else None,
                'operation_result':job['result']['result'] if job and job['result'] else None,
                'operation_status':job['result']['status'] if job and job['result'] else getattr(job['error'],'code',503) if job and job['error'] else None}
