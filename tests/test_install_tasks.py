import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "addon" / "installTasks.py"


class InstallTaskTests(unittest.TestCase):
	def test_marks_only_existing_addon_for_removal(self):
		existing = types.SimpleNamespace(
			name="samsungTVVoices",
			isPendingInstall=False,
			requestRemove=mock.Mock(),
		)
		pending = types.SimpleNamespace(
			name="samsungTVVoices",
			isPendingInstall=True,
			requestRemove=mock.Mock(),
		)
		addon_handler = types.ModuleType("addonHandler")
		addon_handler.getAvailableAddons = lambda: [existing, pending]
		sys.modules["addonHandler"] = addon_handler
		spec = importlib.util.spec_from_file_location("tvInstallTasksUnderTest", MODULE)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)

		module.onInstall()

		existing.requestRemove.assert_called_once_with()
		pending.requestRemove.assert_not_called()


if __name__ == "__main__":
	unittest.main()
