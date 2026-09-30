import importlib.util
import io
import os
import sys
import tarfile
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tests" / ".test-config"
sys.modules["globalVars"] = types.SimpleNamespace(
	appArgs=types.SimpleNamespace(configPath=str(CONFIG)),
)
MODULE = ROOT / "addon" / "synthDrivers" / "_samsungTVVoices" / "firmwareStore.py"
spec = importlib.util.spec_from_file_location("firmwareStore", MODULE)
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


class ResetConnection:
	def __init__(self, chunks=()):
		self.chunks = list(chunks)

	def recv(self, size):
		if self.chunks:
			return self.chunks.pop(0)
		raise ConnectionResetError(10054, "connection reset")

	def settimeout(self, timeout):
		pass

	def close(self):
		pass


class FailedExtractor:
	def __init__(self, arguments, **kwargs):
		self.polls = 0
		self.arguments = arguments
		output = b"startup noise\n" * 1000 + b"fatal guest diagnostic\n"
		self.stdout = io.BytesIO(output)
		if kwargs["stderr"] != store.subprocess.STDOUT:
			kwargs["stderr"].write(output)

	def poll(self):
		self.polls += 1
		return None if self.polls == 1 else 0xC000001D


def test_extractor_failure_keeps_exit_code_and_diagnostic_tail():
	with patch.object(store.subprocess, "Popen", FailedExtractor), patch.object(
		store.socket, "create_connection", return_value=ResetConnection(),
	):
		try:
			store._extractSpeechTarOnce("platform.img", "speech.tar", lambda *args: None)
		except store._ExtractorDisconnected as error:
			message = str(error)
			assert "0xC000001D" in message, message
			assert "fatal guest diagnostic" in message, message
			assert "awaiting guest status" in message, message
			assert len(message) < 10000, len(message)
		else:
			raise AssertionError("An extractor crash was accepted.")


def test_extractor_output_is_bounded():
	output = [b""]
	stream = io.BytesIO(b"x" * 100000 + b"important final diagnostic")
	store._collectExtractorOutput(stream, output)
	assert len(output[0]) <= store._EXTRACTOR_DIAGNOSTIC_LIMIT
	assert output[0].endswith(b"important final diagnostic")
	assert stream.closed


def test_header_timeout_is_distinct_from_a_reset():
	connection = ResetConnection()
	with patch.object(connection, "recv", side_effect=store.socket.timeout("test timeout")):
		try:
			store._readLine(connection)
		except store._ExtractorDisconnected as error:
			assert "timed out" in str(error), error
		else:
			raise AssertionError("A missing extractor response was accepted.")


def test_install_lock():
	assert store._installLock.acquire(blocking=False)
	try:
		try:
			store.installPack("europe", lambda *args: None)
		except RuntimeError as error:
			assert "already in progress" in str(error), error
		else:
			raise AssertionError("A second firmware install was not rejected.")
	finally:
		store._installLock.release()


def test_blank_firmware_is_rejected():
	with tempfile.TemporaryDirectory() as work:
		disk = os.path.join(work, "dummy.img")
		with open(disk, "wb") as stream:
			stream.truncate(1024 * 1024)
		try:
			store._extractSpeechTar(disk, os.path.join(work, "speech.tar"), lambda *args: None)
		except RuntimeError as error:
			assert "firmware filesystem" in str(error).lower(), error
		else:
			raise AssertionError("The extractor unexpectedly accepted a blank disk.")


def _guestInitramfsForTest(command):
	data = Path(store.EXTRACTOR_INITRAMFS_PATH).read_bytes()
	position = 0
	while position + 110 <= len(data):
		header = bytearray(data[position:position + 110])
		assert header[:6] == b"070701", "The test expects a newc initramfs."
		size = int(header[54:62], 16)
		nameSize = int(header[94:102], 16)
		name = data[position + 110:position + 110 + nameSize - 1]
		start = (position + 110 + nameSize + 3) & ~3
		end = (start + size + 3) & ~3
		if name in (b"init", b"./init"):
			body = data[start:start + size]
			assert b"mkdir -p /tmp/extracted\n" in body
			body = body.replace(
				b"mkdir -p /tmp/extracted\n",
				command + b"\nmkdir -p /tmp/extracted\n",
				1,
			)
			header[54:62] = ("%08x" % len(body)).encode("ascii")
			modified = (
				data[:position] + header + data[position + 110:start] + body +
				b"\0" * ((-len(body)) % 4) + data[end:]
			)
			break
		position = end
	else:
		raise AssertionError("The extractor init script was not found.")
	return modified


def test_guest_shutdown_before_header_is_diagnosed():
	modified = _guestInitramfsForTest(b"echo 'Isolated guest shutdown diagnostic' >&2\npoweroff -f")
	with tempfile.TemporaryDirectory() as work:
		initramfs = Path(work) / "shutdown.cpio"
		initramfs.write_bytes(modified)
		disk = Path(work) / "blank.img"
		with disk.open("wb") as stream:
			stream.truncate(1024 * 1024)
		with patch.object(store, "EXTRACTOR_INITRAMFS_PATH", str(initramfs)), patch.object(
			store, "_EXTRACTOR_RESULT_TIMEOUT_SECONDS", 20,
		):
			try:
				store._extractSpeechTarOnce(str(disk), str(Path(work) / "speech.tar"), lambda *args: None)
			except store._ExtractorDisconnected as error:
				assert "Isolated guest shutdown diagnostic" in str(error), error
				assert "awaiting guest status" in str(error), error
			else:
				raise AssertionError("The prematurely stopped guest was accepted.")
	assert not any(
		thread.name == "Samsung TV extractor diagnostics" for thread in store.threading.enumerate()
	), "An extractor diagnostic reader survived process shutdown."


