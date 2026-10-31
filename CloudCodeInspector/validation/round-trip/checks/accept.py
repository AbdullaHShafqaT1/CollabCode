import runpy,sys
from pathlib import Path
f=runpy.run_path(str(Path(sys.argv[1])/'app.py'))['add']
assert f(2,3)==5
assert f(-4,2)==-2
