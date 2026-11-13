"""Keep local proxy fixtures independent of the controller's HTTP proxies."""
import os
from pathlib import Path


def fixture_env(directory):
    directory = Path(directory)
    env = dict(os.environ)
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(name, None)
    env.update(NO_PROXY='*', no_proxy='*',
               XDG_DATA_HOME=str(directory / 'data'),
               XDG_CONFIG_HOME=str(directory / 'config'))
    return env
