"""Read the existing Tapo poll result; never open an additional camera session."""
HOSTS={f'192.168.1.{n}':f'tapo_c222_{n}' for n in (160,161,163)}
def storage_state(runtime):
 if not isinstance(runtime,dict) or runtime.get('refreshEnabled') is False:return 'unknown'
 coordinator=runtime.get('coordinator')
 if coordinator is not None and not coordinator.last_update_success:return 'unknown'
 data=runtime.get('camData')
 if not isinstance(data,dict):return 'unknown'
 disks=data.get('sdCardData')
 if not isinstance(disks,list) or not disks:return 'unknown'
 statuses={str(d.get('status','')).lower() for d in disks if isinstance(d,dict)}
 if 'normal' in statuses:return 'ready'
 if statuses and statuses <= {'offline','none','absent'}:return 'absent'
 if statuses & {'unformatted','formatting','abnormal','error','readonly'}:return 'needs_attention'
 return 'unknown'
def camera_storage(hass):
 result={workload:'unknown' for workload in HOSTS.values()}
 for entry in hass.config_entries.async_entries('tapo_control'):
  workload=HOSTS.get(entry.data.get('ip_address'))
  if workload:result[workload]=storage_state(hass.data.get('tapo_control',{}).get(entry.entry_id))
 return result
