# Samsung TV Voices for NVDA

Samsung TV Voices makes compatible speech voices from official Samsung television firmware available through NVDA. The add-on contains no Samsung firmware, speech engine, model or voice data. Users choose an official regional firmware package, which is downloaded directly from Samsung and verified before its speech components are installed.

The accessible manager supports background and multiple-package downloads, removal, live voice refresh, resumable transfers, and a stable status list. NVDA exposes voice, rate, pitch, volume, head size, interruption, spelling and Say All support.

## Repository layout

- `addon` contains the NVDA add-on and its bundled runtime dependencies.
- `extractor` contains the corresponding unixtract, msddecrypt and modified VDFS extraction source.
- `guest` contains the small guest service and initramfs build inputs used by the isolated ARM helper.

The QEMU binary is an unmodified xPack QEMU Arm distribution; its notices and licences are included with the add-on. The project-specific extractor and guest sources are retained here so the shipped helper components can be maintained.

## Legal notice

This independent project is not affiliated with or endorsed by Samsung. Firmware is downloaded only after the user requests it. Samsung owns the optional firmware and speech components, and users remain responsible for applicable terms and law.

See the [add-on manual](addon/doc/en/readme.html) for installation, supported firmware, credits and the complete changelog.

