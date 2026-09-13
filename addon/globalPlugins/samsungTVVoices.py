# license: GPL-2.0-or-later
"""Firmware and update manager for Samsung TV Voices."""

import builtins
import os
import threading
import time
import weakref

import addonHandler
import globalPluginHandler
import gui
from logHandler import log
import synthDriverHandler
from synthDrivers._samsungTVVoices import firmwareStore
from ._signedWebUpdater import SignedWebUpdater
import ui
import wx

_ = getattr(builtins, "_", lambda text: text)
_activeUpdater = None
_downloadLock = threading.RLock()
_downloadState = {
	"busy": False,
	"percent": 0,
	"status": "",
	"details": {},
	"selected": (),
	"activePackId": None,
	"completedSerial": 0,
}


def _downloadSnapshot():
	with _downloadLock:
		return dict(_downloadState)


def _cleanupInstallerCache():
	try:
		removed = firmwareStore.cleanupStaleInstallerFiles()
		if removed:
			log.info("Samsung TV Voices: removed %d expired installer artifact(s)", removed)
	except Exception:
		log.error("Samsung TV Voices could not clean its expired installer files", exc_info=True)


def _callPanel(panelRef, methodName, *args):
	panel = panelRef()
	if panel is None:
		return
	try:
		if panel.IsBeingDeleted():
			return
		getattr(panel, methodName)(*args)
	except RuntimeError:
		# wx can destroy the native control between resolving the weak reference
		# and dispatching the callback.
		return


def _setDownloadProgress(percent, status, activePackId=None, details=None):
	with _downloadLock:
		_downloadState["percent"] = max(0, min(100, int(percent)))
		_downloadState["status"] = status
		_downloadState["activePackId"] = activePackId
		if details is not None:
			_downloadState["details"] = dict(details)


def _refreshActiveSynth():
	try:
		from synthDrivers import samsungTVVoices as samsungSynth
		synth = synthDriverHandler.getSynth()
		if synth.name == "samsungTVVoices":
			synth.refreshFirmwareRuntime()
		else:
			samsungSynth.reloadInstalledVoices()
	except Exception:
		log.error("Could not refresh Samsung TV voices", exc_info=True)


def _startFirmwareDownload(packIds):
	packIds = tuple(packIds)
	with _downloadLock:
		if _downloadState["busy"]:
			return False
		_downloadState.update(
			busy=True,
			percent=0,
			status=_("Preparing {count} selected firmware pack(s)...").format(count=len(packIds)),
			details={"stage": _("Preparing")},
			selected=packIds,
		)

	transfer = {"packId": None, "startedAt": 0.0, "startedBytes": 0}

	def progress(position, packId, phase, received, total):
		packName = _(firmwareStore.PACKS[packId]["name"])
		phaseName = {
			"download": _("Downloading"),
			"verify": _("Verifying"),
			"decrypt": _("Decrypting"),
			"extract": _("Extracting speech files"),
		}.get(phase, phase)
		percent = min(100, round(received * 100 / total)) if total else 0
		overall = round(((position - 1) + percent / 100) * 100 / len(packIds))
		if total:
			speedText = ""
			if phase == "download":
				if transfer["packId"] != packId:
					transfer.update(packId=packId, startedAt=time.monotonic(), startedBytes=received)
				elapsed = time.monotonic() - transfer["startedAt"]
				transferred = max(0, received - transfer["startedBytes"])
				if elapsed > 0 and transferred > 0:
					speedText = _(", {speed:.1f} MB/s").format(
						speed=transferred / elapsed / (1024 * 1024),
					)
			status = _("{phase} {pack}, {position} of {count}: {percent}% ({received:.1f} of {total:.1f} MB){speed}").format(
				phase=phaseName, pack=packName, position=position, count=len(packIds), percent=percent,
				received=received / (1024 * 1024), total=total / (1024 * 1024),
				speed=speedText,
			)
		else:
			status = _("{phase} {pack}, {position} of {count}...").format(
				phase=phaseName, pack=packName, position=position, count=len(packIds),
			)
		details = {
			"pack": packName,
			"stage": phaseName,
			"item": _("{position} of {count}").format(position=position, count=len(packIds)),
			"progress": _("{percent}%").format(percent=percent),
		}
		if total:
			details["downloaded"] = _("{received:.1f} of {total:.1f} MB").format(
				received=received / (1024 * 1024), total=total / (1024 * 1024),
			)
			if speedText:
				details["speed"] = speedText.lstrip(", ")
		_setDownloadProgress(overall, status, packId, details)

	def worker():
		errors = []
		installed = []
		for position, packId in enumerate(packIds, start=1):
			transfer["packId"] = None
			_setDownloadProgress(
				round((position - 1) * 100 / len(packIds)),
				_("Starting {pack}, {position} of {count}...").format(
					pack=_(firmwareStore.PACKS[packId]["name"]), position=position, count=len(packIds),
				),
				packId,
				{
					"pack": _(firmwareStore.PACKS[packId]["name"]),
					"stage": _("Starting"),
					"item": _("{position} of {count}").format(position=position, count=len(packIds)),
				},
			)
			try:
				firmwareStore.installPack(
					packId,
					lambda phase, received, total, p=position, i=packId: progress(
						p, i, phase, received, total,
					),
				)
			except Exception as error:
				log.error("Samsung TV firmware installation failed for %s", packId, exc_info=True)
				errors.append((packId, str(error)))
			else:
				installed.append(packId)
		if installed:
			wx.CallAfter(_refreshActiveSynth)
		if errors:
			details = "\n".join(
				_("{pack}: {error}").format(pack=_(firmwareStore.PACKS[packId]["name"]), error=error)
				for packId, error in errors
			)
			message = _("Installed {installed} firmware pack(s); {failed} failed.").format(
				installed=len(installed), failed=len(errors),
			)
			status = message + "\n" + details
		else:
			message = _("Installed {count} firmware pack(s). The voices are now available.").format(
				count=len(installed),
			)
			status = message
		with _downloadLock:
			_downloadState.update(
				busy=False,
				percent=100 if not errors else _downloadState["percent"],
				status=status,
				details={"result": status},
				activePackId=None,
				completedSerial=_downloadState["completedSerial"] + 1,
			)
		wx.CallAfter(ui.message, message)

	threading.Thread(target=worker, name="Samsung TV firmware installation", daemon=True).start()
	return True


