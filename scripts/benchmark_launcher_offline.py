"""Offline installed-dependency launcher timing; isolated copy, no real services.
Reuses the existing runtime probe's external-action guards and lifecycle cleanup.
Never claims hospital latency, full clinical readiness, installation timing or p95.
"""
import argparse,ast,hashlib,json,os,shutil,subprocess,sys,tempfile,time
from pathlib import Path
RESTART_ENV_KEYS=('CMUH_RESTART_HANDSHAKE','CMUH_RESTART_READY_EVENT','CMUH_RESTART_PARENT_CAPS')
# Dependency bootstrap runs before the traced main entry: never inherit a live
# application's handshake path or named READY event, including direct workers.
for key in RESTART_ENV_KEYS:os.environ.pop(key,None)
parser=argparse.ArgumentParser()
parser.add_argument('--worker',action='store_true')
parser.add_argument('--app',type=Path)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
args=parser.parse_args()
ROOT=args.root.resolve()
MARKER='.cmuh_offline_launcher_probe'
if not args.worker:
 args.output.write_text(json.dumps({'status':'running','source_root':str(ROOT)}),encoding='utf-8')
 rows=[]
 with tempfile.TemporaryDirectory(prefix='cmuh_launcher_perf_') as tmp:
  app=Path(tmp).resolve()
  assert app.is_relative_to(Path(tempfile.gettempdir()).resolve())
  (app/MARKER).write_text('isolated offline deployment',encoding='utf-8')
  launcher=next(p for p in ROOT.glob('*.pyw') if 'main.py' in p.read_text(encoding='utf-8') and '_PROGRAM' in p.read_text(encoding='utf-8'))
  shutil.copy2(launcher,app/launcher.name)
  shutil.copy2(ROOT/'version_pointer.py',app/'version_pointer.py')
  shutil.copytree(ROOT/'src',app/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
  shutil.copytree(ROOT/'assets',app/'assets')
  for i in range(3):
   out=app/f'measure-{i}.json'
   env=dict(os.environ,CMUH_APP_DIR=str(app),CMUH_LAUNCHER=str(app/launcher.name))
   for key in RESTART_ENV_KEYS:env.pop(key,None)
   start=time.perf_counter()
   cp=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__)), '--worker','--app',str(app),'--output',str(out)],cwd=app,env=env,capture_output=True,text=True,encoding='utf-8',timeout=180)
   if cp.returncode:
    print(cp.stdout,cp.stderr)
    raise SystemExit(cp.returncode)
   d=json.loads(out.read_text(encoding='utf-8'))
   assert d['status']=='completed' and d['external_actions']==[]
   d['process_start_to_clean_exit_seconds']=time.perf_counter()-start
   d['sample']=i
   rows.append(d)
 result={'status':'completed','rows':rows,'scope':'Exact root launcher recovery/resolver and source __main__ to first real Tk dispatch, with already-installed dependencies. Temporary deployment and fake administrator/mutex/monitor/watchdog/operational startup boundaries. Window remains hidden. Guard/import/instrumentation overhead included; not hospital latency or hotkey/network readiness. First-install unmeasured; no p95/speedup claim.'}
 result['probe_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
 args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
 print(json.dumps({'status':result['status'],'rows':[{'sample':r['sample'],'version':r['source_version'],'cache_before':r['dependency_cache_before_launch'],'root_launcher_to_first_dispatch_seconds':r['root_launcher_to_first_dispatch_seconds'],'worker_start_to_first_dispatch_seconds':r['worker_start_to_first_dispatch_seconds']} for r in rows]}))
 raise SystemExit(0)

started=time.perf_counter()
import socket,threading,runpy
from contextlib import ExitStack
from unittest.mock import patch
blocked=[]
class ForbiddenExternalAction(BaseException):pass
def forbidden(*a,**k):
 blocked.append('external action attempted')
 raise ForbiddenExternalAction('No real network/process/hotkey action allowed')
if args.app is None:
 parser.error('--worker requires --app')
appdir=args.app.resolve()
if not appdir.is_relative_to(Path(tempfile.gettempdir()).resolve()) or not (appdir/MARKER).is_file():
 raise SystemExit('Worker refuses non-isolated deployment; no app code executed')
if not args.output.resolve().is_relative_to(appdir):
 raise SystemExit('Worker output must stay in its isolated deployment')
args.output.write_text(json.dumps({'status':'running'}),encoding='utf-8')
dependency_cache_before=(appdir/'.deps_cache').is_file()
sys.path.insert(0,str(appdir/'src'))
launcher=next(p for p in appdir.glob('*.pyw') if 'main.py' in p.read_text(encoding='utf-8'))
main_path=appdir/'src/main.py'
if not main_path.resolve().is_relative_to(appdir) or not launcher.resolve().is_relative_to(appdir):
 raise SystemExit('Worker source must stay in its isolated deployment')
os.environ['CMUH_APP_DIR']=str(appdir)
os.environ['CMUH_LAUNCHER']=str(launcher)
source=main_path.read_text(encoding='utf-8-sig')
tree=ast.parse(source)
entry=next(n for n in tree.body if isinstance(n,ast.If) and ast.dump(n.test)==ast.dump(ast.parse("__name__ == '__main__'",mode='eval').body))
entry_line=entry.body[0].lineno
observed=[]
callback_errors=[]
boundaries=[]
state={}
with ExitStack() as stack:
 for obj,name in [(socket.socket,'connect'),(socket.socket,'connect_ex'),(socket,'create_connection'),(socket,'getaddrinfo'),(subprocess,'Popen')]:stack.enter_context(patch.object(obj,name,forbidden))
 import requests,keyboard,win32gui,psutil,webbrowser,ctypes
 stack.enter_context(patch.object(requests.Session,'request',forbidden))
 for obj,name in [(psutil.Process,'kill'),(psutil.Process,'terminate'),(os,'kill'),(os,'startfile'),(webbrowser,'open'),(ctypes.windll.user32,'MessageBoxW')]:
  stack.enter_context(patch.object(obj,name,forbidden))
 for name in ['add_hotkey','hook','press','send','write','unhook_all','unhook_all_hotkeys']:
  if hasattr(keyboard,name):stack.enter_context(patch.object(keyboard,name,forbidden))
 for name in ['PostMessage','SendMessage','SendMessageTimeout']:
  if hasattr(win32gui,name):stack.enter_context(patch.object(win32gui,name,forbidden))
 from cmuh_common.resource_meter import ResourceMeter
 from cmuh_common import health,splash
 stack.enter_context(patch.object(ResourceMeter,'start',lambda self:boundaries.append('resource monitor')))
 stack.enter_context(patch.object(health,'start_health_monitor',lambda *a,**k:boundaries.append('health monitor')))
 stack.enter_context(patch.object(splash.StartupSplash,'show',lambda self:None))
 stack.enter_context(patch.object(splash.StartupSplash,'close',lambda self:None))
 real_start=threading.Thread.start
 def start_thread(self):
  if self.name=='InnerWatchdog':boundaries.append('inner watchdog');return
  return real_start(self)
 stack.enter_context(patch.object(threading.Thread,'start',start_thread))
 import tkinter as tk
 stack.enter_context(patch.object(tk.Tk,'iconify',lambda self:self.withdraw()))
 def first_dispatch(root,*a,**k):
  ns=state['namespace']
  app=ns['app']
  root.report_callback_exception=lambda kind,*args:callback_errors.append(kind.__name__)
  ready=[]
  root.after_idle(lambda:ready.append(app.notebook.index('current')))
  root.update_idletasks();root.update()
  assert ready and app.notebook.winfo_exists()
  observed.append(time.perf_counter()-started)
  app._cleanup_for_exit()
  app.bg_executor.shutdown(wait=True,cancel_futures=True)
  app.session.close();app.duty_session.close()
  for cb in root.tk.call('after','info'):root.tk.call('after','cancel',cb)
  root.destroy()
  ns['stop_event_main'].set();ns['stop_event_automation'].set()
 stack.enter_context(patch.object(tk.Tk,'mainloop',first_dispatch))
 def tracer(frame,event,arg):
  if event=='call' and Path(frame.f_code.co_filename)==main_path and frame.f_code.co_name=='<module>':return line_tracer
  return None
 def line_tracer(frame,event,arg):
  if event=='line' and frame.f_lineno==entry_line:
   ns=frame.f_globals
   state['namespace']=ns
   for name in ['run_as_admin','_set_windows_dpi_awareness','_set_windows_app_user_model_id','single_instance_gate','release_single_instance','place_tk_window_on_preferred_monitor','_pl_kill_orphan_chromedriver','safe_unhook_all_hotkeys']:
    assert name in ns, 'Startup boundary renamed; refuse unguarded measurement: '+name
    ns[name]=lambda *a,**k:None
   ns['AutomationApp'].deferred_initialization=lambda self:boundaries.append('operational startup')
   sys.settrace(None)
   boundaries.append('administrator/mutex/placement/cleanup')
   return None
  return line_tracer
 launcher_start=time.perf_counter()
 sys.settrace(tracer)
 try:runpy.run_path(str(launcher),run_name='__main__')
 finally:sys.settrace(None)
 assert observed and not blocked and not callback_errors
 result={'status':'completed','root_launcher_to_first_dispatch_seconds':observed[0]-(launcher_start-started),'worker_start_to_first_dispatch_seconds':observed[0],'external_actions':blocked,'callback_errors':callback_errors,'fake_boundaries':boundaries,'source_main_sha256':hashlib.sha256(main_path.read_bytes()).hexdigest(),'source_launcher_sha256':hashlib.sha256(launcher.read_bytes()).hexdigest(),'source_version':state['namespace']['CURRENT_VERSION'],'dependency_cache_before_launch':dependency_cache_before,'dependencies_installed':True,'dependency_installation_performed':False,'scope':'Root launcher and unmodified main __main__ executed on temporary copy; real hidden Tk callback dispatch succeeded. Service/system boundaries faked; operational deferred startup excluded from first UI marker.'}
 args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
