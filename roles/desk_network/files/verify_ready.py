import json,urllib.request
ready=[]
for node,ip in {'deskb1x':'10.10.10.11','deskb2x':'10.10.10.12','deskb3x':'10.10.10.13'}.items():
 try:
  with urllib.request.urlopen('http://'+ip+':20213/health',timeout=2) as r:
   v=json.load(r)
   if r.status==200 and v['node']==node and v['state']=='active':ready.append(node)
 except Exception:pass
assert len(ready)==1,'Expected exactly one native-ready Network owner'
print('Native-ready Network owner:',ready[0])
