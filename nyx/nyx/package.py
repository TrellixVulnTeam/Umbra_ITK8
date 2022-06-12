import os
from posixpath import curdir, dirname, split
import shutil
import subprocess
import tarfile
from string import Template
from pkg_resources import parse_version
from nyx.globals import *

class NyxPackage:
    """A packaged application, potentially from source"""

    def __init__(self, name, path):
        self.name: str = name
        self.version = NyxPackage.pkgstr_version(name)
        self.state = {
            "have_source": False,
            "patched": False,
            "built": False,
            "built_from_source": False,
            "installed": False,
            "architecture": "x86_64"
        }
        self.description = ""
        self.architectures = ["*"]
        self.installType = "sysroot"
        self.dependencies = []
        self.patches = []
        self.steps = {}
        self.steps["configure"] = []
        self.steps["build"] = []
        self.steps["package"] = []
        self.install_root = '/lib/'
        self.pkg_file_path = path

        # Build Stuff
        self.buildEnvironment = {}
        self.buildEnvironment["env"] = {}
        self.source = {}
        self.source["type"] = "local" # Local, Local Copy, Git, http, 


    def compareVersions(x, y):
        x_ver = parse_version(x.version)
        y_ver = parse_version(y.version)

        if x_ver > y_ver:
            return -1
        elif x_ver < y_ver:
            return 1
        return 0

    def loadJson(self, pkg_json:any):
        self.name                    = pkg_json["name"] or self.name
        self.architectures           = pkg_json.get("architecture", ["*"])
        self.description             = pkg_json.get("description", "")
        self.dependencies            = pkg_json.get("depends_on", [])
        self.patches                 = pkg_json.get("patches", [])
        self.buildEnvironment["env"] = pkg_json.get("environment", dict())
        self.installType             = pkg_json.get("install_type", "sysroot") # "sysroot", "tool", "initrd" are valid options
        self.steps["configure"]      = pkg_json.get("configure_steps", [])
        self.steps["build"]          = pkg_json.get("build_steps", [])
        self.steps["package"]        = pkg_json.get("package_steps", [])
        self.install_root            = pkg_json.get("install_root", "/")
        self.source["type"]          = pkg_json.get("acquisition", "local")
        self.source["path"]          = pkg_json.get("src_uri", "")
        self.source["branch"]        = pkg_json.get("git_branch", "master")
        self.source["tag"]           = pkg_json.get("git_tag", "")
        pass

    def print_info(self):
        nyx_log.info(f"Package {self.name}-{self.version} for {self.state['architecture']}")
        nyx_log.info(f"Install Type: {self.installType}")
        nyx_log.info(f"install root: {self.install_root}")
        nyx_log.info(f"supports: {self.supported_arch}")
        nyx_log.info(f"depends on: {self.dependencies}")
        nyx_log.info(f"patches: {len(self.patches)}")
        nyx_log.info(f"steps: {self.steps}")
        nyx_log.info(f"source: {self.source}")
        nyx_log.info(f"environment: {self.buildEnvironment}")        


    def pkgstr_version(s: str) -> str or None:
        ver_part = s.rpartition('-')
        return ver_part[2] if ver_part[0] != "" else None


    def pkgstr_name(s: str) -> str: return s.rpartition('-')[0] or s


    def execute_commands(self, steps, cwd, config, env) -> bool:
        final_env = self.compute_environment(config, env)
        #nyx_log.info(f"Execute: {cwd}, {steps}, env {final_env}")

        for x in steps:
            temp_obj = Template(x)
            prefix = final_env['INSTALL_DIR']
            tool_prefix = os.path.abspath(config['build_env']['tool_path']) + "/" + self.install_root
            parsed_steps = temp_obj.substitute(SYSROOT=final_env["SYSROOT"],INSTALL_DIR=final_env["INSTALL_DIR"], TARGET=f'{config["target"]}-umbra', PREFIX=prefix,TOOLPREFIX=tool_prefix, THREADS=f'{os.cpu_count()}')
            splitargs = [parsed_steps]
            status = subprocess.run(splitargs, shell=True, cwd=cwd, env=final_env)
            if status.returncode != 0:
                return False
        return True


    def get_source_dir(self, config) -> str:
        if (self.source["type"] == "local"):
            return config["build_env"]["source_path"] + self.source["path"] 
        return f"{config['build_env']['build_path']}src/{self.name}-{self.version}";


    def compute_environment(self, config, global_environment):
        env = dict()
        env |= global_environment
        env['INSTALL_DIR'] = os.path.abspath(config["build_env"]["build_path"] + f"tmp/install/{self.name}/")
        self.util_createpath(env['INSTALL_DIR'])

        env['SYSROOT']     = os.path.abspath(config["build_env"]["system_root"])
        self.util_createpath(env['SYSROOT'])

        env['BUILD_DIR']   = os.path.abspath(config["build_env"]["build_path"] + f"tmp/build/{self.name}")
        self.util_createpath(env['BUILD_DIR'])
        if (self.installType == "tool"):
            env['CC'] = 'gcc' # Native
            env['CXX'] = 'g++'# Native
            env['AR'] = 'ar'  # Native
        else:
            env['DESTDIR'] = os.path.abspath(config["build_env"]["build_path"] + f"tmp/install/{self.name}/")

        env['PATH'] = os.path.abspath(config["build_env"]["tool_path"]) + "/host-tools/bin" + ":" + env['PATH']
        env |= self.buildEnvironment["env"]
        return env


    def build(self, config, args, env, shouldInstall=True):
        self.state["built_from_source"] = True

        if (not self.state["have_source"]): 
            nyx_log.debug(f"Obtaining Source for {self.name}")
            if self.get_source(config, env):
                self.state["have_source"] = True
            else:
                nyx_log.info(f"Failed in obtaining source files for {self.name}!")
                return False;

        if (not self.state["patched"] and len(self.patches) > 0): 
            nyx_log.debug(f"Patching {self.name}...")
            if self.patch(config, env):
                self.state["patched"] = True
            else:
                nyx_log.info(f"Failed in patching source files for {self.name}!")
                return False;

        if (not self.state["built"] and (len(self.steps['configure']) > 0 or len(self.steps['build']) > 0)): 
            nyx_log.debug(f"Building {self.name}...")
            if not self.configure(config, env):
                nyx_log.info(f"Failed in configuration for {self.name}!")
                return False;
            if not self.compile(config, env):
                nyx_log.info(f"Encountered a compiliaton error for {self.name}!")
                return False;
            self.state["built"] = True

        if (not self.has_package(config)): 
            nyx_log.debug(f"Packaging {self.name}...")
            if not self.package(config, env):
                nyx_log.info(f"Failed in packaging files for {self.name}!")
                return False;
        
        if (not self.state["installed"] and shouldInstall):
            nyx_log.debug(f"Installing {self.name}")
            if self.install(config, env):
                self.state["installed"] = True
            else:
                nyx_log.info(f"Failed in installing file to system root for {self.name}!")
                return False;

        # Clean up
        if not args.no_clean:
            nyx_log.info(f"nbuild: {self.name} cleaning up")
            from nyx.actions.package_clean import CleanAction
            CleanAction(config, self.compute_environment(config, env), self).execute()
        # We've packaged it!
        nyx_log.info(f"nbuild: {self.name} packaged successfully.")

        return True;


    def get_source(self, config, env):
        dest_dir = self.get_source_dir(config)

        if (self.source["type"] == 'local'):
            # Out of source build, nothing to do if we already have the files.
            pass
        elif (self.source["type"] == 'local_copy'):
            self.util_createpath(dest_dir)
            shutil.copytree(config["build_env"]["source_path"] + self.source["path"], dest_dir, dirs_exist_ok=True)
        elif (self.source["type"] == 'git'):
            # TODO: How should this be handled?
            if os.path.isdir(os.path.abspath(dest_dir)):
                return True
            self.util_createpath(dest_dir)
            branch = self.source["tag"] if self.source["tag"] != "" else self.source["branch"]
            status = subprocess.run(['git', 'clone', self.source["path"], f'--branch={branch}','--depth=1','.'], shell=False, cwd=dest_dir)
            return status.returncode == 0
        else:
            return False    
        return True


    def has_package(self, config):
        return os.path.exists(self.pkg_path(config))


    def patch(self, config, env) -> bool:
        tmp_dir = self.get_source_dir(config)
        if len(self.patches) > 0:
            for patch_path in self.patches:
                patch = os.path.join(self.pkg_file_path, patch_path)
                nyx_log.debug(f"[nyx]: Applying patch {patch} to {tmp_dir}")
                with open(os.path.abspath(patch), "rb") as f:
                    patch_data = f.read()
                process = subprocess.Popen(['patch', '-ruN', '-p1', '-d', '.'], shell=False, cwd=tmp_dir, stdin=subprocess.PIPE)
                process.communicate(input=patch_data)
        return True


    def configure(self, config, env):
        return self.execute_commands(self.steps["configure"], self.get_source_dir(config), config, env)


    def compile(self, config, env):
        return self.execute_commands(self.steps["build"], self.get_source_dir(config), config, env)


    def package(self, config, env) -> bool:
        pkg_path = os.path.abspath(config["build_env"]["package_cache"])
        self.util_createpath(pkg_path)

        if (self.installType == 'tool'):
            install_dir = os.path.abspath(f"{config['build_env']['tool_path']}/{self.install_root}")
        else:
            install_dir = os.path.abspath(config["build_env"]["build_path"] + f"tmp/install/{self.name}/")
        self.util_createpath(install_dir)
        
        successful = self.execute_commands(self.steps["package"], self.get_source_dir(config), config, env)
        if not successful:
            return False

        # Package into a NPA...
        with tarfile.open(self.pkg_path(config), "w:gz") as zip_ref:
            zip_ref.add(install_dir, arcname='', recursive=True)
        return True


    def install(self, config, env):
        """Installs the package into the system root"""
        from nyx.actions.package_install import InstallAction
        return InstallAction(config, self.compute_environment(config, env), self).execute()


    def clean(self, config, env):
        """Cleans all compiled traces of this package"""
        final_env = self.compute_environment(config, env)
        self.state["installed"] = False
        if self.has_package(config):
            os.remove(self.pkg_path(config))
        from nyx.actions.package_clean import CleanAction
        return CleanAction(config, self.compute_environment(config, env), self).execute()


    def util_createpath(self, path):
        if not os.path.isdir(os.path.abspath(path)):
            os.makedirs(os.path.abspath(path))


    def pkg_path(self, config):
        type = config['host_type']
        return f'{os.path.abspath(config[f"{type}_env"]["package_cache"])}/{self.name}-{self.version}.tar.gz'