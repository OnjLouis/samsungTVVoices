# Samsung TV Voices for NVDA

Samsung TV Voices makes compatible speech voices from official Samsung television firmware available through NVDA. The add-on contains no Samsung firmware, speech engine, model or voice data. Users choose an official regional firmware package, which is downloaded directly from Samsung and verified before its speech components are installed.

The accessible manager supports background and multiple-package downloads, removal, live voice refresh, resumable transfers, automatic recovery from a disconnected extraction helper, and a stable status list. NVDA exposes voice, rate, pitch, volume, head size, interruption, spelling and Say All support.

## Repository layout

- `addon` contains the NVDA add-on and its bundled runtime dependencies.
- `extractor` contains the corresponding unixtract, msddecrypt and modified VDFS extraction source.
- `guest` contains the small guest service and initramfs build inputs used by the isolated ARM helper.

The QEMU binary is an unmodified xPack QEMU Arm distribution; its notices and licences are included with the add-on. The project-specific extractor and guest sources are retained here so the shipped helper components can be maintained.

## Extractor source

The VDFS extractor uses the coherent `vdfs4-tools.0010` source from [upstream commit b09b74e](https://github.com/HinTak/vdfs-tools/tree/b09b74eb5fed5e348d4211a0570bec810d028874), not the incompatible later VDFS headers. The local `samsung_tts_path_is_wanted` filter retains only the speech tree and engine libraries, matching the shipped guest executable.

Build a disposable copy of `extractor/vdfs-tools` on an ARM32 GNU/Linux system with GCC, Make and the normal development libraries:

```sh
CFLAGS='-fcommon -include sys/sysmacros.h' make -j2 unpack
```

The compatibility flags allow the legacy headers to compile with modern GCC and glibc. The Makefile separately configures the bundled libraries and gives the old LZO conformance checks defined signed-overflow behaviour. Compiler output is not part of the NVDA add-on. The guest build script takes a staging directory argument instead of relying on a machine-specific location.

## Legal notice

This independent project is not affiliated with or endorsed by Samsung. Firmware is downloaded only after the user requests it. Samsung owns the optional firmware and speech components, and users remain responsible for applicable terms and law.

See the [add-on manual](addon/doc/en/readme.html) for installation, supported firmware, credits and the complete changelog.
