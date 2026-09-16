"""Run a recompiled SML2 body headless along the Level 1 probe route and report interpreter
fallback sites/events, gameplay progress (mode $FF9B, camera X) and PPM captures under logs/body-<tag>/.
Usage: python tools/run_body.py <exe> <tag> [frames]"""

import json, socket, subprocess, time, os, sys, shutil
from pathlib import Path
exe=Path(sys.argv[1]).resolve(); tag=sys.argv[2]; frames=int(sys.argv[3]) if len(sys.argv)>3 else 4600
rom="F:/Projects/gbcrecomp/Super Mario Land 2/roms/Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb"
folder=Path("F:/Projects/gbcrecomp/Super Mario Land 2/logs")/("body-"+tag); shutil.rmtree(folder,ignore_errors=True); (folder/"logs").mkdir(parents=True)
shutil.copy2(exe, folder/exe.name); (folder/"rom.cfg").write_text(rom)
assets=exe.parent/"assets"
if assets.exists(): shutil.copytree(assets, folder/"assets")
JUMPS=",".join(f"{f}:A:18" for f in range(2620,4561,36))
ROUTE=f"60:S:4,140:S:4,220:S:4,2400:A:4,2600:R:2000,"+JUMPS
ps=socket.socket();ps.bind(('127.0.0.1',0));port=ps.getsockname()[1];ps.close()
env=os.environ.copy();env.update(GBRECOMP_DEBUG_PORT=str(port),GBRECOMP_NO_LAUNCHER='1',SML2_WIDESCREEN='off')
p=subprocess.Popen([str(folder/exe.name),'--input',ROUTE,'--log-file','logs/run.log','--benchmark'],cwd=folder,env=env,stdout=open(folder/'process.log','wb'),stderr=subprocess.STDOUT)
until=time.time()+30
while True:
    try: s=socket.create_connection(('127.0.0.1',port),timeout=1);break
    except OSError:
        if p.poll() is not None or time.time()>until: print(tag,'FAILED to start',p.poll()); print((folder/'logs/run.log').read_text()[-800:]); sys.exit(1)
        time.sleep(.05)
s.settimeout(120); r=s.makefile('r',encoding='utf8'); i=0
def cmd(c,**k):
    global i; i+=1; s.sendall((json.dumps(dict(cmd=c,id=i,**k))+'\n').encode())
    while True:
        o=json.loads(r.readline())
        if o.get('id')==i: return o
def ev(e):
    while True:
        o=json.loads(r.readline())
        if o.get('event')==e: return o
cmd('pause')
res={}
for f in (2500,3400,frames):
    cmd('run_to_frame',frame=f); ev('run_to_done')
    mode=int(cmd('read_ram',addr='ff9b',len=1)['hex'],16)
    cam=cmd('read_ram',addr='ffca',len=2)['hex']; camx=int(cam[0:2],16)|(int(cam[2:4],16)<<8)
    res[f]=dict(mode=mode,camx=camx)
    if 'sml2_capture' and cmd('sml2_capture').get('ok'):
        os.replace(folder/'logs/probe.ppm', folder/f'{f}.ppm')
cmd('quit'); p.wait(15)
fb=folder/'interp_fallbacks.log'
sites=set(); events=0
if fb.exists():
    for line in fb.read_text().splitlines():
        if line.startswith('#') or not line.strip(): continue
        b,a,c=line.split()[:3]; sites.add((b,a)); events+=int(c)
print(json.dumps(dict(tag=tag,frames=res,fallback_sites=len(sites),fallback_events=events,sites=sorted(sites)[:40])))
