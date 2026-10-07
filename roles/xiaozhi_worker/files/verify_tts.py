"""Read-only targeted TTS readiness, without inference or provider imports."""
import argparse
import asyncio
import logging

from nats.aio.client import Client
from core.cluster.nats_config import NatsConnectionConfig
from core.cluster.tts_config import load_bundle
from core.cluster import tts_protocol as wire
from core.cluster.llm_protocol import encode


async def verify(args):
    bundle = load_bundle(args.bundle, args.node_id)
    if (bundle['revision'] != args.revision or bundle['fingerprint'] != args.fingerprint
            or sorted(bundle['workers']) != sorted(args.workers.split(','))):
        raise ValueError('TTS bundle changed from frozen deployment')
    config = NatsConnectionConfig.from_env()
    client = Client()
    async def silent_error(error):
        pass
    try:
        await asyncio.wait_for(client.connect(servers=list(config.servers),
            user=config.username, password=config.password, error_cb=silent_error,
            connect_timeout=2, max_reconnect_attempts=0), 8)
        for node in bundle['workers'] if args.all_nodes else [args.node_id]:
            value = {'protocol': wire.PROTOCOL, 'op': 'status', 'core_id': args.node_id,
                'revision': bundle['revision'], 'fingerprint': bundle['fingerprint']}
            response = await client.request(wire.subject(node), encode(value, wire.MAX_REQUEST), timeout=2)
            header, pcm = wire.unpack(response.data)
            expected = {'protocol': wire.PROTOCOL, 'worker_id': node,
                'revision': bundle['revision'], 'fingerprint': bundle['fingerprint'], 'state': header.get('state')}
            if pcm or header != expected or header['state'] not in ('idle', 'busy') or type(header['revision']) is not int:
                raise ValueError('TTS identity differs')
    finally:
        await asyncio.wait_for(client.close(), 2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--node-id', required=True)
    parser.add_argument('--revision', type=int, required=True)
    parser.add_argument('--fingerprint', required=True)
    parser.add_argument('--workers', required=True)
    parser.add_argument('--all-nodes', action='store_true')
    logging.getLogger('nats').addHandler(logging.NullHandler())
    logging.getLogger('nats').propagate = False
    try:
        asyncio.run(asyncio.wait_for(verify(parser.parse_args()), 20))
    except Exception:
        raise SystemExit('TTS targeted readiness unavailable') from None
    print('TTS targeted readiness passed')
