# license: GPL-2.0-or-later
"""NVDA driver for user-installed Samsung television speech voices."""

import atexit
from array import array
import builtins
from collections import OrderedDict, deque
import ctypes
from ctypes import wintypes
import os
import queue
import socket
import struct
import subprocess
import threading
import time

import config
import globalVars
from logHandler import log
import nvwave
from speech.commands import IndexCommand
from synthDriverHandler import NumericDriverSetting, SynthDriver, VoiceInfo, synthDoneSpeaking, synthIndexReached
from synthDrivers._samsungTVVoices import firmwareStore

_ = getattr(builtins, "_", lambda text: text)

_HERE = os.path.dirname(__file__)
_ENGINE_DIR = os.path.join(_HERE, "_samsungTVVoices")
_QEMU_PATH = os.path.join(_ENGINE_DIR, "qemu-system-armw.exe")
_KERNEL_PATH = os.path.join(_ENGINE_DIR, "samsung-vmlinuz")

_MAGIC = 0x56544D53
_SPEAK = 1
_CANCEL = 2
_QUIT = 3
_READY = 101
_AUDIO = 102
_DONE = 103
_ERROR = 104
_CANCELLED = 105
_HEADER = struct.Struct("<IIII")
_OPTIONS = struct.Struct("<iiiii")
_MAGIC_BYTES = struct.pack("<I", _MAGIC)
_MAX_PAYLOAD = 8 << 20
_SESSION_HOST_NAME = "_samsungTVVoicesSessionHost"
_GUEST_MEMORY = "640M"

_VOICE_PARAMETERS = {}
_AVAILABLE_VOICES = OrderedDict()


def reloadInstalledVoices():
	global _VOICE_PARAMETERS, _AVAILABLE_VOICES
	parameters = {}
	voices = OrderedDict()
	for voiceId, details in firmwareStore.availableVoiceDefinitions().items():
		parameters[voiceId] = (details["languageId"], details["model"])
		voices[voiceId] = VoiceInfo(voiceId, details["name"], language=details["locale"])
	_VOICE_PARAMETERS = parameters
	_AVAILABLE_VOICES = voices
	return voices


reloadInstalledVoices()


class _HostError(RuntimeError):
	pass


class _IO_COUNTERS(ctypes.Structure):
	_fields_ = (
		("ReadOperationCount", ctypes.c_ulonglong),
		("WriteOperationCount", ctypes.c_ulonglong),
		("OtherOperationCount", ctypes.c_ulonglong),
		("ReadTransferCount", ctypes.c_ulonglong),
		("WriteTransferCount", ctypes.c_ulonglong),
		("OtherTransferCount", ctypes.c_ulonglong),
	)


class _BASIC_LIMITS(ctypes.Structure):
	_fields_ = (
		("PerProcessUserTimeLimit", ctypes.c_longlong),
		("PerJobUserTimeLimit", ctypes.c_longlong),
		("LimitFlags", wintypes.DWORD),
		("MinimumWorkingSetSize", ctypes.c_size_t),
		("MaximumWorkingSetSize", ctypes.c_size_t),
		("ActiveProcessLimit", wintypes.DWORD),
		("Affinity", ctypes.c_size_t),
		("PriorityClass", wintypes.DWORD),
		("SchedulingClass", wintypes.DWORD),
	)


class _EXTENDED_LIMITS(ctypes.Structure):
	_fields_ = (
		("BasicLimitInformation", _BASIC_LIMITS),
		("IoInfo", _IO_COUNTERS),
		("ProcessMemoryLimit", ctypes.c_size_t),
		("JobMemoryLimit", ctypes.c_size_t),
		("PeakProcessMemoryUsed", ctypes.c_size_t),
		("PeakJobMemoryUsed", ctypes.c_size_t),
	)


