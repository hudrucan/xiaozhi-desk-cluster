"""Pinned Tapo 3.4.26 read batch and bounded owner observation DTO.

No caller-selected SDK operation, credentials or media session crosses this API.
"""
import collections
import json
import math
import pathlib
import re

HOSTS = {f'tapo_c222_{n}': f'192.168.1.{n}' for n in (160, 161, 163)}
MAX_RESPONSE = 32768
FRESH_FOR = 2
PERIOD = 30
# Audited from the pinned SDK getMost legacy-camera branch, not introspected
# from a running SDK. An SDK upgrade must deliberately update this contract.
READ_REQUESTS = [{'method': 'getLinecrossingDetectionConfig',
  'params': {'linecrossing_detection': {'name': ['detection', 'arming_schedule']}}},
 {'method': 'getDualCamLinkage', 'params': {'dual_cam_linkage': {'name': 'linkage_state'}}},
 {'method': 'readLinkageTargetSetting', 'params': {'dual_cam_linkage': {'read_linkage_target_setting': {}}}},
 {'method': 'getLinkageTargetCapability',
  'params': {'dual_cam_linkage': {'name': 'linkage_target_capability'}}},
 {'method': 'getDualCamCapability', 'params': {'image_capability': {'name': ['dualCam']}}},
 {'method': 'getAllChnInfo', 'params': {'system': {'table': 'chn_info'}}},
 {'method': 'getDiagnoseMode', 'params': {'system': {'name': 'sys'}}},
 {'method': 'getCoverConfig', 'params': {'cover': {'name': ['cover']}}},
 {'method': 'getCoverConfig', 'params': {'cover': {'table': ['region_info']}}},
 {'method': 'getSmartTrackConfig', 'params': {'smart_track': {'name': 'smart_track_info'}}},
 {'method': 'getDeviceIpAddress', 'params': {'network': {'name': ['wan']}}},
 {'method': 'getFloodlightStatus', 'params': {'floodlight': {'get_floodlight_status': ''}}},
 {'method': 'getFloodlightConfig', 'params': {'floodlight': {'name': 'config'}}},
 {'method': 'getFloodlightCapability', 'params': {'floodlight': {'name': 'capability'}}},
 {'method': 'getPirDetCapability', 'params': {'pir_detection': {'name': 'pir_capability'}}},
 {'method': 'getPirDetConfig', 'params': {'pir_detection': {'name': 'pir_det'}}},
 {'method': 'getAlertEventType', 'params': {'msg_alarm': {'table': 'msg_alarm_type'}}},
 {'method': 'getDstRule', 'params': {'system': {'name': 'dst'}}},
 {'method': 'getClockStatus', 'params': {'system': {'name': 'clock_status'}}},
 {'method': 'getTimezone', 'params': {'system': {'name': ['basic']}}},
 {'method': 'getAlertTypeList', 'params': {'msg_alarm': {'name': 'alert_type'}}},
 {'method': 'getNightVisionCapability', 'params': {'image_capability': {'name': ['supplement_lamp']}}},
 {'method': 'getDeviceInfo', 'params': {'device_info': {'name': ['basic_info']}}},
 {'method': 'getDetectionConfig', 'params': {'motion_detection': {'name': ['motion_det']}}},
 {'method': 'getPersonDetectionConfig', 'params': {'people_detection': {'name': ['detection']}}},
 {'method': 'getVehicleDetectionConfig', 'params': {'vehicle_detection': {'name': ['detection']}}},
 {'method': 'getBCDConfig', 'params': {'sound_detection': {'name': ['bcd']}}},
 {'method': 'getPetDetectionConfig', 'params': {'pet_detection': {'name': ['detection']}}},
 {'method': 'getPackageDetectionConfig', 'params': {'package_detection': {'name': ['detection']}}},
 {'method': 'getBarkDetectionConfig', 'params': {'bark_detection': {'name': ['detection']}}},
 {'method': 'getMeowDetectionConfig', 'params': {'meow_detection': {'name': ['detection']}}},
 {'method': 'getGlassDetectionConfig', 'params': {'glass_detection': {'name': ['detection']}}},
 {'method': 'getTamperDetectionConfig', 'params': {'tamper_detection': {'name': ['tamper_det']}}},
 {'method': 'getLensMaskConfig', 'params': {'lens_mask': {'name': ['lens_mask_info']}}},
 {'method': 'getLdc', 'params': {'image': {'name': ['switch']}}},
 {'method': 'getLdc', 'params': {'image': {'name': ['common']}}},
 {'method': 'getLastAlarmInfo', 'params': {'msg_alarm': {'name': ['chn1_msg_alarm_info']}}},
 {'method': 'getLedStatus', 'params': {'led': {'name': ['config']}}},
 {'method': 'getTargetTrackConfig', 'params': {'target_track': {'name': ['target_track_info']}}},
 {'method': 'getPatrolSchedule', 'params': {'patrol': {'get_patrol_schedule': {}}}},
 {'method': 'getPatrolAction', 'params': {'patrol': {'get_patrol_action': {}}}},
 {'method': 'getPresetConfig', 'params': {'preset': {'name': ['preset']}}},
 {'method': 'getFirmwareUpdateStatus', 'params': {'cloud_config': {'name': 'upgrade_status'}}},
 {'method': 'getMediaEncrypt', 'params': {'cet': {'name': ['media_encrypt']}}},
 {'method': 'getConnectionType', 'params': {'network': {'get_connection_type': []}}},
 {'method': 'getAlarmConfig', 'params': {'msg_alarm': {}}},
 {'method': 'getAlarmPlan', 'params': {'msg_alarm_plan': {}}},
 {'method': 'getSirenTypeList', 'params': {'msg_alarm': {}}},
 {'method': 'getSirenTypeList', 'params': {'siren': {}}},
 {'method': 'getSirenConfig', 'params': {'siren': {}}},
 {'method': 'getAlertConfig',
  'params': {'msg_alarm': {'name': ['chn1_msg_alarm_info', 'capability'], 'table': ['usr_def_audio']}}},
 {'method': 'getAlertConfig',
  'params': {'msg_alarm': {'name': ['chn1_msg_alarm_info'], 'table': ['usr_def_audio']}}},
 {'method': 'getLightTypeList', 'params': {'msg_alarm': {}}},
 {'method': 'getSirenStatus', 'params': {'msg_alarm': {}}},
 {'method': 'getSirenStatus', 'params': {'siren': {}}},
 {'method': 'getLightFrequencyInfo', 'params': {'image': {'name': ['common']}}},
 {'method': 'getLightFrequencyCapability', 'params': {'image': {'name': 'common'}}},
 {'method': 'getChildDeviceList', 'params': {'childControl': {'start_index': 0}}},
 {'method': 'getRotationStatus', 'params': {'image': {'name': ['switch']}}},
 {'method': 'getNightVisionModeConfig', 'params': {'image': {'name': ['switch']}}},
 {'method': 'getWhitelampStatus', 'params': {'image': {'get_wtl_status': ['null']}}},
 {'method': 'getWhitelampConfig', 'params': {'image': {'name': ['switch']}}},
 {'method': 'getMsgPushConfig', 'params': {'msg_push': {'name': ['chn1_msg_push_info']}}},
 {'method': 'getSdCardStatus', 'params': {'harddisk_manage': {'table': ['hd_info']}}},
 {'method': 'getCircularRecordingConfig', 'params': {'harddisk_manage': {'name': 'harddisk'}}},
 {'method': 'getRecordPlan', 'params': {'record_plan': {'name': ['chn1_channel']}}},
 {'method': 'getAudioConfig',
  'params': {'audio_config': {'name': ['speaker', 'microphone', 'record_audio']}}},
 {'method': 'getFirmwareAutoUpgradeConfig', 'params': {'auto_upgrade': {'name': ['common']}}},
 {'method': 'getVideoQualities', 'params': {'video': {'name': ['main']}}},
 {'method': 'getVideoCapability', 'params': {'video_capability': {'name': ['main', 'minor']}}},
 {'method': 'getQuickRespList', 'params': {'quick_response': {}}},
 {'method': 'getChimeRingPlan', 'params': {'chime_ring_plan': {'name': 'chn1_chime_ring_plan'}}},
 {'method': 'getRingStatus', 'params': {'ring': {'name': 'status'}}},
 {'method': 'getChimeCtrlList', 'params': {'chime_ctrl': {'get_paired_device_list': {}}}},
 {'method': 'getBatteryPowerSave', 'params': {'battery': {'name': 'power_save'}}},
 {'method': 'getBatteryOperatingMode', 'params': {'battery': {'name': 'operating'}}},
 {'method': 'getBatteryOperatingModeParam', 'params': {'battery': {'name': 'operating_mode_param'}}},
 {'method': 'getPowerMode', 'params': {'battery': {'name': 'power'}}},
 {'method': 'getChargingMode', 'params': {'battery': {'name': 'charging_mode'}}},
 {'method': 'getBatteryStatistic', 'params': {'battery': {'statistic': {'days': 30}}}},
 {'method': 'getBatteryConfig', 'params': {'battery': {'name': 'config'}}},
 {'method': 'getWakeUpConfig', 'params': {'wake_up': {'name': 'config'}}},
 {'method': 'getReboot', 'params': {'timing_reboot': {'name': ['reboot']}}},
 {'method': 'getBatteryCapability', 'params': {'battery': {'name': 'capability'}}},
 {'method': 'getHubStorage', 'params': {'hub_manage': {'name': 'hub_storage_info'}}},
 {'method': 'getPirSensitivity', 'params': {'pir': {'name': 'config'}}},
 {'method': 'getClipsConfig', 'params': {'clips': {'name': 'config'}}}]