def test_guest_console_does_not_corrupt_successful_transfer():
	modified = _guestInitramfsForTest(b"""echo 'Successful transfer console diagnostic' >&2
mkdir -p /tmp/fixture
echo 'speech transfer fixture' > /tmp/fixture/speech.txt
printf 'SAMSUNG_TTS_TAR_STREAM\\n' > /dev/vport0p1
tar -cf - -C /tmp/fixture speech.txt > /dev/vport0p1
poweroff -f""")
	with tempfile.TemporaryDirectory() as work:
		initramfs = Path(work) / "transfer.cpio"
		initramfs.write_bytes(modified)
		disk = Path(work) / "blank.img"
		with disk.open("wb") as stream:
			stream.truncate(1024 * 1024)
		destination = Path(work) / "speech.tar"
		with patch.object(store, "EXTRACTOR_INITRAMFS_PATH", str(initramfs)), patch.object(
			store, "_EXTRACTOR_RESULT_TIMEOUT_SECONDS", 20,
		):
			store._extractSpeechTarOnce(str(disk), str(destination), lambda *args: None)
		with tarfile.open(destination) as archive:
			assert archive.getnames() == ["speech.txt"]
			with archive.extractfile("speech.txt") as stream:
				assert stream.read() == b"speech transfer fixture\n"
	assert not any(
		thread.name == "Samsung TV extractor diagnostics" for thread in store.threading.enumerate()
	), "A successful transfer left its diagnostic reader running."


def test_header_reset_has_actionable_error():
	try:
		store._readLine(ResetConnection())
	except store._ExtractorDisconnected as error:
		assert "before reporting its status" in str(error), error
	else:
		raise AssertionError("A reset before the extractor header was accepted.")


def test_stream_reset_requires_complete_tar_end():
	with tempfile.TemporaryDirectory() as work:
		incomplete = os.path.join(work, "incomplete.tar")
		try:
			store._receiveStream(ResetConnection((b"partial tar data",)), incomplete, 4096, lambda *args: None)
		except store._ExtractorDisconnected as error:
			assert "before completing" in str(error), error
		else:
			raise AssertionError("A truncated TAR stream was accepted after a reset.")

		complete = os.path.join(work, "complete.tar")
		store._receiveStream(ResetConnection((b"tar data" + (b"\0" * 1024),)), complete, 4096, lambda *args: None)
		assert os.path.getsize(complete) == len(b"tar data") + 1024


def test_extractor_retries_one_early_disconnect():
	original = store._extractSpeechTarOnce
	attempts = []
	def extractOnce(*args):
		attempts.append(args)
		if len(attempts) == 1:
			raise store._ExtractorDisconnected("first attempt disconnected")
		return "complete"
	store._extractSpeechTarOnce = extractOnce
	try:
		assert store._extractSpeechTar("platform.img", "speech.tar", lambda *args: None) == "complete"
	finally:
		store._extractSpeechTarOnce = original
	assert len(attempts) == 2


def test_stale_installer_files_expire_without_touching_fresh_or_installed_data():
	with tempfile.TemporaryDirectory() as work:
		originalConfigPath = store.globalVars.appArgs.configPath
		store.globalVars.appArgs.configPath = work
		try:
			root = store.dataRoot()
			os.makedirs(root)
			pack = store.PACKS["europe"]
			baseName = "%s-old.zip" % pack["family"]
			staleZip = os.path.join(root, baseName)
			stalePartial = staleZip + ".part"
			freshZip = os.path.join(root, "%s-current.zip" % pack["family"])
			staleWork = os.path.join(root, "firmware-abandoned")
			freshWork = os.path.join(root, "firmware-current")
			installed = os.path.join(store.packsRoot(), "europe")
			for path in (staleWork, freshWork, installed):
				os.makedirs(path)
			for path in (staleZip, stalePartial, freshZip):
				Path(path).write_bytes(b"firmware")
			now = time.time()
			staleTime = now - store._INSTALLER_RETENTION_SECONDS - 1
			for path in (staleZip, stalePartial, staleWork):
				os.utime(path, (staleTime, staleTime))
			removed = store._cleanupStaleInstallerFiles(now=now)
			assert removed == 3
			assert not os.path.exists(staleZip)
			assert not os.path.exists(stalePartial)
			assert not os.path.exists(staleWork)
			assert os.path.isfile(freshZip)
			assert os.path.isdir(freshWork)
			assert os.path.isdir(installed)
		finally:
			store.globalVars.appArgs.configPath = originalConfigPath


if __name__ == "__main__":
	test_install_lock()
	test_blank_firmware_is_rejected()
	test_guest_shutdown_before_header_is_diagnosed()
	test_guest_console_does_not_corrupt_successful_transfer()
	test_header_reset_has_actionable_error()
	test_extractor_failure_keeps_exit_code_and_diagnostic_tail()
	test_extractor_output_is_bounded()
	test_header_timeout_is_distinct_from_a_reset()
	test_stream_reset_requires_complete_tar_end()
	test_extractor_retries_one_early_disconnect()
	test_stale_installer_files_expire_without_touching_fresh_or_installed_data()
	print("Samsung TV firmware-store tests passed.")
