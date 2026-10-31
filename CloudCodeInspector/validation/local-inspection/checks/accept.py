import runpy,sys
from pathlib import Path
m=runpy.run_path(str(Path(sys.argv[1])/'app.py'))
assert m['add'](2,3)==5, 'add(2,3) must return 5'
