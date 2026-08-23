import importlib.util
import os
import sys
import tempfile
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


if __name__ == "__main__":
	test_install_lock()
	test_blank_firmware_is_rejected()
	print("Samsung TV firmware-store tests passed.")