class SamsungTVVoicesPanel(gui.settingsDialogs.SettingsPanel):
	title = _("Samsung TV Voices")

	def makeSettings(self, settingsSizer):
		self._busy = _downloadSnapshot()["busy"]
		self._settings = firmwareStore.loadSettings()
		helper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		helper.addItem(wx.StaticText(
			self,
			label=_("No Samsung voice files are included. Select one or both official firmware packs; they will be downloaded from Samsung and prepared on this computer."),
		))
		firmwareLabel = helper.addItem(wx.StaticText(self, label=_("Available &firmware packs:")))
		self.packList = helper.addItem(wx.ListCtrl(self, style=wx.LC_REPORT | wx.BORDER_SUNKEN))
		firmwareLabel.SetName(_("Available firmware packs"))
		self.packList.SetName(_("Available firmware packs"))
		self.packList.InsertColumn(0, _("Firmware pack"), width=300)
		self.packList.InsertColumn(1, _("Version"), width=90)
		self.packList.InsertColumn(2, _("Status"), width=100)
		self.packList.InsertColumn(3, _("Download size"), width=120)
		buttons = wx.BoxSizer(wx.HORIZONTAL)
		self.downloadButton = wx.Button(self, label=_("&Download or update"))
		self.removeButton = wx.Button(self, label=_("&Remove"))
		buttons.Add(self.downloadButton, 0, wx.RIGHT, 8)
		buttons.Add(self.removeButton)
		helper.addItem(buttons)
		self.progress = helper.addItem(wx.Gauge(self, range=100))
		self.progress.SetName(_("Firmware installation progress"))
		statusLabel = helper.addItem(wx.StaticText(self, label=_("Download status:")))
		self.statusList = helper.addItem(wx.ListCtrl(self, style=wx.LC_REPORT | wx.BORDER_SUNKEN, size=(-1, 135)))
		statusLabel.SetName(_("Download status"))
		self.statusList.SetName(_("Download status"))
		self.statusList.InsertColumn(0, _("Detail"), width=130)
		self.statusList.InsertColumn(1, _("Value"), width=420)
		self._statusRows = {}
		for key, label in (
			("pack", _("Pack")),
			("stage", _("Stage")),
			("item", _("Item")),
			("progress", _("Progress")),
			("downloaded", _("Downloaded")),
			("speed", _("Speed")),
			("result", _("Result")),
		):
			row = self.statusList.InsertItem(self.statusList.GetItemCount(), label)
			self.statusList.SetItem(row, 1, "")
			self._statusRows[key] = row
		voicesLabel = helper.addItem(wx.StaticText(self, label=_("Installed &voices:")))
		self.voiceList = helper.addItem(wx.ListBox(self))
		voicesLabel.SetName(_("Installed voices"))
		self.voiceList.SetName(_("Installed voices"))
		updateSizer = wx.BoxSizer(wx.HORIZONTAL)
		updateLabel = wx.StaticText(self, label=_("Add-on &update checks:"))
		self.updateInterval = wx.Choice(self, choices=(_("Never"), _("Hourly"), _("Daily")))
		self.updateInterval.SetName(_("Add-on update checks"))
		intervals = ("never", "hourly", "daily")
		self.updateInterval.SetSelection(intervals.index(self._settings["updateInterval"]))
		self.checkNowButton = wx.Button(self, label=_("Check &now"))
		updateSizer.Add(updateLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
		updateSizer.Add(self.updateInterval, 1, wx.RIGHT, 8)
		updateSizer.Add(self.checkNowButton)
		helper.addItem(updateSizer)
		self.helpButton = helper.addItem(wx.Button(self, label=_("&Help")))
		self.downloadButton.Bind(wx.EVT_BUTTON, self.onDownload)
		self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)
		self.checkNowButton.Bind(wx.EVT_BUTTON, self.onCheckNow)
		self.helpButton.Bind(wx.EVT_BUTTON, lambda event: self._openManual())
		self.packList.Bind(wx.EVT_LIST_ITEM_SELECTED, self.onSelectionChanged)
		self.packList.Bind(wx.EVT_LIST_ITEM_DESELECTED, self.onSelectionChanged)
		self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)
		self.Bind(wx.EVT_WINDOW_DESTROY, self.onDestroy)
		self._refresh()
		snapshot = _downloadSnapshot()
		self._seenCompletedSerial = snapshot["completedSerial"]
		self._downloadTimer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._syncDownloadState, self._downloadTimer)
		self._syncDownloadState()
		self._downloadTimer.Start(250)

	def _refresh(self, selected=None):
		selected = set(selected or ())
		self.packList.DeleteAllItems()
		self._packIds = list(firmwareStore.PACKS)
		for position, packId in enumerate(self._packIds):
			pack = firmwareStore.PACKS[packId]
			row = self.packList.InsertItem(position, _(pack["name"]))
			self.packList.SetItem(row, 1, pack["version"])
			self.packList.SetItem(row, 2, _("Installed") if firmwareStore.isPackInstalled(packId) else _("Available"))
			self.packList.SetItem(row, 3, pack["sizeLabel"])
			self.packList.SetItemData(row, position)
			if packId in selected:
				self.packList.Select(row)
		if not selected and self._packIds:
			self.packList.Select(0)
			self.packList.Focus(0)
		definitions = firmwareStore.availableVoiceDefinitions()
		self.voiceList.Set([details["name"] for details in definitions.values()])
		if definitions:
			self.voiceList.SetSelection(0)
		self._updateButtons()

	def _selectedPackIds(self):
		selected = []
		row = self.packList.GetFirstSelected()
		while row >= 0:
			selected.append(self._packIds[self.packList.GetItemData(row)])
			row = self.packList.GetNextItem(row, wx.LIST_NEXT_ALL, wx.LIST_STATE_SELECTED)
		return selected

	def _updateButtons(self):
		selected = self._selectedPackIds()
		self.downloadButton.Enable(not self._busy and bool(selected))
		self.removeButton.Enable(not self._busy and any(firmwareStore.isPackInstalled(packId) for packId in selected))

	def onSelectionChanged(self, event):
		self._updateButtons()
		event.Skip()

	def onDownload(self, event):
		packIds = self._selectedPackIds()
		if self._busy or not packIds:
			return
		if not firmwareStore.installedPackIds():
			answer = gui.messageBox(
				_("The selected official TV firmware will be downloaded directly from Samsung. The add-on will keep only its speech files in your NVDA user-data folder and delete the large firmware package afterward. Samsung owns those files; this unofficial add-on is not affiliated with or endorsed by Samsung. Continue?"),
				_("Download Samsung TV firmware"),
				wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION,
			)
			if answer != wx.YES:
				return
		if _startFirmwareDownload(packIds):
			self._syncDownloadState()

	def _syncDownloadState(self, event=None):
		snapshot = _downloadSnapshot()
		self._busy = snapshot["busy"]
		self.progress.SetValue(snapshot["percent"])
		for key, row in self._statusRows.items():
			value = str(snapshot["details"].get(key, ""))
			if self.statusList.GetItemText(row, 1) != value:
				self.statusList.SetItem(row, 1, value)
		for row, packId in enumerate(self._packIds):
			if snapshot["busy"] and packId == snapshot["activePackId"]:
				rowStatus = _("Downloading")
			else:
				rowStatus = _("Installed") if firmwareStore.isPackInstalled(packId) else _("Available")
			if self.packList.GetItemText(row, 2) != rowStatus:
				self.packList.SetItem(row, 2, rowStatus)
		if snapshot["completedSerial"] != self._seenCompletedSerial:
			self._seenCompletedSerial = snapshot["completedSerial"]
			self._refresh(snapshot["selected"])
		else:
			self._updateButtons()

	def onRemove(self, event):
		packIds = [packId for packId in self._selectedPackIds() if firmwareStore.isPackInstalled(packId)]
		if not packIds:
			return
		remaining = set(firmwareStore.installedPackIds()) - set(packIds)
		try:
			active = synthDriverHandler.getSynth().name == "samsungTVVoices"
		except Exception:
			active = False
		if active and not remaining:
			ui.message(_("Switch to another synthesizer before removing the last Samsung TV firmware pack."))
			return
		answer = gui.messageBox(
			_("Remove {count} selected firmware pack(s) and their installed voices? They can be downloaded again later.").format(count=len(packIds)),
			_("Remove Samsung TV firmware"), wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
		)
		if answer != wx.YES:
			return
		self._busy = True
		self._updateButtons()
		panelRef = weakref.ref(self)

		def worker():
			error = None
			try:
				firmwareStore.removePacks(packIds)
				wx.CallAfter(_refreshActiveSynth)
			except Exception as caught:
				error = str(caught)
				log.error("Could not remove Samsung TV firmware", exc_info=True)
			wx.CallAfter(_callPanel, panelRef, "_removeFinished", packIds, error)

		threading.Thread(target=worker, name="Samsung TV firmware removal", daemon=True).start()

	def _removeFinished(self, packIds, error):
		if self.IsBeingDeleted():
			return
		self._busy = False
		message = _("The selected firmware could not be removed: {error}").format(error=error) if error else _("Removed {count} firmware pack(s).").format(count=len(packIds))
		self.statusList.SetItem(self._statusRows["result"], 1, message)
		ui.message(message)
		self._refresh(packIds)

	def onCheckNow(self, event):
		if _activeUpdater is None:
			ui.message(_("The add-on updater is not available."))
			return
		_activeUpdater.checkNow(True)

	def onCharHook(self, event):
		if event.GetKeyCode() == wx.WXK_F1:
			self._openManual()
			return
		event.Skip()

	def onDestroy(self, event):
		if event.GetEventObject() is self and hasattr(self, "_downloadTimer"):
			self._downloadTimer.Stop()
		event.Skip()

	def _openManual(self):
		try:
			manual = os.path.join(addonHandler.getCodeAddon().path, "doc", "en", "readme.html")
			if os.path.isfile(manual):
				os.startfile(manual)
				return
		except Exception:
			log.error("Could not open the Samsung TV Voices manual", exc_info=True)
		ui.message(_("The Samsung TV Voices manual is not available."))

	def onSave(self):
		intervals = ("never", "hourly", "daily")
		self._settings["updateInterval"] = intervals[self.updateInterval.GetSelection()]
		firmwareStore.saveSettings(self._settings)
		if _activeUpdater is not None:
			_activeUpdater.setInterval(self._settings["updateInterval"])


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		global _activeUpdater
		super().__init__()
		if SamsungTVVoicesPanel not in gui.settingsDialogs.NVDASettingsDialog.categoryClasses:
			gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(SamsungTVVoicesPanel)
		self._updater = SignedWebUpdater()
		_activeUpdater = self._updater
		self._updater.start()
		threading.Thread(
			target=_cleanupInstallerCache,
			name="Samsung TV installer cache cleanup",
			daemon=True,
		).start()
		settings = firmwareStore.loadSettings()
		if not settings["firmwarePromptShown"] and not firmwareStore.installedPackIds():
			settings["firmwarePromptShown"] = True
			firmwareStore.saveSettings(settings)
			self._firstRunTimer = wx.CallLater(1500, self._promptForFirmware)
		else:
			self._firstRunTimer = None

	def _promptForFirmware(self):
		answer = gui.messageBox(
			_("Samsung TV Voices does not include Samsung firmware or voice files. Open its settings panel now to choose an official firmware pack to download from Samsung?"),
			_("Choose Samsung TV firmware"), wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION,
		)
		if answer == wx.YES:
			gui.mainFrame._popupSettingsDialog(gui.settingsDialogs.NVDASettingsDialog, SamsungTVVoicesPanel)

	def terminate(self):
		global _activeUpdater
		if self._firstRunTimer is not None:
			self._firstRunTimer.Stop()
		self._updater.stop()
		_activeUpdater = None
		try:
			gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(SamsungTVVoicesPanel)
		except ValueError:
			pass
		super().terminate()
