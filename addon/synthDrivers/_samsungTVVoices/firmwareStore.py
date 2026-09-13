# license: GPL-2.0-or-later
"""Samsung firmware download, extraction, and locally installed runtime storage."""

from collections import OrderedDict
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile

import globalVars


ENGINE_DIR = os.path.dirname(__file__)
QEMU_PATH = os.path.join(ENGINE_DIR, "qemu-system-armw.exe")
KERNEL_PATH = os.path.join(ENGINE_DIR, "samsung-vmlinuz")
CLEAN_INITRAMFS_PATH = os.path.join(ENGINE_DIR, "samsung-clean-initramfs.cpio")
EXTRACTOR_INITRAMFS_PATH = os.path.join(ENGINE_DIR, "samsung-extractor-initramfs.cpio")
UNIXTRACT_PATH = os.path.join(ENGINE_DIR, "tools", "unixtract.exe")
_installLock = threading.Lock()
_EXTRACTOR_ATTEMPTS = 2
_EXTRACTOR_CONNECT_TIMEOUT_SECONDS = 60
_EXTRACTOR_RESULT_TIMEOUT_SECONDS = 900
_TAR_END_SIZE = 1024
_INSTALLER_RETENTION_SECONDS = 24 * 60 * 60


class _ExtractorDisconnected(RuntimeError):
	pass

PACKS = OrderedDict((
	("europe", {
		"name": "European and UK firmware",
		"family": "T-KTMDEUC",
		"version": "1410.1",
		"sizeLabel": "1,289.06 MB",
		"sha256": "AC092BAE5CD86EC4EB76D3FE85DF12F6036A6CAEB4488E3C345EAE95138149C7",
		"url": "https://org.downloadcenter.samsung.com/downloadfile/ContentsFile.aspx?CDSite=UNI_IE&OriginYN=N&ModelType=N&ModelName=UE50MU6120K&CttFileID=7176347&CDCttType=FM&VPath=FM%2F202605%2F20260507184411893%2FT-KTMDEUC.zip",
		"supportUrl": "https://www.samsung.com/ie/support/model/UE50MU6120KXXU/",
	}),
	("northAmerica", {
		"name": "North American firmware",
		"family": "T-KTMAKUC",
		"version": "1410.1",
		"sizeLabel": "1,231.15 MB",
		"sha256": "F09733B139FD4BE18468AD211BA5E867496D626932A2E271027ABB4BF59B2D34",
		"url": "https://org.downloadcenter.samsung.com/downloadfile/ContentsFile.aspx?CDSite=UNI_LATIN_EN&OriginYN=N&ModelType=N&ModelName=UN50MU6300F&CttFileID=7049647&CDCttType=FM&VPath=FM%2F202605%2F20260511142740436%2FT-KTMAKUC.zip",
		"supportUrl": "https://www.samsung.com/latin_en/support/model/UN50MU6300FXZA/",
	}),
))

LANGUAGES = (
	("ko_KR", 0, "Korean", "ko_KR", "kr", "Ko"),
	("en_US", 1, "US English", "en_US", "usen", "EnUS"),
	("en_GB", 2, "UK English", "en_GB", "uken", "EnGB"),
	("de_DE", 4, "German", "de_DE", "ge", "De"),
	("fr_FR", 5, "French", "fr_FR", "fr", "Fr"),
	("es_ES", 6, "Spanish", "es_ES", "es", "Es"),
	("it_IT", 7, "Italian", "it_IT", "it", "It"),
	("nl_NL", 8, "Dutch", "nl_NL", "nl", "Nl"),
	("pl_PL", 11, "Polish", "pl_PL", "pl", "Pl"),
	("ru_RU", 12, "Russian", "ru_RU", "ru", "Ru"),
	("da_DK", 13, "Danish", "da_DK", "dk", "Dk"),
	("sv_SE", 14, "Swedish", "sv_SE", "se", "Se"),
	("fi_FI", 15, "Finnish", "fi_FI", "fi", "Fi"),
	("nb_NO", 16, "Norwegian", "nb_NO", "no", "No"),
	("pt_PT", 17, "Portuguese", "pt_PT", "pt", "Pt"),
)


