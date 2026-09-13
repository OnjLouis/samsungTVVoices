import importlib.util
import os
import sys
import tempfile
import time
import types
from pathlib import Path


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
	test_header_reset_has_actionable_error()
	test_stream_reset_requires_complete_tar_end()
	test_extractor_retries_one_early_disconnect()
	test_stale_installer_files_expire_without_touching_fresh_or_installed_data()
	print("Samsung TV firmware-store tests passed.")