class _ProcessJob:
	"""Ensure the emulator cannot survive the NVDA process that owns it."""

	_KILL_ON_JOB_CLOSE = 0x00002000
	_EXTENDED_LIMIT_INFORMATION = 9

	def __init__(self, process):
		self._handle = None
		kernel32 = ctypes.windll.kernel32
		kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
		kernel32.CreateJobObjectW.restype = wintypes.HANDLE
		kernel32.SetInformationJobObject.argtypes = (
			wintypes.HANDLE,
			ctypes.c_int,
			ctypes.c_void_p,
			wintypes.DWORD,
		)
		kernel32.SetInformationJobObject.restype = wintypes.BOOL
		kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
		kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
		kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
		kernel32.CloseHandle.restype = wintypes.BOOL
		handle = kernel32.CreateJobObjectW(None, None)
		if not handle:
			raise ctypes.WinError()
		limits = _EXTENDED_LIMITS()
		limits.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
		if not kernel32.SetInformationJobObject(
			handle,
			self._EXTENDED_LIMIT_INFORMATION,
			ctypes.byref(limits),
			ctypes.sizeof(limits),
		):
			kernel32.CloseHandle(handle)
			raise ctypes.WinError()
		if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
			kernel32.CloseHandle(handle)
			raise ctypes.WinError()
		self._handle = handle

	def close(self):
		if self._handle:
			ctypes.windll.kernel32.CloseHandle(self._handle)
			self._handle = None