def dataRoot():
	return os.path.join(globalVars.appArgs.configPath, "samsungTVVoices")


def packsRoot():
	return os.path.join(dataRoot(), "packs")


def runtimePath():
	return os.path.join(dataRoot(), "samsung-runtime-initramfs.cpio")


def settingsPath():
	return os.path.join(dataRoot(), "settings.json")


def _cleanupStaleInstallerFiles(now=None):
	root = dataRoot()
	if not os.path.isdir(root):
		return 0
	cutoff = (time.time() if now is None else now) - _INSTALLER_RETENTION_SECONDS
	candidates = []
	familyPrefixes = tuple("%s-" % pack["family"] for pack in PACKS.values())
	try:
		entries = tuple(os.scandir(root))
	except OSError:
		entries = ()
	for entry in entries:
		if entry.name.startswith("firmware-") and entry.is_dir(follow_symlinks=False):
			candidates.append(entry.path)
		elif (
			entry.is_file(follow_symlinks=False)
			and entry.name.startswith(familyPrefixes)
			and entry.name.endswith((".zip", ".zip.part"))
		):
			candidates.append(entry.path)
	removed = 0
	for path in candidates:
		try:
			if os.path.getmtime(path) >= cutoff:
				continue
			if os.path.isdir(path):
				shutil.rmtree(path)
			else:
				os.remove(path)
			removed += 1
		except OSError:
			continue
	return removed


def cleanupStaleInstallerFiles():
	if not _installLock.acquire(blocking=False):
		return 0
	try:
		return _cleanupStaleInstallerFiles()
	finally:
		_installLock.release()


def loadSettings():
	settings = {"updateInterval": "daily", "firmwarePromptShown": False}
	try:
		with open(settingsPath(), "r", encoding="utf-8") as stream:
			loaded = json.load(stream)
		if isinstance(loaded, dict):
			settings.update(loaded)
	except (OSError, ValueError):
		pass
	if settings["updateInterval"] not in ("never", "hourly", "daily"):
		settings["updateInterval"] = "daily"
	return settings


def saveSettings(settings):
	os.makedirs(dataRoot(), exist_ok=True)
	target = settingsPath()
	temporary = target + ".tmp"
	with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
		json.dump(settings, stream, indent=2, sort_keys=True)
		stream.write("\n")
	os.replace(temporary, target)


def packPath(packId):
	return os.path.join(packsRoot(), packId)


def isPackInstalled(packId):
	return os.path.isfile(os.path.join(packPath(packId), "pack.json"))


def installedPackIds():
	return tuple(packId for packId in PACKS if isPackInstalled(packId))


def _runtimeRoots():
	return [os.path.join(packPath(packId), "runtime") for packId in installedPackIds()]


def availableVoiceDefinitions():
	voices = OrderedDict()
	for localeKey, languageId, languageName, locale, suffix, modelFolder in LANGUAGES:
		languageFound = any(
			os.path.isfile(os.path.join(root, "Lib", "libSMT_lang_%s.so" % suffix)) and
			os.path.isdir(os.path.join(root, "Model", modelFolder))
			for root in _runtimeRoots()
		)
		if not languageFound:
			continue
		for model, gender in ((0, "Female"), (1, "Male")):
			prefix = "smt%s_" % ("f" if model == 0 else "m")
			modelFound = any(
				any(name.startswith(prefix) for name in os.listdir(os.path.join(root, "Model", modelFolder)))
				for root in _runtimeRoots()
				if os.path.isdir(os.path.join(root, "Model", modelFolder))
			)
			if modelFound:
				voiceId = "%s_%s" % (localeKey, gender.lower())
				voices[voiceId] = {
					"languageId": languageId,
					"model": model,
					"name": "%s %s" % (languageName, gender),
					"locale": locale,
				}
	return voices


