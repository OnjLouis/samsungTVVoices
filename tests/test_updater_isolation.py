"""Keep TV's updater isolated from other NVDA global plugins."""

import ast
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class UpdaterIsolationTests(unittest.TestCase):
	def test_cached_foreign_updater_cannot_supply_the_tv_updater(self):
		pluginDir = ROOT / "addon/globalPlugins"
		tree = ast.parse((pluginDir / "samsungTVVoices.py").read_text(encoding="utf-8"))
		statement = next(node for node in tree.body if isinstance(node, ast.ImportFrom)
			and any(alias.name == "SignedWebUpdater" for alias in node.names))
		foreignClass = type("ForeignUpdater", (), {})
		foreignModule = types.ModuleType("globalPlugins._signedWebUpdater")
		foreignModule.SignedWebUpdater = foreignClass
		package = types.ModuleType("globalPlugins")
		package.__path__ = [str(pluginDir)]
		modules = {
			"globalPlugins": package, "globalPlugins._signedWebUpdater": foreignModule,
			"addonHandler": types.SimpleNamespace(getAvailableAddons=lambda:
				[types.SimpleNamespace(name="samsungTVVoices", manifest={"version": "1.2.3"})]),
			"core": types.ModuleType("core"), "gui": types.ModuleType("gui"),
			"synthDriverHandler": types.ModuleType("synthDriverHandler"),
			"logHandler": types.SimpleNamespace(log=mock.Mock()),
			"systemUtils": types.SimpleNamespace(ExecAndPump=mock.Mock()),
			"wx": types.ModuleType("wx"),
			"synthDrivers._samsungTVVoices": types.SimpleNamespace(firmwareStore=
				types.SimpleNamespace(loadSettings=lambda: {"updateInterval": "hourly"})),
		}
		with mock.patch.dict(sys.modules, modules):
			namespace = {"__package__": "globalPlugins"}
			exec(compile(ast.Module(body=[statement], type_ignores=[]), "plugin-import", "exec"), namespace)
			cls = namespace["SignedWebUpdater"]
			self.assertIsNot(foreignClass, cls)
			module = importlib.import_module(cls.__module__)
			self.assertEqual("https://github.com/OnjLouis/samsungTVVoices/releases/latest/download/samsungTVVoices-update.json", module.MANIFEST_URL)
			self.assertEqual("1.2.3", module._currentVersion())
			self.assertEqual("hourly", cls()._interval)


if __name__ == "__main__":
	unittest.main()