COUNTS = dict(collections.Counter(r['method'] for r in READ_REQUESTS))
FORBIDDEN = frozenset(('password', 'cloudpassword', 'supersecretkey', 'stok',
                       'userid', 'playerid', 'token', 'authorization', 'localkey',
                       'encryptkey', 'encryptionkey', 'secret', 'session'))


def encoded(value):
    return json.dumps(value, separators=(',', ':'), allow_nan=False).encode()


def token(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{32}', value) is not None


def policy(path):
    try:raw = pathlib.Path(path).read_bytes()
    except FileNotFoundError:return frozenset()
    if len(raw) > 4096:raise ValueError('Observation policy size')
    v = json.loads(raw)
    if set(v) != {'version', 'workloads'} or type(v['version']) is not int or v['version'] != 1 or not isinstance(v['workloads'], list):raise ValueError('Observation policy')
    if any(not isinstance(w, str) or w not in HOSTS for w in v['workloads']) or len(set(v['workloads'])) != len(v['workloads']):raise ValueError('Observation cameras')
    return frozenset(v['workloads'])


def channels(value):
    if not isinstance(value, list) or len(value) > 2 or any(not isinstance(x, str) or not re.fullmatch('[0-9]{1,2}', x) for x in value) or len(set(value)) != len(value):raise ValueError('Observation channels')
    return value


def request(v):
    if not isinstance(v, dict) or set(v) != {'version', 'workload', 'generation', 'after'} or type(v['version']) is not int or v['version'] != 1 or v['workload'] not in HOSTS or not token(v['generation']) or (v['after'] is not None and not token(v['after'])):raise ValueError('Observation binding')
    return dict(v)


def safe_tree(value, depth=0, budget=None):
    budget = budget if budget is not None else [4096]
    budget[0] -= 1
    if depth > 12 or budget[0] < 0:raise ValueError('Observation complexity')
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or len(k) > 128 or re.sub('[^a-z]', '', k.lower()) in FORBIDDEN:raise ValueError('Observation private field')
            safe_tree(v, depth+1, budget)
    elif isinstance(value, list):
        if len(value) > 256:raise ValueError('Observation list')
        for v in value:safe_tree(v, depth+1, budget)
    elif isinstance(value, str):
        if len(value) > 1024:raise ValueError('Observation string')
    elif value is not None and type(value) not in (bool, int, float):raise ValueError('Observation type')
    elif type(value) in (int, float) and (not math.isfinite(value) or abs(value) > 1e16):raise ValueError('Observation number')


def batch(value):
    if not isinstance(value, dict) or set(value) != set(COUNTS):raise ValueError('Observation methods')
    for method, count in COUNTS.items():
        if not isinstance(value[method], list) or len(value[method]) != count or any(v is not False and not isinstance(v, dict) for v in value[method]):raise ValueError('Observation result shape')
    safe_tree(value)
    if not isinstance(value['getDeviceInfo'][0], dict) or not value['getDeviceInfo'][0].get('device_info', {}).get('basic_info'):raise ValueError('Observation device identity')
    if len(encoded(value)) > MAX_RESPONSE-1024:raise ValueError('Observation size')
    return value


def snapshot(v, expected, now):
    if not isinstance(v, dict) or set(v) != {'version', 'workload', 'generation', 'state', 'snapshot', 'observed_at', 'channels', 'data'}:raise ValueError('Observation fields')
    if type(v.get('version')) is not int:raise ValueError('Observation version')
    if any(v.get(k) != expected[k] for k in ('version', 'workload', 'generation')) or v['state'] != 'ready' or not token(v['snapshot']):raise ValueError('Observation scope')
    at = v['observed_at']
    if type(at) not in (int, float) or not math.isfinite(at) or not -1 <= now-at <= FRESH_FOR:raise ValueError('Observation expired')
    if expected['after'] is not None and v['snapshot'] == expected['after']:raise ValueError('Observation refresh pending')
    channels(v['channels']);batch(v['data'])
    if len(encoded(v)) > MAX_RESPONSE:raise ValueError('Observation size')
    return v


def read_request(v):
    """Reject SDK implicit write fallback and nested/raw RPCs before transport."""
    if not isinstance(v, dict) or set(v) != {'method', 'params'} or v['method'] != 'multipleRequest' or not isinstance(v['params'], dict) or set(v['params']) != {'requests'}:raise ValueError('Read-only camera adapter')
    requests = v['params']['requests']
    if not isinstance(requests, list) or not 1 <= len(requests) <= len(READ_REQUESTS):raise ValueError('Read batch limit')
    counts = collections.Counter()
    for original in requests:
        item = json.loads(encoded(original))
        # The pinned SDK adds chn_id only to these existing config roots.
        for root in item.get('params', {}).values():
            if isinstance(root, dict) and 'chn_id' in root:channels(root.pop('chn_id'))
        if item not in READ_REQUESTS:raise ValueError('Read-only camera adapter')
        counts[item['method']] += 1
        if counts[item['method']] > COUNTS[item['method']]:raise ValueError('Read batch duplicate')
    return v