def _download(url, destination, progress):
	partial = destination + ".part"
	received = os.path.getsize(partial) if os.path.isfile(partial) else 0
	headers = {"User-Agent": "SamsungTVVoices/1.0"}
	if received:
		headers["Range"] = "bytes=%d-" % received
	request = urllib.request.Request(url, headers=headers)
	with urllib.request.urlopen(request, timeout=60) as response:
		if received and getattr(response, "status", 200) != 206:
			received = 0
			mode = "wb"
		else:
			mode = "ab" if received else "wb"
		remaining = int(response.headers.get("Content-Length") or 0)
		total = received + remaining if remaining else 0
		with open(partial, mode) as output:
			while True:
				chunk = response.read(1024 * 1024)
				if not chunk:
					break
				output.write(chunk)
				received += len(chunk)
				progress("download", received, total)
	os.replace(partial, destination)


def _sha256(path):
	digest = hashlib.sha256()
	with open(path, "rb") as stream:
		for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest().upper()


def _findUpgradeMsd(firmwareZip, destination):
	with zipfile.ZipFile(firmwareZip) as archive:
		members = [item for item in archive.infolist() if item.filename.lower().endswith("/upgrade.msd") or item.filename.lower() == "upgrade.msd"]
		if len(members) != 1:
			raise RuntimeError("The Samsung package did not contain exactly one upgrade.msd file.")
		with archive.open(members[0]) as source, open(destination, "wb") as output:
			shutil.copyfileobj(source, output, 4 * 1024 * 1024)


def _findPlatformImage(folder):
	matches = []
	for root, _, files in os.walk(folder):
		for name in files:
			if name.casefold() == "platform.img":
				matches.append(os.path.join(root, name))
	if len(matches) != 1:
		raise RuntimeError("The decrypted firmware did not contain exactly one platform.img file.")
	return matches[0]


def _readLine(connection, limit=4096):
	data = bytearray()
	while len(data) < limit:
		try:
			part = connection.recv(1)
		except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, socket.timeout) as error:
			raise _ExtractorDisconnected(
				"The firmware extraction helper disconnected before reporting its status."
			) from error
		if not part:
			raise _ExtractorDisconnected(
				"The firmware extraction helper disconnected before reporting its status."
			)
		if part == b"\n":
			return bytes(data).decode("ascii", "replace")
		data.extend(part)
	raise RuntimeError("The firmware extraction helper returned an invalid response.")


def _receiveExact(connection, destination, size, progress):
	received = 0
	with open(destination, "wb") as output:
		while received < size:
			try:
				chunk = connection.recv(min(1024 * 1024, size - received))
			except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, socket.timeout) as error:
				raise _ExtractorDisconnected(
					"The firmware extraction helper disconnected before completing its result."
				) from error
			if not chunk:
				raise _ExtractorDisconnected(
					"The firmware extraction helper disconnected before completing its result."
				)
			output.write(chunk)
			received += len(chunk)
			progress("extract", received, size)


def _receiveStream(connection, destination, maximumSize, progress):
	received = 0
	tail = bytearray()
	with open(destination, "wb") as output:
		while True:
			try:
				chunk = connection.recv(1024 * 1024)
			except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, socket.timeout):
				break
			if not chunk:
				break
			received += len(chunk)
			if received > maximumSize:
				raise RuntimeError("The firmware extraction helper returned too much data.")
			output.write(chunk)
			tail.extend(chunk)
			if len(tail) > _TAR_END_SIZE:
				del tail[:-_TAR_END_SIZE]
			progress("extract", received, 0)
	if received == 0:
		raise _ExtractorDisconnected("The firmware extraction helper returned no speech data.")
	if len(tail) < _TAR_END_SIZE or any(tail):
		raise _ExtractorDisconnected(
			"The firmware extraction helper disconnected before completing its TAR result."
		)


