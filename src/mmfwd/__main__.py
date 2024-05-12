import signal
import yaml
from gi.repository import GLib
from mmfwd import *

CONFIG_FILENAME = "mmfwd.yaml"

def handle_signal (loop):
	'''Handle exit signals'''
	loop.quit()

# load config
try:
	from yaml import CLoader as Loader, CDumper as Dumper
except ImportError:
	from yaml import Loader, Dumper

conf = yaml.load(
	open(os.getenv("MMFWD_CONFIG") or CONFIG_FILENAME), Loader)["mmfwd"]
# instantiate the singleton objects
app = Application(conf)
main_loop = GLib.MainLoop()

GLib.unix_signal_add(
	GLib.PRIORITY_HIGH, signal.SIGINT, handle_signal, main_loop)
GLib.unix_signal_add(
	GLib.PRIORITY_HIGH, signal.SIGTERM, handle_signal, main_loop)


# do the Glib loop
main_loop.run()
