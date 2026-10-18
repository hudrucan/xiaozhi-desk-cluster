"""Mounted as sitecustomize.py only in the camera-enabled native Core.

Install the SDK guard before HA/native integration imports or background tasks.
No credentials, network I/O, camera login or SDK client construction.
"""
import importlib.util
import os
import pathlib
import sys
import types


def arm(root=pathlib.Path('/config'), authority=pathlib.Path('/opt/desk-camera-authority/owner.json')):
    source=root/'custom_components/desk_cluster/recording_remote.py'
    spec=importlib.util.spec_from_file_location('_desk_recording_bootstrap',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    hass=types.SimpleNamespace(config=types.SimpleNamespace(path=lambda name:str(root/name)))
    selected=module.policy(hass)
    # Existing phase-0 installations may have no worker authority policy yet.
    active=module.policy(types.SimpleNamespace(config=types.SimpleNamespace(path=lambda name:str(authority)))) if authority.exists() else frozenset()
    if not active<=selected:raise ValueError('Core guard does not cover worker authority')
    state=module.install_sdk_guard(hass)
    state['before_native_setup']=True


if os.environ.get('DESK_CAMERA_RECORDING_GUARD')=='1':
    try:arm()
    except BaseException:
        try:sys.stderr.write('Core recording guard bootstrap failed; refusing unguarded startup.\n')
        finally:os._exit(78)
