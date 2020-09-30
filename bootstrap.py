
from __future__ import print_function

import os
import re
import shutil
import sys
import platform
import tempfile
import traceback
import subprocess
import argparse

from glob import glob
from shutil import rmtree
from textwrap import dedent

try:
    from urllib.request import urlopen, Request, urlretrieve
except ImportError:
    from urllib2 import urlopen, Request
    from urllib import urlretrieve

ap = argparse.ArgumentParser()
ap.add_argument('--dir', help='the name of the directory to install to (default: MusicBot)')
args = ap.parse_args()


PY_VERSION = sys.version_info
SYS_PLATFORM = sys.platform
SYS_UNAME = platform.uname()
SYS_ARCH = ('32', '64')[SYS_UNAME[4].endswith('64')]
SYS_PKGMANAGER = None  # TODO: Figure this out

PLATFORMS = ['win32', 'linux', 'darwin', 'linux2']

MINIMUM_PY_VERSION = (3, 5)
TARGET_PY_VERSION = "3.5.2"

if SYS_PLATFORM not in PLATFORMS:
    raise RuntimeError('Unsupported system "%s"' % SYS_PLATFORM)

if SYS_PLATFORM == 'linux2':
    SYS_PLATFORM = 'linux'

TEMP_DIR = tempfile.TemporaryDirectory(prefix='musicbot-')
try:
    PY_BUILD_DIR = os.path.join(TEMP_DIR, "Python-%s" % TARGET_PY_VERSION)
except TypeError:
    PY_BUILD_DIR = os.path.join(TEMP_DIR.name, "Python-%s" % TARGET_PY_VERSION)

INSTALL_DIR = args.dir if args.dir is not None else 'MusicBot'

GET_PIP = "https://bootstrap.pypa.io/get-pip.py"

if PY_VERSION >= (3,):
    raw_input = input


def read_from_urllib(r):
    if PY_VERSION[0] == 2:
        return r.read()
    else:
        return r.read().decode("utf-8")


def sudo_check_output(args, **kwargs):
    if not isinstance(args, (list, tuple)):
        args = args.split()

    return subprocess.check_output(('sudo',) + tuple(args), **kwargs)


def sudo_check_call(args, **kwargs):
    if not isinstance(args, (list, tuple)):
        args = args.split()

    return subprocess.check_call(('sudo',) + tuple(args), **kwargs)

def tmpdownload(url, name=None, subdir=''):
    if name is None:
        name = os.path.basename(url)

    _name = os.path.join(TEMP_DIR.name, subdir, name)
    return urlretrieve(url, _name)

def find_library(libname):
    if SYS_PLATFORM == 'win32': return


def yes_no(question):
    while True:
        ri = raw_input('{} (y/n): '.format(question))
        if ri.lower() in ['yes', 'y']: return True
        elif ri.lower() in ['no', 'n']: return False



###############################################################################


class SetupTask(object):
    def __getattribute__(self, item):
        try:
            return object.__getattribute__(self, item + '_' + SYS_PLATFORM)
        except:
            pass

        if item.endswith('_dist'):
            try:
                return object.__getattribute__(self, item.rsplit('_', 1)[0] + '_' + SYS_PLATFORM)
            except:
                try:
                    return object.__getattribute__(self, item.rsplit('_', 1)[0])
                except:
                    pass

        return object.__getattribute__(self, item)

    @classmethod
    def run(cls):
        self = cls()
        if not self.check():
            self.setup(self.download())

    def check(self):
        pass

    def download(self):
        pass

    def setup(self, data):
        pass


###############################################################################


