from copy import copy
import datetime
import os
import re
import signal
import subprocess
import sys
from typing import Any
import gi
import yaml
gi.require_version('ModemManager', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import GLib, Gio, ModemManager

try:
    from yaml import CLoader as Loader, CDumper as Dumper
except ImportError:
    from yaml import Loader, Dumper

APP_ID = "me.snart.mmfwd"

class ModemIdentity:
	def __init__ (self, conf: dict[str, Any]):
		self.n_own: str = None

		if conf:
			self.n_own: str = conf.get("n-own")

class Forward:
	def __init__ (self, conf: dict[str, Any]):
		self.mailto: list[str] = conf.get("mailto", [])
		self.cmd: list[str] = conf.get("cmd", [])

	def post_sms (self, doc):
		cmd = list[str]()
		for arg in self.cmd:
			cmd.append(arg.format(
				type = "sms",
				origin = doc["sms"]["from"],
				to = doc["sms"]["to"],
				ts_req = doc["sms"]["ts-req"],
				ts_del = doc["sms"]["ts-del"],
			))

		with subprocess.Popen(cmd, stdin = subprocess.PIPE) as p:
			p.stdin.write(("---" + os.linesep).encode())
			yaml.dump(doc, p.stdin, encoding = 'utf-8', allow_unicode = True)
			p.stdin.close()

	def post_call (self, doc):
		cmd = list[str]()
		for arg in self.cmd:
			cmd.append(arg.format(
				type = "call",
				origin = doc["call"]["from"],
				to = doc["call"]["to"],
				multiparty = doc["call"]["multiparty"],
			))

		with subprocess.Popen(cmd, stdin = subprocess.PIPE) as p:
			p.stdin.write(("---" + os.linesep).encode())
			yaml.dump(doc, p.stdin, encoding = 'utf-8', allow_unicode = True)
			p.stdin.close()

class Instance:
	def __init__ (self, conf: dict[str, Any]):
		self.mobj: str = None
		self.mid = ModemIdentity(conf.get("mid"))
		self.fwd = Forward(conf.get("fwd"))
		self.callam = conf.get("call-am", {
			'enabled': False
		})
		self.callam_proc = None
		self.callam_ringtone_proc = None
		self.callam_timer = None

	def match (self, m) -> bool:
		if self.mid.n_own:
			for n in m.get_property('own-numbers'):
				if re.match(self.mid.n_own, n):
					return True
		else:
			return True

		return False

class CallbackUserData:
	def __init__ (self):
		self.instance = None
		self.modem = None
		self.messaging = None
		self.voice = None
		self.call = None
		self.own_numbers = None

class Application:
	def __init__(self, conf: dict[str, Any]):
		self.instances = list[Instance]()
		for i in conf["instances"]:
			self.instances.append(Instance(i))

		# Flag for initial logs
		self.initializing = True
		# Setup DBus monitoring
		self.connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
		self.manager = ModemManager.Manager.new_sync(
			self.connection,
			Gio.DBusObjectManagerClientFlags.DO_NOT_AUTO_START,
			None)
		# IDs for added/removed signals
		self.object_added_id = 0
		self.object_removed_id = 0
		# Follow availability of the ModemManager process
		self.available = False
		self.manager.connect('notify::name-owner', self.on_name_owner)
		self.on_name_owner(self.manager, None)
		# Finish initialization
		self.initializing = False

	def attach_to (self, obj, instance):
		modem = obj.get_modem()
		messaging = obj.get_modem_messaging()
		voice = obj.get_modem_voice()

		ud = CallbackUserData()
		ud.instance = instance
		ud.modem = modem
		ud.messaging = messaging
		ud.voice = voice
		ud.own_numbers = modem.get_property('own-numbers')
		ud.device = modem.get_property('device')
		ud.audio_port = None

		if instance.callam['enabled']:
			for p in modem.get_property('ports'):
				if p[1] == ModemManager.ModemPortType.AUDIO:
					ud.audio_port = p[0]
					break

			assert ud.audio_port is not None, "Call-am enabled, but the modem has no audio port"

		modem.connect('state-changed', self.on_modem_state_updated, ud)
		messaging.connect('added', self.on_message_added, ud)
		voice.connect('call-added', self.on_call_added, ud)

		# fire request to sync
		messaging.list(None, self.on_messages, ud)
		voice.list_calls(None, self.on_calls_sync, ud)

	def set_available(self):
		"""
		ModemManager is now available.
		"""
		if not self.available or self.initializing:
			print('[ModemWatcher] ModemManager %s service is available in bus' % self.manager.get_version())
		self.object_added_id = self.manager.connect('object-added', self.on_object_added)
		self.object_removed_id = self.manager.connect('object-removed', self.on_object_removed)
		self.available = True
		# Initial scan
		if self.initializing:
			for obj in self.manager.get_objects():
				self.on_object_added(self.manager, obj)

	def set_unavailable(self):
		"""
		ModemManager is now unavailable.
		"""
		if self.available or self.initializing:
			print('[ModemWatcher] ModemManager service not available in bus')
		if self.object_added_id:
			self.manager.disconnect(self.object_added_id)
			self.object_added_id = 0
		if self.object_removed_id:
			self.manager.disconnect(self.object_removed_id)
			self.object_removed_id = 0
		self.available = False

	def on_name_owner(self, manager, prop):
		"""
		Name owner updates.
		"""
		if self.manager.get_name_owner():
			self.set_available()
		else:
			self.set_unavailable()

	def on_modem_state_updated(self, modem, old, new, reason, ud):
		"""
		Modem state updated
		"""
		print('[ModemWatcher] %s: modem state updated: %s -> %s (%s) ' %
				(modem.get_object_path(),
				ModemManager.ModemState.get_string (old),
				ModemManager.ModemState.get_string (new),
				ModemManager.ModemStateChangeReason.get_string (reason)))

	def on_object_added(self, manager, obj):
		"""
		Object added.
		"""
		modem = obj.get_modem()
		print('[ModemWatcher] %s: modem managed by ModemManager [%s]: %s (%s)' %
				(obj.get_object_path(),
				modem.get_equipment_identifier(),
				modem.get_manufacturer(),
				modem.get_model()))

		for i in self.instances:
			if not i.match(modem):
				continue

			mstate = modem.get_state()
			if mstate == ModemManager.ModemState.FAILED:
				sys.stderr.write(
					"[mmfwd] matching modem in failed state!" + os.linesep)
				# TODO: warn
				continue

			if mstate == ModemManager.ModemState.DISABLED:
				print('''[mmfwd] {m}: enabling disabled target modem'''.format(
					m = obj.get_object_path()))
				modem.enable()

			print('''[mmfwd] {m}: attaching to target modem'''.format(
				m = obj.get_object_path()))
			self.attach_to(obj, i)

	def on_object_removed(self, manager, obj):
		"""
		Object removed.
		"""
		path = obj.get_object_path()

		print('[ModemWatcher] %s: modem unmanaged by ModemManager' % path)

	def on_message_added (self, messaging, path, received, ud):
		messaging.list(None, self.on_messages, ud)

	def on_messages (self, messaging, task, ud):
		for m in messaging.list_finish(task):
			if m.get_state() != ModemManager.SmsState.RECEIVED:
				continue

			path = m.get_path()
			doc = {
				"sms": {
					"from": m.get_number(),
					"to": ud.own_numbers,
					"text": m.get_text(),
					"data": m.get_data(),
					"ts-req": m.get_timestamp(),
					"ts-del": m.get_discharge_timestamp(),
				},
			}

			print("---")
			yaml.dump(doc, sys.stdout, allow_unicode = True)
			ud.instance.fwd.post_sms(doc)

			messaging.delete(path, None, self.on_message_delete)

	def on_message_delete (self, messaging, task):
		messaging.delete_finish(task)

	def on_call_added (self, voice, path, ud):
		print("on_call_added()") # FIXME
		voice.list_calls(None, self.on_calls_added, ud)

	def on_incoming_call (self, call, ud):
		doc = {
			"call": {
				"from": call.get_number(),
				"to": ud.own_numbers,
				"multiparty": call.get_multiparty(),
			}
		}

		print("---")
		yaml.dump(doc, sys.stdout, allow_unicode = True)
		ud.instance.fwd.post_call(doc)

	def on_calls_sync (self, voice, task, ud):
		print("on_calls_sync()") # FIXME
		for c in voice.list_calls_finish(task):
			state = c.get_state()
			path = c.get_path()

			if (state == ModemManager.CallState.ACTIVE or
					state == ModemManager.CallState.RINGING_IN):
				c.hangup(None, self.on_call_hangup, ud)
			elif state == ModemManager.CallState.TERMINATED:
				voice.delete_call(path, None, self.on_call_delete, None)

	def on_calls_cleanup (self, voice, task, ud):
		print("on_calls_cleanup()") # FIXME
		for c in voice.list_calls_finish(task):
			if c.get_state() == ModemManager.CallState.TERMINATED:
				voice.delete_call(c.get_path(), None, self.on_call_delete, None)

	def on_calls_added (self, voice, task, ud):
		print("on_calls_added()") # FIXME

		hasCall = False
		for c in voice.list_calls_finish(task):
			state = c.get_state()
			nud = copy(ud)
			nud.call = c

			if state != ModemManager.CallState.RINGING_IN:
				continue

			if hasCall:
				c.hangup(None, self.on_call_hangup, nud)
			else:
				hasCall = True
				self.on_incoming_call(c, ud)

				if ud.instance.callam['enabled']:
					c.accept(None, self.on_call_accept, nud)
				else:
					c.hangup(None, self.on_call_hangup, nud)

	def on_call_change (self, call, old, new, reason, ud):
		print("on_call_change()") # FIXME
		if new == ModemManager.CallState.TERMINATED:
			ud.voice.list_calls(None, self.on_calls_cleanup, ud)
		else:
			call.hangup(None, self.on_call_hangup, ud)

		if ud.instance.callam_ringtone_proc is not None:
			os.killpg(ud.instance.callam_ringtone_proc.pid, signal.SIGKILL)
			ud.instance.callam_ringtone_proc.wait()
			ud.instance.callam_ringtone_proc = None

		if ud.instance.callam_proc is not None:
			os.killpg(ud.instance.callam_proc.pid, signal.SIGKILL)
			ud.instance.callam_proc.wait()
			ud.instance.callam_proc = None

			# reset the modem
			# fucking hate this cheap BS modem
			path = "%s/bConfigurationValue" % ud.device
			os.system('''echo -1 > ''' + path)
			os.system('''echo 1 > ''' + path)

		if ud.instance.callam_timer is not None:
			GLib.source_remove(ud.instance.callam_timer)
			ud.instance.callam_timer = None

	def on_call_hangup (self, call, task, ud):
		print("on_call_hangup()") # FIXME
		call.hangup_finish(task)
		ud.voice.list_calls(None, self.on_calls_cleanup, ud)

	def on_call_delete (self, voice, task, ud):
		try:
			voice.delete_call_finish(task)
		except: pass

	def on_call_accept (self, call, task, ud):
		try:
			call.accept_finish(task)
		except gi.repository.GLib.GError as e:
			sys.stderr.write("on_call_accept(): " + str(e))
			return
		print("on_call_accept()") # FIXME
		call.connect('state-changed', self.on_call_change, ud)

		if ud.instance.callam.get('ringtone-exec'):
			try:
				ud.instance.callam_ringtone_proc = subprocess.Popen(
					ud.instance.callam['ringtone-exec'],
					start_new_session = True)
			except Exception as e:
				sys.stderr.write(e + os.linesep)

		# The custom ModemManager will send AT+CPCMREG.
		# mmfwd-callam process will set up the serial, play the hello message
		# and record
		try:
			now = datetime.datetime.now(datetime.UTC)

			n_from = call.get_number() or ""

			dir = "rec/%02d-%02d" % (now.year, now.month)
			filename = now.isoformat(timespec = 'milliseconds') + '_' + n_from
			path = dir + '/' + filename

			os.makedirs(dir, exist_ok = True)

			env = os.environ.copy()
			env['MMFWD_CALLAM_PLAYBACK'] = str(ud.instance.callam['playback'])

			exec = [ ud.instance.callam['exec'], "/dev/" + ud.audio_port, path ]
			ud.instance.callam_proc = subprocess.Popen(
				exec,
				env = env,
				start_new_session = True)
			# 5 minutes timeout
			ud.instance.callam_timer = GLib.timeout_add_seconds(
				60 * 4,
				self.on_call_timeout,
				ud)
		except Exception as e:
			raise e

	def on_call_timeout (self, ud):
		ud.call.hangup(None, self.on_call_hangup, ud)
		ud.instance.callam_timer = None
		return False
