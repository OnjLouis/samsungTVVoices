# license: GPL-2.0-or-later
"""Prepare an existing Samsung TV Voices add-on for replacement."""

import addonHandler


ADDON_NAME = "samsungTVVoices"


def onInstall():
	for addon in addonHandler.getAvailableAddons():
		if addon.name != ADDON_NAME or getattr(addon, "isPendingInstall", False):
			continue
		addon.requestRemove()
		break