def _extractSpeechTarOnce(platformImage, destinationTar, progress):
	reservation = socket.socket()
	reservation.bind(("127.0.0.1", 0))
	port = reservation.getsockname()[1]
	reservation.close()
	arguments = [
		QEMU_PATH, "-M", "virt", "-cpu", "cortex-a15", "-smp", "2", "-m", "512M",
		"-L", ENGINE_DIR, "-kernel", KERNEL_PATH, "-initrd", EXTRACTOR_INITRAMFS_PATH,
		"-append", "rdinit=/init quiet loglevel=0 samsung.mode=extract",
		"-drive", "file=%s,format=raw,if=none,id=firmware,readonly=on" % platformImage,
		"-device", "virtio-blk-device,drive=firmware",
		"-display", "none", "-monitor", "none", "-serial", "null",
		"-device", "virtio-serial-device",
		"-chardev", "socket,id=nvda,host=127.0.0.1,port=%d,server=on,wait=off,nodelay=on" % port,
		"-device", "virtserialport,chardev=nvda,name=org.onj.samsung.extract",
		"-no-reboot",
	]
	diagnostics = tempfile.TemporaryFile()
	process = subprocess.Popen(
		arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=diagnostics,
		cwd=ENGINE_DIR, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
	)
	connection = None
	failure = None
	try:
		connectDeadline = time.monotonic() + _EXTRACTOR_CONNECT_TIMEOUT_SECONDS
		while time.monotonic() < connectDeadline:
			if process.poll() is not None:
				break
			try:
				connection = socket.create_connection(("127.0.0.1", port), timeout=0.25)
				break
			except OSError:
				time.sleep(0.05)
		if connection is None:
			raise _ExtractorDisconnected("The firmware extraction helper could not be reached.")
		connection.settimeout(_EXTRACTOR_RESULT_TIMEOUT_SECONDS)
		line = _readLine(connection)
		if line.startswith("ERROR "):
			raise RuntimeError(line[6:] or "The firmware extraction helper failed.")
		if line == "SAMSUNG_TTS_TAR_STREAM":
			_receiveStream(connection, destinationTar, 512 * 1024 * 1024, progress)
		else:
			parts = line.split()
			if len(parts) != 2 or parts[0] != "SAMSUNG_TTS_TAR":
				raise RuntimeError("The firmware extraction helper returned an invalid header.")
			size = int(parts[1])
			if size <= 0 or size > 512 * 1024 * 1024:
				raise RuntimeError("The firmware extraction helper returned an invalid result size.")
			_receiveExact(connection, destinationTar, size, progress)
	except Exception as error:
		failure = error
	finally:
		if connection is not None:
			connection.close()
		if process.poll() is None:
			process.terminate()
			try:
				process.wait(3)
			except subprocess.TimeoutExpired:
				process.kill()
				process.wait(2)
		diagnostics.seek(0)
		detail = diagnostics.read(4096).decode("utf-8", "replace").strip()
		diagnostics.close()
	if failure is not None:
		message = str(failure)
		if detail:
			message = "%s Helper details: %s" % (message, detail[-2000:])
		if isinstance(failure, _ExtractorDisconnected):
			raise _ExtractorDisconnected(message) from failure
		raise RuntimeError(message) from failure


def _extractSpeechTar(platformImage, destinationTar, progress):
	lastError = None
	for attempt in range(_EXTRACTOR_ATTEMPTS):
		try:
			return _extractSpeechTarOnce(platformImage, destinationTar, progress)
		except _ExtractorDisconnected as error:
			lastError = error
			try:
				os.remove(destinationTar)
			except OSError:
				pass
			if attempt + 1 < _EXTRACTOR_ATTEMPTS:
				time.sleep(0.5)
	raise RuntimeError(
		"The firmware extraction helper disconnected twice before completing. "
		"The verified Samsung download has been retained, so trying again will not download it again. "
		"Restart NVDA and retry; if this continues, security software may be stopping the bundled helper. "
		"Last helper error: %s" % lastError
	) from lastError


