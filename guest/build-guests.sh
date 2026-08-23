#!/bin/sh
set -eu
base=/mnt/archive/codex-temp/samsung-tv-public
rm -rf "$base/clean-root" "$base/extractor-root"
cp -a "$base/private-root" "$base/clean-root"
rm -rf "$base/clean-root/opt/smt/runtime"
cp "$base/clean-init" "$base/clean-root/init"
chmod 755 "$base/clean-root/init"

cp -a "$base/clean-root" "$base/extractor-root"
rm -f "$base/extractor-root/opt/smt/samsung_guest"
cp "$base/vdfs-tools/unpack.vdfs" "$base/extractor-root/bin/unpack.vdfs"
chmod 755 "$base/extractor-root/bin/unpack.vdfs"
mkdir -p "$base/extractor-root/lib/modules/6.1.0-50-armmp/kernel/drivers/block"
cp "$base/linux-image/lib/modules/6.1.0-50-armmp/kernel/drivers/block/virtio_blk.ko" "$base/extractor-root/lib/modules/6.1.0-50-armmp/kernel/drivers/block/"
cp /lib/ld-linux-armhf.so.3 "$base/extractor-root/lib/"
mkdir -p "$base/extractor-root/lib/arm-linux-gnueabihf"
cp /lib/arm-linux-gnueabihf/libc.so.6 "$base/extractor-root/lib/arm-linux-gnueabihf/"
for app in cat mkdir stat tar; do
	ln -sf busybox "$base/extractor-root/bin/$app"
done
cp "$base/extract-init" "$base/extractor-root/init"
chmod 755 "$base/extractor-root/init"

cd "$base/clean-root"
find . -print0 | cpio --null -o --format=newc > "$base/samsung-clean-initramfs.cpio" 2>/tmp/samsung-clean-cpio.log
cd "$base/extractor-root"
find . -print0 | cpio --null -o --format=newc > "$base/samsung-extractor-initramfs.cpio" 2>/tmp/samsung-extractor-cpio.log
ls -lh "$base"/samsung-*-initramfs.cpio