class EnsurePython(SetupTask):
    PYTHON_BASE = "https://www.python.org/ftp/python/{ver}/"
    PYTHON_TGZ = PYTHON_BASE + "Python-{ver}.tgz"
    PYTHON_EXE = PYTHON_BASE + "python-{ver}.exe"
    PYTHON_PKG = PYTHON_BASE + "python-{ver}-macosx10.6.pkg"

    def check(self):
        if PY_VERSION >= MINIMUM_PY_VERSION:
            return True


    def download_win32(self):
        exe, _ = tmpdownload(self.PYTHON_EXE.format(ver=TARGET_PY_VERSION))
        return exe

    def setup_win32(self, data):
        args = {
            'PrependPath': '1',
            'InstallLauncherAllUsers': '0',
            'Include_test': '0'
        }
        command = [data, '/quiet'] + ['='.join(x) for x in args.items()]

        subprocess.check_call(command)
        self._restart(None)

    def download_linux(self):
        tgz, _ = tmpdownload(self.PYTHON_TGZ.format(ver=TARGET_PY_VERSION))
        return tgz

    def setup_linux(self, data):
        if os.path.exists(PY_BUILD_DIR):
            try:
                shutil.rmtree(PY_BUILD_DIR)
            except OSError:
                sudo_check_call("rm -rf %s" % PY_BUILD_DIR)

        subprocess.check_output("tar -xf {} -C {}".format(data, TEMP_DIR.name).split())

        olddir = os.getcwd()
        os.chdir(PY_BUILD_DIR)

        subprocess.check_call('./configure --enable-ipv6 --enable-shared --with-system-ffi --without-ensurepip'.split())
        subprocess.check_call('make')
        sudo_check_call("make install")

        os.chdir(olddir)

        executable = "python{}".format(TARGET_PY_VERSION[0:3])

        self._restart(None)

        print("Rebooting into Python {}...".format(TARGET_PY_VERSION))
        os.execl("/usr/local/bin/{}".format(executable), "{}".format(executable), __file__)

    def download_darwin(self):
        pkg, _ = tmpdownload(self.PYTHON_PKG.format(ver=TARGET_PY_VERSION))
        return pkg

    def setup_darwin(self, data):
        subprocess.check_call(data.split())
        self._restart(None)

    def _restart(self, *cmds):
        pass


class EnsureEnv(SetupTask):
    pass

class EnsureBrew(SetupTask):
    def check(self):
        if SYS_PLATFORM == 'darwin':
            try:
                subprocess.check_output(['brew'])
            except FileNotFoundError:
                return False
            except subprocess.CalledProcessError:
                pass

        return True

    def download(self):
        cmd = '/usr/bin/ruby -e "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/master/install)"'
        subprocess.check_call(cmd, shell=True)

    def setup(self, data):
        subprocess.check_call('brew update'.split())


class EnsureGit(SetupTask):
    WIN_OPTS = dedent("""
        [Setup]
        Lang=default
        Group=Git
        NoIcons=0
        SetupType=default
        Components=ext,ext\shellhere,assoc
        Tasks=
        PathOption=Cmd
        SSHOption=OpenSSH
        CRLFOption=CRLFAlways
        BashTerminalOption=MinTTY
        PerformanceTweaksFSCache=Enabled
        """)

    def check(self):
        try:
            subprocess.check_output(['git', '--version'])
        except FileNotFoundError:
            return False

        return True

    @staticmethod
    def _get_latest_win_git_version():
        version = ('2.10.1', 'v2.10.1.windows.1')
        try:
            url = "https://github.com/git-for-windows/git/releases/latest"
            req = Request(url, method='HEAD')

            with urlopen(req) as resp:
                full_ver = os.path.basename(resp.url)

            match = re.match(r'v(\d+\.\d+\.\d+)', full_ver)
            return match.groups()[0], full_ver
        except:
            return version

    @classmethod
    def _get_latest_win_get_download(cls):
        dist_ver, full_ver = cls._get_latest_win_git_version()
        url = "https://github.com/git-for-windows/git/releases/download/{fullver}/Git-{ver}-{arch}-bit.exe"

        return url.format(full_ver=full_ver, ver=dist_ver, arch=SYS_ARCH)

    def download_win32(self):
        result, _ = tmpdownload(self._get_latest_win_git_version(), 'git-setup.exe')
        return result

    def setup_win32(self, data):
        with tempfile.NamedTemporaryFile('w+', encoding='utf8') as f:
            f.file.write(self.WIN_OPTS)
            f.file.flush()

            args = [
                data,
                '/SILENT',
                '/NORESTART',
                '/NOCANCEL',
                '/SP-',
                '/LOG',
                '/SUPPRESSMSGBOXES',
                '/LOADINF="%s"' % f.name
            ]
            subprocess.check_call(args)

    def download_linux(self):
        pass

    def download_darwin(self):
        subprocess.check_call('brew install git'.split())


class EnsureFFmpeg(SetupTask):
    AVCONV_CHECK = b"Please use avconv instead"

    def check_win32(self):
        return True

    def check(self):
        try:
            data = subprocess.check_output(['ffmpeg', '-version'], stderr=subprocess.STDOUT)
        except FileNotFoundError:
            return False
        else:
            return self.AVCONV_CHECK not in data

    def download_linux(self):
        pass

    def setup_linux(self, data):
        pass

    def download_darwin(self):
        subprocess.check_call('brew install ffmpeg'.split())