def _safeTarExtract(archivePath, destination):
	root = os.path.abspath(destination)
	with tarfile.open(archivePath, "r:") as archive:
		for member in archive:
			name = member.name.replace("\\", "/").lstrip("/")
			if not name or name.startswith("../") or "/../" in name or member.issym() or member.islnk():
				continue
			target = os.path.abspath(os.path.join(root, *name.split("/")))
			if os.path.commonpath((root, target)) != root:
				raise RuntimeError("The firmware extractor returned an unsafe path.")
			if member.isdir():
				os.makedirs(target, exist_ok=True)
			elif member.isfile():
				os.makedirs(os.path.dirname(target), exist_ok=True)
				source = archive.extractfile(member)
				if source is None:
					raise RuntimeError("The firmware extractor returned an unreadable file.")
				with source, open(target, "wb") as output:
					shutil.copyfileobj(source, output, 1024 * 1024)


def _normaliseRuntime(extracted, runtime):
	voiceRoot = os.path.join(extracted, "usr", "share", "voice", "tts", "smt_vd")
	if not os.path.isdir(voiceRoot):
		raise RuntimeError("The selected firmware did not contain Samsung TV speech data.")
	shutil.copytree(voiceRoot, runtime, dirs_exist_ok=True)
	engineCandidates = (
		os.path.join(extracted, "usr", "lib", "libSMT_engine_sh.so"),
		os.path.join(extracted, "lib", "libSMT_engine_sh.so"),
	)
	engine = next((path for path in engineCandidates if os.path.isfile(path)), None)
	if engine is None:
		raise RuntimeError("The selected firmware did not contain the Samsung speech engine.")
	os.makedirs(os.path.join(runtime, "Lib"), exist_ok=True)
	shutil.copy2(engine, os.path.join(runtime, "Lib", "libSMT_engine_sh.so"))
	if not os.path.isfile(os.path.join(runtime, "Data", "symbol.ini")):
		raise RuntimeError("The selected firmware contained incomplete Samsung speech data.")


def _writeNewcEntry(stream, name, mode, data=b"", inode=1):
	nameBytes = name.encode("utf-8") + b"\0"
	fields = (inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(nameBytes), 0)
	header = b"070701" + b"".join(("%08x" % value).encode("ascii") for value in fields)
	stream.write(header)
	stream.write(nameBytes)
	stream.write(b"\0" * ((4 - ((len(header) + len(nameBytes)) % 4)) % 4))
	stream.write(data)
	stream.write(b"\0" * ((4 - (len(data) % 4)) % 4))


def rebuildRuntime():
	os.makedirs(dataRoot(), exist_ok=True)
	target = runtimePath()
	if not installedPackIds():
		try:
			os.remove(target)
		except FileNotFoundError:
			pass
		return
	merged = tempfile.mkdtemp(prefix="runtime-", dir=dataRoot())
	try:
		for root in _runtimeRoots():
			shutil.copytree(root, merged, dirs_exist_ok=True)
		temporary = target + ".tmp"
		with open(temporary, "wb") as output, open(CLEAN_INITRAMFS_PATH, "rb") as clean:
			shutil.copyfileobj(clean, output, 1024 * 1024)
			inode = 1000
			for root, directories, files in os.walk(merged):
				directories.sort()
				files.sort()
				relativeRoot = os.path.relpath(root, merged)
				archiveRoot = "opt/smt/runtime" if relativeRoot == "." else "opt/smt/runtime/" + relativeRoot.replace("\\", "/")
				_writeNewcEntry(output, archiveRoot, 0o040755, inode=inode)
				inode += 1
				for name in files:
					path = os.path.join(root, name)
					with open(path, "rb") as stream:
						data = stream.read()
					_writeNewcEntry(output, archiveRoot + "/" + name, 0o100644, data, inode)
					inode += 1
			_writeNewcEntry(output, "TRAILER!!!", 0, inode=inode)
		os.replace(temporary, target)
	finally:
		shutil.rmtree(merged, ignore_errors=True)


