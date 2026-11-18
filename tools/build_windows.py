"""Build the llmirror Windows executable from this project directory."""
from pathlib import Path
import platform
import struct
import sys


def main():
    if platform.system() != 'Windows' or struct.calcsize('P') != 8:
        raise SystemExit('Build llmirror.exe on 64-bit Windows with Python 3.12.')
    try:
        import PyInstaller.__main__
    except ImportError:
        raise SystemExit('Install build dependencies: python -m pip install -r tools/requirements-build.txt')
    project = Path(__file__).resolve().parents[1]
    PyInstaller.__main__.run([
        '--noconfirm', '--onefile', '--name', 'llmirror',
        '--distpath', str(project/'dist'), '--workpath', str(project/'build'),
        '--specpath', str(project), '--paths', str(project),
        '--add-data', str(project/'static')+';static',
        '--icon', str(project/'icon.ico'),
        '--version-file', str(project/'windows-version.txt'),
        str(project/'launcher.py'),
    ])


if __name__ == '__main__':
    main()