class EnsureOpus(SetupTask):

    def check_win32(self):
        return True

    def check(self):
        pass

    def download_linux(self):
        pass

    def setup_linux(self, data):
        pass

    def download_darwin(self):
        pass

    def setup_darwin(self, data):
        pass


class EnsureFFI(SetupTask):

    def check_win32(self):
        return True

    def check(self):
        pass

    def download_linux(self):
        pass

    def setup_linux(self, data):
        pass

    def download_darwin(self):
        pass

    def setup_darwin(self, data):
        pass



class EnsureSodium(SetupTask):
    def check_win32(self):
        return True


class EnsureCompiler(SetupTask):

    def check_win32(self):
        return True


class EnsurePip(SetupTask):
    def check(self):
        try:
            import pip
        except ImportError:
            return False
        else:
            return True

    def download(self):
        try:
            import ensurepip
            return False
        except ImportError:
            f = tempfile.NamedTemporaryFile(delete=False)
            print("Downloading pip...")
            urlretrieve(GET_PIP, f.name)
            return f.name

    def setup(self, data):
        if not data:
            print("Installing pip...")
            try:
                import ensurepip
                ensurepip.bootstrap()
            except PermissionError:
                # panic and try and sudo it
                sudo_check_call("python3.5 -m ensurepip")
            return

        print("Installing pip...")
        try:
            sudo_check_call(["python3.5", "{}".format(data)])
        except FileNotFoundError:
            subprocess.check_call(["python3.5", "{}".format(data)])


class GitCloneMusicbot(SetupTask):
    GIT_URL = "https://github.com/Just-Some-Bots/MusicBot.git"
    GIT_CMD = "git clone --depth 10 --no-single-branch %s %s" % (GIT_URL, INSTALL_DIR)

    def download(self):
        print("Cloning files using Git...")
        if os.path.isdir(INSTALL_DIR):
            r = yes_no('A folder called %s already exists here. Overwrite?' % INSTALL_DIR)
            if r is False:
                print('Exiting. Use the --dir parameter when running this script to specify a different folder.')
                sys.exit(1)
            else:
                os.rmdir(INSTALL_DIR)
        subprocess.check_call(self.GIT_CMD.split())

    def setup(self, data):
        os.chdir(INSTALL_DIR)

        import pip
        pip.main("install --upgrade -r requirements.txt".split())


class SetupMusicbot(SetupTask):
    def _rm(self, f):
        try:
            return os.unlink(f)
        except:
            pass

    def _rm_glob(self, p):
        fs = glob(p)
        for f in fs:
            self._rm(f)

    def _rm_dir(self, d):
        return rmtree(d, ignore_errors=True)

    def download(self):
        self._rm('.dockerignore')
        self._rm('Dockerfile')

    def setup_win32(self, data):
        self._rm_glob('*.sh')
        self._rm_glob('*.command')

    def setup_linux(self, data):
        self._rm_glob('*.bat')
        self._rm_glob('*.command')
        self._rm_dir('bin')

    def setup_darwin(self, data):
        self._rm_glob('*.bat')
        self._rm_glob('*.sh')
        self._rm_dir('bin')


###############################################################################


def preface():
    print(" MusicBot Bootstrapper (v0.1) ".center(50, '#'))
    print("This script will install the MusicBot into a folder called '%s' in your current directory." % INSTALL_DIR,
          "\nDepending on your system and environment, several packages and dependencies will be installed.",
          "\nTo ensure there are no issues, you should probably run this script as an administrator.")
    print()
    raw_input("Press enter to begin. ")
    print()


def main():
    preface()
    print("Bootstrapping MusicBot on Python %s." % '.'.join(list(map(str, PY_VERSION))))

    EnsurePython.run()
    EnsureBrew.run()
    EnsureGit.run()
    EnsureFFmpeg.run()
    EnsureOpus.run()
    EnsureFFI.run()
    EnsureSodium.run()
    EnsureCompiler.run()
    EnsurePip.run()
    GitCloneMusicbot.run()
    SetupMusicbot.run()


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        pass
    except Exception:
        traceback.print_exc()
        raw_input("An error has occured, press enter to exit. ")
    finally:
        TEMP_DIR.cleanup()