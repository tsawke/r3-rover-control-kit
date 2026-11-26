"""Build a portable Windows/Pi release from an explicit file list."""

from hashlib import sha256
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from control_kit import PI_FILES, ROOT, WHEEL, WHEEL_SHA256


DIST = ROOT / "dist" / "R3-Control-Kit.zip"
PREFIX = "R3-Control-Kit"
FILES = (
    *PI_FILES, "r3_ui.py", "control_kit.py", "README.md",
    "Run-Kit.cmd", "Prepare-Key.cmd", "Setup-Pi.cmd", "Start-Control.cmd", "Check-Pi.cmd",
    "ui/index.html", "ui/app.js", "ui/style.css", "ui/icons.svg",
)
WHEEL_URL = ("https://files.pythonhosted.org/packages/07/bc/"
             "587a445451b253b285629263eb51c2d8e9bcea4fc97826266d186f96f558/" + WHEEL)
LICENSE_URL = "https://raw.githubusercontent.com/pyserial/pyserial/v3.5/LICENSE.txt"


def fetch(url):
    with urlopen(url, timeout=20) as response:
        return response.read()


def main():
    wheel = fetch(WHEEL_URL)
    if sha256(wheel).hexdigest() != WHEEL_SHA256:
        raise ValueError("pySerial download did not pass the published PyPI checksum.")
    license_text = fetch(LICENSE_URL)
    DIST.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(DIST, "w") as output:
        def add(name, data):
            output.writestr(ZipInfo(f"{PREFIX}/{name}"), data, compress_type=ZIP_DEFLATED)

        for name in FILES:
            add(name, (ROOT / name).read_bytes().replace(b"\r\n", b"\n"))
        add(f"deps/{WHEEL}", wheel)
        add("deps/pyserial-LICENSE.txt", license_text)
    print(DIST)
    print("SHA-256:", sha256(DIST.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