class _SamsungHost:
	"""Own one warm ARM guest and its framed speech protocol."""

	def __init__(self):
		self._process = None
		self._processJob = None
		self._connection = None
		self._stream = None
		self._messages = queue.Queue(maxsize=64)
		self._writeLock = threading.Lock()
		self._lifecycleLock = threading.Lock()
		self._generationLock = threading.Lock()
		self._generation = 0
		self._recentErrors = deque(maxlen=20)

	@staticmethod
	def _readExact(stream, size):
		data = bytearray()
		while len(data) < size:
			part = stream.read(size - len(data))
			if not part:
				raise EOFError
			data.extend(part)
		return bytes(data)

	def _readHeader(self, stream):
		prefix = bytearray()
		while not prefix.endswith(_MAGIC_BYTES):
			prefix.extend(self._readExact(stream, 1))
			if len(prefix) > len(_MAGIC_BYTES):
				del prefix[:-len(_MAGIC_BYTES)]
		return _MAGIC_BYTES + self._readExact(stream, _HEADER.size - len(_MAGIC_BYTES))

	def _putMessage(self, process, messages, message):
		while process is self._process:
			try:
				messages.put(message, timeout=0.1)
				return
			except queue.Full:
				continue

	def _reader(self, process, messages, stream):
		try:
			while process is self._process:
				magic, messageType, generation, size = _HEADER.unpack(self._readHeader(stream))
				if magic != _MAGIC or size > _MAX_PAYLOAD:
					raise _HostError("The Samsung helper returned an invalid message.")
				payload = self._readExact(stream, size) if size else b""
				self._putMessage(process, messages, (messageType, generation, payload))
		except EOFError:
			if process is self._process:
				self._putMessage(process, messages, (None, 0, b"The Samsung helper stopped unexpectedly."))
		except Exception as error:
			if process is self._process:
				self._putMessage(process, messages, (None, 0, str(error).encode("utf-8", "replace")))

	def _stderrReader(self, process):
		try:
			for line in iter(process.stderr.readline, b""):
				if process is not self._process:
					return
				text = line.decode("utf-8", "replace").strip()
				if text:
					self._recentErrors.append(text)
		except Exception:
			pass

	def nextGeneration(self):
		with self._generationLock:
			self._generation = (self._generation + 1) & 0xFFFFFFFF
			if self._generation == 0:
				self._generation = 1
			return self._generation

	def isRunning(self):
		return self._process is not None and self._process.poll() is None

	def _startOnce(self):
		with self._lifecycleLock:
			if self.isRunning():
				return
			self._stopUnlocked()
			self._messages = queue.Queue(maxsize=64)
			self._recentErrors.clear()
			reservation = socket.socket()
			reservation.bind(("127.0.0.1", 0))
			port = reservation.getsockname()[1]
			reservation.close()
			arguments = [
				_QEMU_PATH,
				"-M", "virt", "-cpu", "cortex-a15", "-smp", "2", "-m", _GUEST_MEMORY,
				"-L", _ENGINE_DIR,
				"-kernel", _KERNEL_PATH,
				"-initrd", firmwareStore.runtimePath(),
				"-append", "rdinit=/init quiet loglevel=0",
				"-display", "none", "-monitor", "none", "-serial", "null",
				"-device", "virtio-serial-device",
				"-chardev", f"socket,id=nvda,host=127.0.0.1,port={port},server=on,wait=off,nodelay=on",
				"-device", "virtserialport,chardev=nvda,name=org.onj.samsung",
				"-no-reboot",
			]
			try:
				process = subprocess.Popen(
					arguments,
					stdin=subprocess.DEVNULL,
					stdout=subprocess.DEVNULL,
					stderr=subprocess.PIPE,
					bufsize=0,
					cwd=_ENGINE_DIR,
				)
			except OSError as error:
				raise _HostError("The Samsung speech helper could not be started.") from error
			self._process = process
			try:
				self._processJob = _ProcessJob(process)
			except Exception:
				self._processJob = None
				log.debugWarning("Samsung TV Voices: process job protection is unavailable", exc_info=True)
			connection = None
			connectStarted = time.monotonic()
			while connection is None and time.monotonic() - connectStarted < 5:
				if process.poll() is not None:
					break
				try:
					connection = socket.create_connection(("127.0.0.1", port), timeout=0.25)
				except OSError:
					time.sleep(0.05)
			if connection is None:
				detail = "; ".join(self._recentErrors)
				if process.poll() is not None and process.stderr is not None:
					try:
						detail = process.stderr.read().decode("utf-8", "replace").strip() or detail
					except Exception:
						pass
				self._stopUnlocked()
				raise _HostError(detail or "QEMU did not open the Samsung speech channel.")
			connection.settimeout(None)
			self._connection = connection
			self._stream = connection.makefile("rwb", buffering=0)
			messages = self._messages
			threading.Thread(
				target=self._reader,
				args=(process, messages, self._stream),
				name="Samsung TV Voices host reader",
				daemon=True,
			).start()
			threading.Thread(
				target=self._stderrReader,
				args=(process,),
				name="Samsung TV Voices host diagnostics",
				daemon=True,
			).start()
			try:
				messageType, _, payload = messages.get(timeout=15)
			except queue.Empty as error:
				detail = "; ".join(self._recentErrors)
				self._stopUnlocked()
				raise _HostError(detail or "The Samsung speech helper did not become ready.") from error
			if messageType != _READY:
				detail = payload.decode("utf-8", "replace") or "; ".join(self._recentErrors)
				self._stopUnlocked()
				raise _HostError(detail or "The Samsung speech helper failed during startup.")

	def start(self):
		if self.isRunning():
			return
		lastError = None
		for attempt in range(2):
			try:
				self._startOnce()
				return
			except _HostError as error:
				lastError = error
				if attempt == 0:
					log.debugWarning("Samsung TV Voices: retrying cold helper startup after: %s", error)
					time.sleep(0.1)
		raise lastError

	def send(self, messageType, generation=0, payload=b""):
		process = self._process
		stream = self._stream
		if process is None or process.poll() is not None or stream is None:
			raise _HostError("The Samsung speech helper is not running.")
		packet = _HEADER.pack(_MAGIC, messageType, generation, len(payload)) + payload
		try:
			with self._writeLock:
				stream.write(packet)
				stream.flush()
		except (OSError, ValueError) as error:
			raise _HostError("The Samsung speech helper connection was lost.") from error

	def getMessage(self, timeout):
		return self._messages.get(timeout=timeout)

	def cancel(self, generation):
		if generation is None or not self.isRunning():
			return
		try:
			self.send(_CANCEL, generation)
		except _HostError:
			pass

	def _stopUnlocked(self):
		process = self._process
		stream = self._stream
		connection = self._connection
		self._process = None
		self._stream = None
		self._connection = None
		if process is not None and process.poll() is None:
			try:
				with self._writeLock:
					if stream is not None:
						stream.write(_HEADER.pack(_MAGIC, _QUIT, 0, 0))
						stream.flush()
			except (OSError, ValueError):
				pass
			try:
				process.wait(timeout=2)
			except subprocess.TimeoutExpired:
				process.terminate()
				try:
					process.wait(timeout=1)
				except subprocess.TimeoutExpired:
					process.kill()
					process.wait(timeout=1)
		if self._processJob is not None:
			self._processJob.close()
			self._processJob = None
		for channel in (stream, connection):
			try:
				if channel is not None:
					channel.close()
			except Exception:
				pass
		if process is not None:
			for processStream in (process.stderr,):
				try:
					processStream.close()
				except Exception:
					pass

	def stop(self):
		with self._lifecycleLock:
			self._stopUnlocked()


def _getSessionHost():
	host = getattr(globalVars, _SESSION_HOST_NAME, None)
	if host is None or not all(hasattr(host, method) for method in ("start", "send", "getMessage", "stop")):
		host = _SamsungHost()
		setattr(globalVars, _SESSION_HOST_NAME, host)
	return host


