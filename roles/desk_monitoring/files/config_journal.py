"""Small encrypted configuration-only Paxos journal; no metrics or background IO."""
import copy,hashlib,hmac,json,os,threading


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()

def atomic(path, data):
    temporary = path.with_name(path.name + '.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Failure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class Journal:
    """Single-decree Paxos per revision with a bounded, encrypted checkpoint.

    Promise and accept are fsynced only on configuration changes. A new proposer
    must carry forward the highest accepted value returned by its promise quorum.
    Certificates retain a chosen value across proposer failure and node restart.
    """
    def __init__(self, path, cipher, node, members, key):
        self.path, self.cipher, self.node = path, cipher, node
        self.members, self.key = set(members), key
        self.lock = threading.RLock()
        self.state = json.loads(cipher.decrypt(path.read_bytes())) if path.exists() else {
            'revision': -1, 'value': None, 'certificate': [], 'promised': [0, ''],
            'accepted': None, 'mapping': {}, 'applied_revision': -1, 'applied_value': None}

    def save(self):
        atomic(self.path, self.cipher.encrypt(encoded(self.state)))

    def status(self, full=False):
        with self.lock:
            result = {k: copy.deepcopy(self.state[k]) for k in ('revision', 'promised', 'accepted')}
            result['applied_revision'] = self.state['applied_revision']
            result['digest'] = digest(self.state['value'])
            if full:
                result.update(value=copy.deepcopy(self.state['value']),
                              certificate=copy.deepcopy(self.state['certificate']))
            else:
                result.pop('accepted')
            return result

    def initialize(self, value):
        with self.lock:
            if self.state['revision'] >= 0:
                if self.state['revision'] == 0 and digest(self.state['value']) != digest(value):
                    raise Failure(409, 'Existing monitoring seed differs; retain existing configuration')
                return
            self.state.update(revision=0, value=value)
            self.save()

    def promise(self, revision, ballot):
        with self.lock:
            if revision != self.state['revision'] + 1:
                return {'head': self.status(True)}
            if ballot <= self.state['promised']:
                raise Failure(409, 'Configuration ballot superseded')
            self.state['promised'] = ballot
            self.save()
            return {'accepted': copy.deepcopy(self.state['accepted'])}

    def receipt(self, revision, ballot, value):
        message = {'node': self.node, 'revision': revision, 'ballot': ballot, 'digest': digest(value)}
        return {**message, 'signature': hmac.new(self.key, encoded(message), hashlib.sha256).hexdigest()}

    def accept(self, revision, ballot, value):
        with self.lock:
            if revision != self.state['revision'] + 1 or ballot < self.state['promised']:
                raise Failure(409, 'Configuration revision superseded')
            previous = self.state['accepted']
            if previous and previous['ballot'] == ballot and digest(previous['value']) != digest(value):
                raise Failure(409, 'A ballot cannot accept two configurations')
            self.state.update(promised=ballot, accepted={'ballot': ballot, 'value': value})
            self.save()
            return self.receipt(revision, ballot, value)

    def commit(self, revision, value, certificate):
        with self.lock:
            if revision <= self.state['revision']:
                if revision == self.state['revision'] and digest(value) != digest(self.state['value']):
                    raise Failure(409, 'Conflicting configuration checkpoint')
                return
            voters, ballots = set(), set()
            for receipt in certificate:
                message = {k: receipt[k] for k in ('node', 'revision', 'ballot', 'digest')}
                expected = hmac.new(self.key, encoded(message), hashlib.sha256).hexdigest()
                if (message['node'] not in self.members or message['revision'] != revision
                        or message['digest'] != digest(value)
                        or not hmac.compare_digest(expected, receipt['signature'])):
                    raise Failure(403, 'Invalid configuration certificate')
                voters.add(message['node'])
                ballots.add(tuple(message['ballot']))
            if len(voters) < 2 or len(ballots) != 1:
                raise Failure(409, 'Configuration requires two independent acknowledgments')
            self.state.update(revision=revision, value=value, certificate=certificate,
                              promised=[0, ''], accepted=None)
            self.save()

