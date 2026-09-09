@echo off
chcp 65001 >nul
cd /d %~dp0
set MPLCONFIGDIR=%~dp0logs\mpl
.venv\Scripts\python.exe -c "import sys,io,importlib.util

sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8')
sys.path.insert(0,'.')
s=importlib.util.spec_from_file_location('gc_main','????v6_optimized.py')
m=importlib.util.module_from_spec(s)
s.loader.exec_module(m)
ok=True
for st in ('bull','bear','sideways'):
 p,src=m.select_params(st)
 pf=p.get('pre_filter_threshold')
 mp=p.get('max_concurrent_positions')
 print(st+': pre_filter='+str(pf)+' max_pos='+str(mp))
 if pf is None or pf<60 or (st=='bear' and pf!=68): ok=False
print('CHECK', 'PASS' if ok else 'FAIL')
"
pause

