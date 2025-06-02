import glob
import os
from . import *

with acquire_plock(): # flock on pid file
	for path in glob.glob("rec/????-??"): # for each YYYY-MM dir,
		if not os.path.isdir(path):
			continue
		proc_dir(path)
