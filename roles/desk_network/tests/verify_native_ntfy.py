"""Run in the pinned image. Isolated fixture databases never touch its inventory.

--send MARKER also exercises the unmodified native send() with installed settings.
"""
import ast
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import re
import runpy
import sqlite3
import sys


def verify_filters():
    queries = runpy.run_path('/app/server/messaging/notification_sections.py')['SQL_TEMPLATES']
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE Events(eveMac TEXT, eveEventType TEXT, eveDateTime TEXT);
        CREATE TABLE Events_Devices(devName TEXT,eveMac TEXT,devVendor TEXT,eveIp TEXT,
            devLastIP TEXT,eveDateTime TEXT,eveEventType TEXT,devComments TEXT,devVlan TEXT,
            devSite TEXT,devSSID TEXT,evePendingAlertEmail INTEGER);
        INSERT INTO Events_Devices VALUES('fixture','02:00:00:00:00:01','','192.0.2.1',
            '192.0.2.1',datetime('now','-10 minutes'),'New Device','','','','',1);
    ''')
    def rows(section):
        return db.execute(queries[section].format(condition='',alert_down_minutes=5)).fetchall()
    assert len(rows('new_devices')) == 1
    db.execute('UPDATE Events_Devices SET evePendingAlertEmail=0')
    assert not rows('new_devices')
    db.execute("UPDATE Events_Devices SET eveEventType='Device Down',evePendingAlertEmail=1,eveDateTime=datetime('now','-2 minutes')")
    assert not rows('down_devices'), 'Transient down must not alert'
    db.execute("UPDATE Events_Devices SET eveDateTime=datetime('now','-10 minutes')")
    assert len(rows('down_devices')) == 1
    db.execute("INSERT INTO Events VALUES('02:00:00:00:00:01','Connected',datetime('now','-1 minute'))")
    assert not rows('down_devices'), 'Recovered device must not alert'
    db.close()
    print('Native new/down filters, pending suppression, delay and reconnect: PASS')


def verify_pipeline():
    # All native filesystem/database access is redirected before importing modules.
    # Only the external HTTP transport and subprocess dispatch are replaced.
    with tempfile.TemporaryDirectory(prefix='ntfy-pipeline-') as temporary:
        root=Path(temporary)
        for key,folder in [('NETALERTX_DATA','data'),('NETALERTX_CONFIG','config'),
                           ('NETALERTX_DB','db'),('NETALERTX_TMP','tmp'),
                           ('NETALERTX_API','api'),('NETALERTX_LOG','log')]:
            path=root/folder;path.mkdir();os.environ[key]=str(path)
        (root/'log/plugins').mkdir()
        sys.path[:0]=['/app/server','/app/server/plugins']
        import conf, helper
        config={'TIMEZONE':'UTC','LOG_LEVEL':'none','REPORT_DASHBOARD_URL':'http://fixture.invalid',
                'NTFY_RUN':'on_notification','NTFY_RUN_TIMEOUT':10,'NTFY_HOST':'http://fixture.invalid',
                'NTFY_TOPIC':'desk-events','NTFY_TOKEN':'fixture-token','NTFY_USER':'',
                'NTFY_PASSWORD':'','NTFY_PRIORITY':'default','NTFY_VERIFY_SSL':True,
                'NTFY_URL_QUERY_STRING':'','NTFY_CUSTOM_HEADERS':[],
                'NTFPRCS_INCLUDED_SECTIONS':['new_devices','down_devices'],
                'NTFPRCS_alert_down_time':5,'NTFPRCS_TEXT_SECTION_HEADERS':True}
        (root/'config/app.conf').write_text('\n'.join(k+'='+repr(v) for k,v in config.items()))
        with patch.object(helper,'get_setting_value',side_effect=lambda k,default='':config.get(k,default)):
            from database import DB
            from messaging.reporting import get_notifications
            from models.notification_instance import NotificationInstance
            import importlib.util
            spec=importlib.util.spec_from_file_location('fixture_native_ntfy','/app/server/plugins/_publisher_ntfy/ntfy.py')
            publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)
            conf.newVersionAvailable=False;conf.REPORT_DASHBOARD_URL=config['REPORT_DASHBOARD_URL']
            db=DB();db.open()
            db.sql.executescript("""
                CREATE TABLE Devices(devMac TEXT PRIMARY KEY,devName TEXT,devVendor TEXT,
                    devLastIP TEXT,devComments TEXT,devVlan TEXT,devSite TEXT,devSSID TEXT,
                    devAlertEvents INTEGER,devAlertDown INTEGER,devLastNotification TEXT);
                CREATE TABLE Events(eveMac TEXT,eveIp TEXT,eveDateTime TEXT,
                    eveEventType TEXT,evePendingAlertEmail INTEGER);
                CREATE VIEW Events_Devices AS SELECT * FROM Events JOIN Devices ON eveMac=devMac;
                CREATE TABLE Plugins_Events(dummy TEXT);
            """)
            def event(name,kind,minutes,enabled=1,reconnect=False):
                mac=f'02:00:00:00:00:{db.sql.execute("SELECT count(*) FROM Devices").fetchone()[0]:02x}'
                db.sql.execute('INSERT INTO Devices VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (mac,name,'fixture','192.0.2.1','','','','',1,enabled,''))
                db.sql.execute("INSERT INTO Events VALUES(?,'192.0.2.1',datetime('now',?),?,1)",
                               (mac,f'-{minutes} minutes',kind))
                if reconnect:
                    db.sql.execute("INSERT INTO Events VALUES(?,'192.0.2.1',datetime('now'),'Connected',1)",(mac,))
                db.commitDB()
            # Use the pinned main loop's actual reporting/create/publish/clear block.
            tree=ast.parse(Path('/app/server/__main__.py').read_text())
            main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
            parent=None
            for node in ast.walk(main):
                if isinstance(node,ast.If) and ast.unparse(node.test)=='notificationObj.HasNotifications':
                    dispatch=node;break
            for node in ast.walk(main):
                for field in ('body','orelse','finalbody'):
                    body=getattr(node,field,None)
                    if isinstance(body,list) and dispatch in body:parent=body;break
                if parent is not None:break
            start=next(i for i,n in enumerate(parent) if isinstance(n,ast.Assign)
                       and any(isinstance(t,ast.Name) and t.id=='final_json' for t in n.targets))
            block=compile(ast.Module(body=parent[start:parent.index(dispatch)+1],type_ignores=[]),
                          'pinned-native-notification-loop','exec')
            class Dispatch:
                def run_plugin_scripts(self,stage):
                    assert stage=='on_notification'
                    assert config['NTFY_RUN']=='on_notification'
                    publisher.main()
            scope={'db':db,'get_notifications':get_notifications,'NotificationInstance':NotificationInstance,
                   'pm':Dispatch(),'mylog':lambda *args:None}
            response=SimpleNamespace(status_code=200,text='{"id":"fixture-message"}')
            with patch.object(publisher.requests,'post',return_value=response) as post:
                event('fixture-new','New Device',10)
                event('fixture-down','Device Down',10)
                event('fixture-short-down','Device Down',2)
                event('fixture-opt-out','Device Down',10,enabled=0)
                event('fixture-reconnected','Device Down',10,reconnect=True)
                event('fixture-other-event','IP Changed',10)
                exec(block,scope)
                assert post.call_count==1
                call=post.call_args
                assert call.args[0]=='http://fixture.invalid/desk-events'
                assert call.kwargs['headers']['Authorization']=='Bearer fixture-token'
                text=call.kwargs['data']
                assert 'fixture-new' in text and 'fixture-down' in text
                for name in ('fixture-short-down','fixture-opt-out','fixture-reconnected','fixture-other-event'):
                    assert name not in text,name
                exec(block,scope)
                assert post.call_count==1,'Unchanged scan replayed a processed notification'
                assert not NotificationInstance(db).getNew()
            db.sql_connection.close()
        print('Pinned event -> processor -> NotificationInstance -> NTFY.main -> HTTP; repeated scan: PASS')


def verify_processor_autoload():
    # 26.10.0: initialise.py:585-590 includes RUN != 'disabled'; the helper
    # returns '' when the core NTFPRCS config has no RUN setting.
    plugin=json.loads(Path('/app/server/plugins/notification_processing/config.json').read_text())
    assert not any(s['function']=='RUN' for s in plugin['settings'])
    source=ast.parse(Path('/app/server/utils/plugin_utils.py').read_text())
    functions=ast.Module(body=[n for n in source.body if isinstance(n,ast.FunctionDef)
                              and n.name in ('get_set_value_for_init','get_plugin_setting_obj')],type_ignores=[])
    namespace={};exec(compile(functions,'pinned-loader-helpers','exec'),namespace)
    run=namespace['get_set_value_for_init'](plugin,{},'RUN')
    assert run==''
    tree=ast.parse(Path('/app/server/initialise.py').read_text())
    predicate=next(n.test for n in ast.walk(tree) if isinstance(n,ast.If)
                   and 'pref in conf.LOADED_PLUGINS' in ast.unparse(n.test))
    assert eval(compile(ast.Expression(predicate),'pinned-loader-predicate','eval'),
                {'pref':'NTFPRCS','plugin_run':run,'conf':SimpleNamespace(LOADED_PLUGINS=['NTFY'])})
    print('Pinned NTFPRCS auto-load without explicit LOADED_PLUGINS entry: PASS')


def send_fixture(marker):
    # Execute the image's own publisher functions; substitute only its settings lookup.
    # Read literal configuration, never exec() the credential-bearing app.conf.
    config = {}
    for node in ast.parse(Path('/data/config/app.conf').read_text()).body:
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
            try:config[node.targets[0].id]=ast.literal_eval(node.value)
            except ValueError:pass
    assert config['NTFY_HOST']=='http://192.168.1.186:2586'
    assert config['NTFY_TOPIC']=='desk-events' and config['NTFY_RUN']=='on_notification'
    assert config['NTFPRCS_INCLUDED_SECTIONS']==['new_devices','down_devices']
    import requests
    source=ast.parse(Path('/app/server/plugins/_publisher_ntfy/ntfy.py').read_text())
    functions=ast.Module(body=[n for n in source.body if isinstance(n,ast.FunctionDef)
                              and n.name in ('header_is_sendable','build_custom_headers','send')],type_ignores=[])
    namespace={'requests':requests,'json':json,'re':re,'pluginName':'NTFY',
               'get_setting_value':config.get,'mylog':lambda *args:None}
    exec(compile(functions,'native-ntfy-functions','exec'),namespace)
    for event in ('New Device','Device Down'):
        message=f'{marker} | {event} | synthetic notification; inventory unchanged'
        body,status=namespace['send']('',message,5)
        assert status==200, f'Native publish returned {status}'
        result=json.loads(body)
        assert result['message']==message
        print(json.dumps({'event':event,'id':result['id']}))


if __name__=='__main__':
    verify_processor_autoload()
    verify_filters()
    verify_pipeline()
    if len(sys.argv)==3 and sys.argv[1]=='--send':send_fixture(sys.argv[2])
