"""PlatformIO post-build step: turn firmware.hex into a drag-and-drop UF2.

The XIAO nRF52840 bootloader (Adafruit nRF52 bootloader) mounts a USB drive
after a double-tap on RESET; copying a .uf2 onto it flashes the application.
Outputs, next to firmware.hex:
    kman-fw-<version>-<env>.uf2   drag-and-drop image (family 0xADA52840)
    kman-fw-<version>-<env>.hex   same image for SWD programmers (J-Link, DAPLink)
"""

import re
import shutil
import subprocess
from pathlib import Path

Import("env")  # noqa: F821  (provided by PlatformIO/SCons)

NRF52840_FAMILY = "0xADA52840"


def fw_version() -> str:
    cfg = Path(env.subst("$PROJECT_INCLUDE_DIR")) / "config.h"  # noqa: F821
    m = re.search(r'#define\s+KMAN_FW_VERSION\s+"([^"]+)"', cfg.read_text())
    return m.group(1) if m else "unknown"


def make_uf2(source, target, env):
    hex_path = Path(target[0].get_abspath())
    framework = env.PioPlatform().get_package_dir("framework-arduinoadafruitnrf52-seeed")
    uf2conv = Path(framework) / "tools" / "uf2conv" / "uf2conv.py"
    stem = f"kman-fw-{fw_version()}-{env.subst('$PIOENV')}"
    uf2 = hex_path.with_name(stem + ".uf2")
    subprocess.check_call([env.subst("$PYTHONEXE"), str(uf2conv), str(hex_path), "-c",
                           "-f", NRF52840_FAMILY, "-o", str(uf2)])
    shutil.copyfile(hex_path, hex_path.with_name(stem + ".hex"))
    print(f"UF2 image: {uf2}")


env.AddPostAction("$BUILD_DIR/${PROGNAME}.hex", make_uf2)  # noqa: F821