def _installPack(packId, progress):
	if packId not in PACKS:
		raise ValueError("Unknown Samsung firmware pack.")
	pack = PACKS[packId]
	os.makedirs(dataRoot(), exist_ok=True)
	if shutil.disk_usage(dataRoot()).free < 6 * 1024 * 1024 * 1024:
		raise RuntimeError("At least 6 GB of free space is required while installing Samsung TV firmware.")
	work = tempfile.mkdtemp(prefix="firmware-", dir=dataRoot())
	download = os.path.join(dataRoot(), "%s-%s.zip" % (pack["family"], pack["version"]))
	installed = False
	try:
		cached = os.path.isfile(download) and _sha256(download) == pack["sha256"]
		if not cached:
			try:
				os.remove(download)
			except OSError:
				pass
			_download(pack["url"], download, progress)
			progress("verify", 0, 0)
			if _sha256(download) != pack["sha256"]:
				try:
					os.remove(download)
				except OSError:
					pass
				raise RuntimeError("The downloaded firmware did not match Samsung's verified package.")
		else:
			progress("verify", 0, 0)
		upgrade = os.path.join(work, "upgrade.msd")
		_findUpgradeMsd(download, upgrade)
		decrypted = os.path.join(work, "decrypted")
		os.makedirs(decrypted)
		progress("decrypt", 0, 0)
		result = subprocess.run(
			[UNIXTRACT_PATH, upgrade, decrypted], stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
			creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=1800,
		)
		if result.returncode:
			raise RuntimeError(result.stdout.decode("utf-8", "replace").strip() or "Samsung firmware decryption failed.")
		platform = _findPlatformImage(decrypted)
		tarPath = os.path.join(work, "speech.tar")
		progress("extract", 0, 0)
		_extractSpeechTar(platform, tarPath, progress)
		extracted = os.path.join(work, "extracted")
		os.makedirs(extracted)
		_safeTarExtract(tarPath, extracted)
		candidate = os.path.join(work, "pack")
		runtime = os.path.join(candidate, "runtime")
		os.makedirs(runtime)
		_normaliseRuntime(extracted, runtime)
		with open(os.path.join(candidate, "pack.json"), "w", encoding="utf-8", newline="\n") as stream:
			json.dump({"id": packId, "family": pack["family"], "version": pack["version"], "firmwareSha256": pack["sha256"]}, stream, indent=2, sort_keys=True)
			stream.write("\n")
		os.makedirs(packsRoot(), exist_ok=True)
		target = packPath(packId)
		old = target + ".old"
		shutil.rmtree(old, ignore_errors=True)
		if os.path.isdir(target):
			os.replace(target, old)
		os.replace(candidate, target)
		try:
			rebuildRuntime()
		except Exception:
			shutil.rmtree(target, ignore_errors=True)
			if os.path.isdir(old):
				os.replace(old, target)
			rebuildRuntime()
			raise
		shutil.rmtree(old, ignore_errors=True)
		installed = True
	finally:
		shutil.rmtree(work, ignore_errors=True)
		if installed:
			try:
				os.remove(download)
			except OSError:
				pass


def installPack(packId, progress):
	if not _installLock.acquire(blocking=False):
		raise RuntimeError("Another Samsung TV firmware installation is already in progress.")
	try:
		_cleanupStaleInstallerFiles()
		return _installPack(packId, progress)
	finally:
		_installLock.release()


def removePacks(packIds):
	for packId in packIds:
		if packId in PACKS:
			shutil.rmtree(packPath(packId), ignore_errors=True)
	rebuildRuntime()


def runtimeAvailable():
	return os.path.isfile(runtimePath()) and bool(availableVoiceDefinitions())