def _shutdownSessionHost():
	host = getattr(globalVars, _SESSION_HOST_NAME, None)
	if host is not None:
		try:
			host.stop()
		except Exception:
			pass
		try:
			delattr(globalVars, _SESSION_HOST_NAME)
		except AttributeError:
			pass


atexit.register(_shutdownSessionHost)


def _makePlayer():
	try:
		return nvwave.WavePlayer(
			channels=1,
			samplesPerSec=48000,
			bitsPerSample=16,
			outputDevice=config.conf["audio"]["outputDevice"],
		)
	except Exception:
		return nvwave.WavePlayer(1, 48000, 16)


class SynthDriver(SynthDriver):
	name = "samsungTVVoices"
	description = _("Samsung TV Voices")
	supportedSettings = (
		SynthDriver.VoiceSetting(),
		SynthDriver.RateSetting(minStep=5),
		SynthDriver.PitchSetting(minStep=5),
		SynthDriver.VolumeSetting(minStep=5),
		NumericDriverSetting(
			"headSize",
			_('&Head size'),
			availableInSettingsRing=True,
			defaultVal=50,
			minVal=0,
			maxVal=100,
			minStep=1,
			normalStep=5,
			largeStep=10,
			displayName=_("Head size"),
		),
	)
	supportedCommands = {IndexCommand}
	supportedNotifications = {synthIndexReached, synthDoneSpeaking}

	@classmethod
	def check(cls):
		return all(os.path.isfile(path) for path in (_QEMU_PATH, _KERNEL_PATH)) and firmwareStore.runtimeAvailable()

	def __init__(self):
		super().__init__()
		reloadInstalledVoices()
		if not _AVAILABLE_VOICES:
			raise RuntimeError("Install at least one Samsung TV firmware pack before selecting this synthesizer.")
		self._voice = "en_GB_female" if "en_GB_female" in _AVAILABLE_VOICES else next(iter(_AVAILABLE_VOICES))
		self._rate = 50
		self._pitch = 50
		self._volume = 100
		self._headSize = 50
		self._player = _makePlayer()
		self._host = _getSessionHost()
		self._jobs = queue.Queue()
		self._activeGeneration = None
		self._currentGeneration = self._host.nextGeneration()
		self._stateLock = threading.Lock()
		self._stopping = threading.Event()
		self._workerThread = threading.Thread(
			target=self._worker,
			name="Samsung TV Voices synth",
			daemon=True,
		)
		self._workerThread.start()

	def _get_availableVoices(self):
		return _AVAILABLE_VOICES

	def _get_voice(self):
		return self._voice

	def _set_voice(self, value):
		if value in _VOICE_PARAMETERS:
			self._voice = value

	def refreshAvailableVoices(self):
		previous = self._voice
		reloadInstalledVoices()
		if previous in _AVAILABLE_VOICES:
			return self._voice
		if _AVAILABLE_VOICES:
			self._voice = next(iter(_AVAILABLE_VOICES))
			return self._voice
		raise ValueError("No Samsung TV voices remain installed.")

	def refreshFirmwareRuntime(self):
		self.cancel()
		voice = self.refreshAvailableVoices()
		self._restartHost()
		return _AVAILABLE_VOICES[voice].name

	def _get_rate(self):
		return self._rate

	def _set_rate(self, value):
		self._rate = max(0, min(100, int(value)))

	def _get_pitch(self):
		return self._pitch

	def _set_pitch(self, value):
		self._pitch = max(0, min(100, int(value)))

	def _get_volume(self):
		return self._volume

	def _set_volume(self, value):
		self._volume = max(0, min(100, int(value)))

	def _get_headSize(self):
		return self._headSize

	def _set_headSize(self, value):
		self._headSize = max(0, min(100, int(value)))

	@staticmethod
	def _mapRate(value):
		return round((max(0, min(100, value)) - 50) / 10)

	@staticmethod
	def _mapPitch(value):
		return round((max(0, min(100, value)) - 50) * 0.6)

	@staticmethod
	def _mapHeadSize(value):
		return round((max(0, min(100, value)) - 50) / 5)

	@staticmethod
	def _scaleVolume(data, value):
		value = max(0, min(100, value))
		if value == 100:
			return data
		if value == 0:
			return bytes(len(data))
		samples = array("h")
		samples.frombytes(data)
		gain = value / 100.0
		for index, sample in enumerate(samples):
			samples[index] = round(sample * gain)
		return samples.tobytes()

	def _isCurrent(self, generation):
		with self._stateLock:
			return not self._stopping.is_set() and generation == self._currentGeneration

	def speak(self, speechSequence):
		events = []
		textParts = []

		def flushText():
			text = "".join(textParts).strip()
			textParts.clear()
			if text:
				events.append(("text", text))

		for item in speechSequence:
			if isinstance(item, str):
				textParts.append(item)
			elif isinstance(item, IndexCommand):
				flushText()
				events.append(("index", item.index))
		flushText()
		if not events:
			return
		with self._stateLock:
			generation = self._currentGeneration
		self._jobs.put((generation, events))

	def cancel(self):
		with self._stateLock:
			active = self._activeGeneration
			self._currentGeneration = self._host.nextGeneration()
		self._host.cancel(active)
		try:
			while True:
				self._jobs.get_nowait()
				self._jobs.task_done()
		except queue.Empty:
			pass
		try:
			self._player.stop()
		except Exception:
			log.debugWarning("Samsung TV Voices: audio cancellation failed", exc_info=True)

	def pause(self, switch):
		try:
			self._player.pause(switch)
		except Exception:
			log.debugWarning("Samsung TV Voices: audio pause failed", exc_info=True)

	def terminate(self):
		self._stopping.set()
		self.cancel()
		self._jobs.put(None)
		self._workerThread.join(timeout=3)
		try:
			self._player.close()
		except Exception:
			pass
		super().terminate()

	def _worker(self):
		try:
			self._host.start()
		except Exception:
			log.error("Samsung TV Voices host startup failed", exc_info=True)
		while not self._stopping.is_set():
			job = self._jobs.get()
			if job is None:
				self._jobs.task_done()
				return
			try:
				if self._isCurrent(job[0]):
					self._render(*job)
			except Exception:
				log.error("Samsung TV Voices synthesis failed", exc_info=True)
			finally:
				self._jobs.task_done()

	def _restartHost(self):
		self._host.stop()
		self._host.start()

	def _synthesizeText(self, generation, text):
		if not self._host.isRunning():
			self._host.start()
		voice = self._voice
		language, model = _VOICE_PARAMETERS[voice]
		payload = _OPTIONS.pack(
			language,
			model,
			self._mapRate(self._rate),
			self._mapPitch(self._pitch),
			self._mapHeadSize(self._headSize),
		) + text.encode("utf-8")
		self._host.send(_SPEAK, generation, payload)
		cancelledAt = None
		lastProgressAt = time.monotonic()
		while True:
			if not self._isCurrent(generation) and cancelledAt is None:
				cancelledAt = time.monotonic()
			try:
				messageType, messageGeneration, payload = self._host.getMessage(0.25)
			except queue.Empty:
				if cancelledAt is not None and time.monotonic() - cancelledAt > 1:
					self._restartHost()
					return None
				if time.monotonic() - lastProgressAt > 30 and self._isCurrent(generation):
					self._restartHost()
					raise _HostError("The Samsung speech helper stopped responding.")
				continue
			lastProgressAt = time.monotonic()
			if messageType is None:
				self._restartHost()
				raise _HostError(payload.decode("utf-8", "replace"))
			if messageGeneration != generation:
				continue
			if messageType == _AUDIO:
				if self._isCurrent(generation):
					self._player.feed(self._scaleVolume(payload, self._volume))
			elif messageType == _DONE:
				return True
			elif messageType == _CANCELLED:
				return None
			elif messageType == _ERROR:
				detail = payload.decode("utf-8", "replace")
				raise _HostError(
					f"{detail} Voice: {voice}; language: {language}; model: {model}."
				)

	def _render(self, generation, events):
		with self._stateLock:
			if generation != self._currentGeneration:
				return
			self._activeGeneration = generation
		try:
			for eventType, value in events:
				if not self._isCurrent(generation):
					return
				if eventType == "index":
					synthIndexReached.notify(synth=self, index=value)
					continue
				completed = self._synthesizeText(generation, value)
				if completed is None or not self._isCurrent(generation):
					return
				self._player.idle()
			if self._isCurrent(generation):
				synthDoneSpeaking.notify(synth=self)
		finally:
			with self._stateLock:
				if self._activeGeneration == generation:
					self._activeGeneration = None
