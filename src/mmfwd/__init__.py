from copy import copy
import os
import re
import subprocess
import sys
import threading
from typing import Any
import gi
import yaml
gi.require_version('ModemManager', '1.0')
from gi.repository import Gio, ModemManager

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

	def post (self, doc):
		cmd = list[str]()
		for arg in self.cmd:
			cmd.append(arg.format(
				sender = doc["sms"]["from"],
				to = doc["sms"]["to"],
				ts_req = doc["sms"]["ts-req"],
				ts_del = doc["sms"]["ts-del"],
			))

		with subprocess.Popen(cmd, stdin = subprocess.PIPE) as p:
			p.stdin.write(("--" + os.linesep).encode())
			yaml.dump(doc, p.stdin, encoding = 'utf-8', allow_unicode = True)
			p.stdin.close()

class Instance:
	def __init__ (self, conf: dict[str, Any]):
		self.mobj: str = None
		self.mid = ModemIdentity(conf.get("mid"))
		self.fwd = Forward(conf.get("fwd"))

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

		modem.connect('state-changed', self.on_modem_state_updated, ud)
		messaging.connect('added', self.on_message_added, ud)
		voice.connect('call-added', self.on_call_added, ud)

		# fire request to sync
		messaging.list(None, self.on_messages, ud)
		voice.list_calls(None, self.on_calls, ud)

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
		print('''[mmfwd] on_message_added: {a} {b} {c}'''.format(
			a = messaging,
			b = path,
			c = received,
		))

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

			print("--")
			yaml.dump(doc, sys.stdout, allow_unicode = True)
			ud.instance.fwd.post(doc)

			messaging.delete(path, None, self.on_message_delete)

	def on_message_delete (self, messaging, task):
		messaging.delete_finish(task)

	def on_call_added (self, voice, path, ud):
		voice.list_calls(None, self.on_calls, ud)

	def on_calls (self, voice, task, ud = None):
		for c in voice.list_calls_finish(task):
			state = c.get_state()
			path = c.get_path()
			nud = copy(ud)
			nud.call = c

			if state == ModemManager.CallState.ACTIVE:
				c.hangup(None, self.on_call_hangup, nud)
			elif state == ModemManager.CallState.RINGING_IN:
				if True:
					# FIXME
					# just hang up for now
					c.hangup(None, self.on_call_hangup, nud)
				else:
					c.accept(None, self.on_call_accept, nud)
			elif state == ModemManager.CallState.TERMINATED:
				voice.delete_call(path, None, self.on_call_delete, nud)

	def on_call_change (self, call, old, new, reason, ud):
		ud.voice.list_calls(None, self.on_calls, ud)

	def on_call_hangup (self, call, task, ud):
		call.hangup_finish(task)
		ud.voice.list_calls(None, self.on_calls, ud)

	def on_call_delete (self, voice, task, ud):
		voice.delete_call_finish(task)

	def on_call_accept (self, call, task, ud):
		call.accept_finish(task)
		call.connect('state-changed', self.on_call_change, ud)

		# The custom ModemManager will send AT+CPCMREG.
		# TODO: play the voice message
