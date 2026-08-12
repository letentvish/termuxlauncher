#!/usr/bin/env python3
import http.server
import socketserver
import json
import os
import sys
import subprocess
import threading
import socket
import urllib.parse
import traceback
import shutil

# Default configuration ports
PORTS = {
    "Frontend": 3000,
    "Backend": 8000,
    "AI Engine": 8010
}

def read_env(filepath):
    values = {}
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
    return values

def write_env(filepath, new_values):
    values = read_env(filepath)
    values.update(new_values)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        for k, v in sorted(values.items()):
            f.write(f"{k}={v}\n")

class AppConfig:
    def __init__(self, app_dir):
        self.app_dir = os.path.abspath(app_dir)
        self.manifest_path = os.path.join(self.app_dir, "launcher.json")
        self.services = {}
        self.env_keys = []
        self.load()

    def load(self):
        # 1. Try to load launcher.json
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.services = data.get("services", {})
                    self.env_keys = data.get("required_keys", [])
                    return
            except Exception as e:
                print(f"Error loading launcher.json: {e}")

        # 2. Heuristics fallback stack detection
        detected_services = {}
        
        # Check backend
        be_candidates = ["backend", "api", "server"]
        be_dir = None
        be_entry = None
        for cand in be_candidates:
            path = os.path.join(self.app_dir, cand)
            if os.path.isdir(path):
                for entry in ["main.py", "app.py", "wsgi.py", "server.py"]:
                    if os.path.exists(os.path.join(path, entry)):
                        be_dir = path
                        be_entry = entry
                        break
                if be_dir:
                    break
        
        # Fallback to root directory
        if not be_dir:
            for entry in ["main.py", "app.py"]:
                if os.path.exists(os.path.join(self.app_dir, entry)):
                    be_dir = self.app_dir
                    be_entry = entry
                    break

        if be_entry:
            if be_entry.endswith(".py"):
                detected_services["Backend"] = {
                    "path": be_dir,
                    "runtime": "python",
                    "cmd": ["python", "-m", "uvicorn", f"{be_entry.split('.')[0]}:app", "--port", "8000", "--host", "0.0.0.0"],
                    "port": 8000
                }
            elif be_entry.endswith(".js"):
                detected_services["Backend"] = {
                    "path": be_dir,
                    "runtime": "node",
                    "cmd": ["node", be_entry],
                    "port": 8000
                }

        # Check AI Engine
        ai_candidates = ["ai_engine", "agent", "ai"]
        ai_dir = None
        for cand in ai_candidates:
            path = os.path.join(self.app_dir, cand)
            if os.path.isdir(path):
                if os.path.exists(os.path.join(path, "main.py")) or os.path.exists(os.path.join(path, "app.py")):
                    ai_dir = path
                    break
        
        if ai_dir:
            detected_services["AI Engine"] = {
                "path": ai_dir,
                "runtime": "python",
                "cmd": ["python", "-m", "uvicorn", "main:app", "--port", "8010", "--host", "0.0.0.0"],
                "port": 8010
            }

        # Check Frontend
        fe_candidates = ["frontend", "client", "ui", "web"]
        fe_dir = None
        for cand in fe_candidates:
            path = os.path.join(self.app_dir, cand)
            if os.path.isdir(path) and os.path.exists(os.path.join(path, "package.json")):
                fe_dir = path
                break
        
        if not fe_dir and os.path.exists(os.path.join(self.app_dir, "package.json")):
            fe_dir = self.app_dir

        if fe_dir is not None:
            detected_services["Frontend"] = {
                "path": fe_dir,
                "runtime": "node",
                "cmd": ["npm", "run", "dev"],
                "port": 3000
            }

        self.services = detected_services

        # 3. Detect env keys from .env.example files
        detected_keys = []
        for root, dirs, files in os.walk(self.app_dir):
            if any(x in root for x in ["node_modules", ".git", "venv", ".venv"]):
                continue
            for f in files:
                if f == ".env.example" or f == ".env":
                    env_ex_path = os.path.join(root, f)
                    try:
                        with open(env_ex_path, "r", encoding="utf-8") as file:
                            for line in file:
                                line = line.strip()
                                if line and not line.startswith("#") and "=" in line:
                                    k = line.split("=", 1)[0].strip()
                                    if k and k not in [dk["key"] for dk in detected_keys]:
                                        is_secret = any(s in k.upper() for s in ["KEY", "SECRET", "PASS", "TOKEN"])
                                        detected_keys.append({
                                            "key": k,
                                            "description": f"Env Key: {k}",
                                            "secret": is_secret,
                                            "dir": root
                                        })
                    except:
                        pass
        
        # Add basic fallbacks if no keys found
        if not detected_keys:
            default_keys = ["OPENAI_API_KEY", "GOOGLE_API_KEY", "NVIDIA_API_KEY", "TAVILY_API_KEY"]
            for dk in default_keys:
                detected_keys.append({
                    "key": dk,
                    "description": f"Fallback: {dk}",
                    "secret": True,
                    "dir": self.app_dir if not detected_services else list(detected_services.values())[0]["path"]
                })

        self.env_keys = detected_keys

class ProcessManager:
    def __init__(self):
        self.processes = {}
        self.logs = []
        self.lock = threading.Lock()
        self.launcher_dir = os.path.dirname(os.path.abspath(__file__))
        self.apps_dir = os.path.join(self.launcher_dir, "apps")
        os.makedirs(self.apps_dir, exist_ok=True)
        
        self.project_dir = os.path.abspath(self.launcher_dir)
        self.app_config = AppConfig(self.project_dir)
        
        self.log_limit = 1000
        
        # Task progress tracking
        self.is_task_running = False
        self.active_task_name = None

    def set_task_state(self, is_running, task_name=None):
        with self.lock:
            self.is_task_running = is_running
            self.active_task_name = task_name if is_running else None

    def set_project_dir(self, path):
        with self.lock:
            for name in list(self.processes.keys()):
                self._stop_service_locked(name)
            
            self.project_dir = os.path.abspath(path)
            self.app_config = AppConfig(self.project_dir)
            self.processes = {name: None for name in self.app_config.services}

    def add_log(self, text, stream="system"):
        with self.lock:
            self.logs.append({"time": os.popen("date +%T").read().strip(), "stream": stream, "text": text})
            if len(self.logs) > self.log_limit:
                self.logs.pop(0)

    def is_port_in_use(self, port):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                return s.connect_ex(('127.0.0.1', port)) == 0
        except:
            return False

    def get_status(self, name):
        svc = self.app_config.services.get(name)
        if not svc:
            return "UNKNOWN"
        
        port = svc.get("port")
        in_use = self.is_port_in_use(port) if port else False
        
        with self.lock:
            proc = self.processes.get(name)
        
        if proc and proc.poll() is None:
            return "RUNNING"
        elif in_use:
            return "BUSY (EXTERNAL)"
        return "STOPPED"

    def run_command(self, cmd, cwd, name=None, task_title=None):
        def worker():
            if task_title:
                self.set_task_state(True, task_title)
            try:
                self.add_log(f"Starting process: {' '.join(cmd)} in {cwd}", "system")
                use_shell = (os.name == 'nt')
                
                cmd_to_run = list(cmd)
                if name and self.app_config.services.get(name, {}).get("runtime") == "python":
                    venv_py = os.path.join(cwd, "venv", "bin", "python")
                    if not os.path.exists(venv_py):
                        venv_py = os.path.join(cwd, ".venv", "bin", "python")
                    if not os.path.exists(venv_py) and os.name == 'nt':
                        venv_py = os.path.join(cwd, "venv", "Scripts", "python.exe")
                    if os.path.exists(venv_py):
                        cmd_to_run[0] = venv_py
                        self.add_log(f"Using virtual environment python: {venv_py}", "system")

                env = dict(os.environ)
                if name and self.app_config.services.get(name, {}).get("port"):
                    env["PORT"] = str(self.app_config.services[name]["port"])
                env["CI"] = "true"

                process = subprocess.Popen(
                    cmd_to_run,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    shell=use_shell,
                    env=env
                )
                
                if name:
                    with self.lock:
                        self.processes[name] = process
                
                for line in iter(process.stdout.readline, ''):
                    self.add_log(line.strip(), name.lower() if name else "setup")
                
                process.stdout.close()
                rc = process.wait()
                self.add_log(f"Process exited with code {rc}", "system")
                if name:
                    with self.lock:
                        if self.processes[name] == process:
                            self.processes[name] = None
            except Exception as e:
                self.add_log(f"Process Error: {str(e)}", "error")
                if name:
                    with self.lock:
                        self.processes[name] = None
            finally:
                if task_title:
                    self.set_task_state(False)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def is_android_termux(self):
        return (
            os.path.exists("/data/data/com.termux") or 
            "PREFIX" in os.environ or 
            "TERMUX_VERSION" in os.environ or
            shutil.which("pkg") is not None
        )

    def ensure_nextjs_swc_fix(self, frontend_path):
        if not self.is_android_termux():
            return
        
        babelrc = os.path.join(frontend_path, ".babelrc")
        babel_js = os.path.join(frontend_path, "babel.config.js")
        
        if not os.path.exists(babelrc) and not os.path.exists(babel_js):
            try:
                with open(babelrc, "w", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "presets": ["next/babel"]
                    }, indent=2))
                self.add_log("Created .babelrc in frontend folder to opt-out of SWC and fall back to Babel (Android bypass).", "system")
            except Exception as e:
                self.add_log(f"Warning: Failed to auto-create .babelrc: {str(e)}", "error")

    def ensure_frontend_deps(self, frontend_path):
        self.ensure_nextjs_swc_fix(frontend_path)
        
        node_modules = os.path.join(frontend_path, "node_modules")
        package_json = os.path.join(frontend_path, "package.json")
        use_shell = (os.name == 'nt')
        
        if os.path.exists(package_json):
            # 1. Run npm install if node_modules is missing
            if not os.path.exists(node_modules):
                self.add_log("node_modules missing in frontend directory. Running npm install automatically...", "system")
                try:
                    proc = subprocess.Popen(["npm", "install"], cwd=frontend_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=use_shell)
                    for line in iter(proc.stdout.readline, ''):
                        self.add_log(line.strip(), "setup")
                    proc.wait()
                except Exception as e:
                    self.add_log(f"Auto npm install error: {str(e)}", "error")
            
            # 2. Guarantee SWC compilers (WASM & ARM64) exist on Android/Termux
            if self.is_android_termux():
                arm_pkg = os.path.join(node_modules, "@next", "swc-android-arm64")
                wasm_pkg = os.path.join(node_modules, "@next", "swc-wasm-nodejs")
                
                if not os.path.exists(arm_pkg) and not os.path.exists(wasm_pkg):
                    self.add_log("Android/Termux detected. Installing @next/swc-wasm-nodejs & @next/swc-android-arm64 SWC compilers...", "system")
                    try:
                        proc2 = subprocess.Popen(["npm", "install", "@next/swc-wasm-nodejs", "@next/swc-android-arm64", "--save-optional"], cwd=frontend_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=use_shell)
                        for line in iter(proc2.stdout.readline, ''):
                            self.add_log(line.strip(), "setup")
                        proc2.wait()
                    except Exception as e:
                        self.add_log(f"Auto SWC install error: {str(e)}", "error")

    def ensure_python_deps(self, service_name, cwd):
        venv_dir = os.path.join(cwd, ".venv")
        req_path = os.path.join(cwd, "requirements.txt")
        
        if os.path.exists(req_path) and not os.path.exists(venv_dir):
            self.add_log(f"Virtual environment missing for {service_name}. Creating .venv and running pip install...", "system")
            use_shell = (os.name == 'nt')
            try:
                proc_venv = subprocess.Popen(["python", "-m", "venv", ".venv"], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=use_shell)
                for line in iter(proc_venv.stdout.readline, ''):
                    self.add_log(line.strip(), "setup")
                proc_venv.wait()
                
                venv_py = os.path.join(venv_dir, "bin", "python")
                if os.name == 'nt':
                    venv_py = os.path.join(venv_dir, "Scripts", "python.exe")
                
                if os.path.exists(venv_py):
                    proc_pip = subprocess.Popen([venv_py, "-m", "pip", "install", "-r", "requirements.txt"], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=use_shell)
                    for line in iter(proc_pip.stdout.readline, ''):
                        self.add_log(line.strip(), "setup")
                    proc_pip.wait()
            except Exception as e:
                self.add_log(f"Auto python setup error for {service_name}: {str(e)}", "error")

    def start_service(self, name):
        status = self.get_status(name)
        if status == "RUNNING":
            return {"status": "success", "message": f"{name} is already running."}
        
        svc = self.app_config.services.get(name)
        if not svc:
            return {"status": "error", "message": "Unknown service"}

        cwd = svc["path"]
        cmd = svc["cmd"]
        
        if name == "Backend" and svc.get("runtime") == "python":
            self.ensure_backend_env()
            self.ensure_python_deps(name, cwd)
        elif name == "AI Engine" and svc.get("runtime") == "python":
            self.ensure_python_deps(name, cwd)
        elif name == "Frontend" and svc.get("runtime") == "node":
            self.ensure_frontend_deps(cwd)

        self.run_command(cmd, cwd, name)
        return {"status": "success", "message": f"Started {name}"}

    def stop_service(self, name):
        with self.lock:
            return self._stop_service_locked(name)

    def _stop_service_locked(self, name):
        proc = self.processes.get(name)
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=3)
                self.add_log(f"Terminated {name} cleanly.", "system")
            except subprocess.TimeoutExpired:
                proc.kill()
                self.add_log(f"Force killed {name}.", "system")
            except Exception as e:
                self.add_log(f"Error stopping {name}: {str(e)}", "error")
            
            self.processes[name] = None
            return {"status": "success", "message": f"Stopped {name}"}
        return {"status": "error", "message": f"{name} was not running."}

    def ensure_backend_env(self):
        backend_svc = self.app_config.services.get("Backend")
        if not backend_svc:
            return
        
        env_path = os.path.join(backend_svc["path"], ".env")
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        
        has_secret = False
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("SECRET_KEY="):
                        has_secret = True
                        break
        
        if not has_secret:
            import secrets
            with open(env_path, "a", encoding="utf-8") as f:
                if os.path.exists(env_path) and os.path.getsize(env_path) > 0:
                    f.write("\n")
                f.write(f"SECRET_KEY={secrets.token_urlsafe(48)}\n")
            self.add_log("Generated backend SECRET_KEY in backend/.env", "system")

    def install_prereqs(self):
        is_termux = self.is_android_termux()
        if not is_termux:
            return {"status": "error", "message": "This operation is optimized for Termux. Please install build libraries manually on your system."}
        
        cmd = ["pkg", "install", "-y", "nodejs", "python", "git", "clang", "make", "pkg-config", "libffi", "openssl", "rust", "tur-repo"]
        self.run_command(cmd, self.project_dir, task_title="Installing System Packages")
        return {"status": "success", "message": "Triggered Termux build dependencies installation."}

    def install_deps(self):
        def worker():
            self.set_task_state(True, "Installing App Modules")
            try:
                use_shell = (os.name == 'nt')
                
                for name, svc in self.app_config.services.items():
                    cwd = svc["path"]
                    if svc["runtime"] == "python":
                        self.add_log(f"Setting up Python virtual environment for {name}...", "system")
                        
                        venv_dir = os.path.join(cwd, ".venv")
                        if not os.path.exists(venv_dir):
                            try:
                                self.add_log(f"Creating venv in {venv_dir}...", "system")
                                proc_venv = subprocess.Popen(["python", "-m", "venv", ".venv"], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=use_shell)
                                for line in iter(proc_venv.stdout.readline, ''): self.add_log(line.strip(), "setup")
                                proc_venv.wait()
                            except Exception as e:
                                self.add_log(f"Venv creation failed: {str(e)}", "error")
                        
                        venv_py = os.path.join(venv_dir, "bin", "python")
                        if os.name == 'nt':
                            venv_py = os.path.join(venv_dir, "Scripts", "python.exe")
                        
                        if not os.path.exists(venv_py):
                            venv_py = "python"
                            self.add_log("Venv python not found, falling back to system Python.", "error")
                        
                        self.add_log(f"Installing python dependencies in venv using {venv_py}...", "system")
                        req_path = os.path.join(cwd, "requirements.txt")
                        if os.path.exists(req_path):
                            proc = subprocess.Popen([venv_py, "-m", "pip", "install", "-r", "requirements.txt"], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=use_shell)
                            for line in iter(proc.stdout.readline, ''): self.add_log(line.strip(), "setup")
                            proc.wait()
                            
                    elif svc["runtime"] == "node":
                        self.add_log(f"Installing npm packages for {name}...", "system")
                        package_path = os.path.join(cwd, "package.json")
                        if os.path.exists(package_path):
                            proc = subprocess.Popen(["npm", "install"], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=use_shell)
                            for line in iter(proc.stdout.readline, ''): self.add_log(line.strip(), "setup")
                            proc.wait()
                            
                            if self.is_android_termux():
                                self.add_log("Android/Termux detected. Installing SWC WASM & ARM64 compiler binaries...", "system")
                                proc2 = subprocess.Popen(["npm", "install", "@next/swc-wasm-nodejs", "@next/swc-android-arm64", "--save-optional"], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=use_shell)
                                for line in iter(proc2.stdout.readline, ''): self.add_log(line.strip(), "setup")
                                proc2.wait()
                
                self.add_log("All dependencies installation processes complete.", "system")
            finally:
                self.set_task_state(False)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return {"status": "success", "message": "Triggered pip & npm installation scripts."}

    def list_apps(self):
        apps = [{
            "name": "Default (Local)",
            "path": self.launcher_dir,
            "is_active": (self.project_dir == self.launcher_dir)
        }]
        if os.path.exists(self.apps_dir):
            for item in sorted(os.listdir(self.apps_dir)):
                full_path = os.path.join(self.apps_dir, item)
                if os.path.isdir(full_path):
                    if item.startswith('.') or item == '__pycache__':
                        continue
                    apps.append({
                        "name": item,
                        "path": full_path,
                        "is_active": (self.project_dir == full_path)
                    })
        return apps

    def clone_app(self, repo_url):
        url_path = urllib.parse.urlparse(repo_url).path
        repo_name = url_path.strip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        
        if not repo_name:
            return {"status": "error", "message": "Could not determine repository name from URL."}
        
        dest_dir = os.path.join(self.apps_dir, repo_name)
        if os.path.exists(dest_dir):
            return {"status": "error", "message": f"App '{repo_name}' already exists in library."}
        
        cmd = ["git", "clone", repo_url, dest_dir]
        self.run_command(cmd, self.launcher_dir, task_title=f"Cloning {repo_name}")
        return {"status": "success", "message": f"Started cloning {repo_name}..."}

    def run_diagnostics(self):
        def worker():
            self.set_task_state(True, "Running System Diagnostics")
            try:
                self.add_log("=== RUNNING SYSTEM DIAGNOSTICS ===", "system")
                use_shell = (os.name == 'nt')
                
                try:
                    res = subprocess.run(["python", "--version"], capture_output=True, text=True, shell=use_shell)
                    self.add_log(f"[OK] Python version: {res.stdout.strip() or res.stderr.strip()}", "system")
                except Exception as e:
                    self.add_log(f"[FAIL] Python check: {str(e)}", "error")

                try:
                    res = subprocess.run(["node", "--version"], capture_output=True, text=True, shell=use_shell)
                    self.add_log(f"[OK] Node.js version: {res.stdout.strip() or res.stderr.strip()}", "system")
                except Exception as e:
                    self.add_log(f"[FAIL] Node.js check: {str(e)}", "error")

                for name, svc in self.app_config.services.items():
                    if svc["runtime"] == "python":
                        self.add_log(f"Checking Python libraries in target folder: {svc['path']}", "system")
                        test_script = "import sys; import fastapi; import uvicorn; import pydantic; import openai; print('OK: All imports succeeded!')"
                        try:
                            cmd = ["python", "-c", test_script]
                            venv_py = os.path.join(svc["path"], "venv", "bin", "python")
                            if not os.path.exists(venv_py):
                                venv_py = os.path.join(svc["path"], ".venv", "bin", "python")
                            if os.path.exists(venv_py):
                                cmd[0] = venv_py
                            
                            res = subprocess.run(cmd, capture_output=True, text=True, shell=use_shell)
                            if res.returncode == 0:
                                self.add_log(f"[OK] Python libraries ({name}): {res.stdout.strip()}", "system")
                            else:
                                self.add_log(f"[FAIL] Python libraries ({name}) error: {res.stderr.strip()}", "error")
                                self.add_log("[TIP] Make sure to run '2. App Modules' to install missing requirements.", "system")
                        except Exception as e:
                            self.add_log(f"[FAIL] Python import test failed to run: {str(e)}", "error")

                for name, svc in self.app_config.services.items():
                    if svc["runtime"] == "node":
                        next_binary = os.path.join(svc["path"], "node_modules", ".bin", "next")
                        if os.path.exists(next_binary) or os.path.exists(next_binary + ".cmd"):
                            self.add_log(f"[OK] node_modules found for {name}.", "system")
                        else:
                            self.add_log(f"[FAIL] node_modules is missing or incomplete for {name}.", "error")
                for name, svc in self.app_config.services.items():
                    port = svc.get("port")
                    if port:
                        if self.is_port_in_use(port):
                            self.add_log(f"[INFO] Port {port} ({name}) is CURRENTLY IN USE by an external process.", "system")
                        else:
                            self.add_log(f"[OK] Port {port} ({name}) is free.", "system")

                self.add_log("=== DIAGNOSTICS COMPLETE ===", "system")
            except Exception as e:
                self.add_log(f"Diagnostics error: {str(e)}", "error")
            finally:
                self.set_task_state(False)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return {"status": "success", "message": "Diagnostic tests started. Check logs below."}

# Initialize global process manager
manager = ProcessManager()

class WebLauncherHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass 

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_UI.encode('utf-8'))
            return

        elif path == "/api/status":
            with manager.lock:
                task_state = {
                    "running": manager.is_task_running,
                    "name": manager.active_task_name
                }
            data = {
                "project_dir": manager.project_dir,
                "task_state": task_state,
                "services": {
                    name: {
                        "status": manager.get_status(name),
                        "port": manager.app_config.services[name].get("port")
                    } for name in manager.app_config.services
                }
            }
            self.send_json(data)
            return

        elif path == "/api/logs":
            with manager.lock:
                logs_copy = list(manager.logs)
            self.send_json({"logs": logs_copy})
            return

        elif path == "/api/apps":
            self.send_json({"apps": manager.list_apps()})
            return

        elif path == "/api/settings":
            schema = []
            for env_item in manager.app_config.env_keys:
                key = env_item["key"]
                folder = env_item["dir"]
                env_path = os.path.join(folder, ".env")
                env_data = read_env(env_path)
                
                schema.append({
                    "key": key,
                    "description": env_item["description"],
                    "secret": env_item["secret"],
                    "value": env_data.get(key, "")
                })
            self.send_json({"schema": schema})
            return

        elif path == "/api/browse":
            target_path = query.get('path', [os.path.expanduser("~")])[0]
            try:
                if not os.path.exists(target_path):
                    target_path = os.path.expanduser("~")
                
                items = []
                parent = os.path.dirname(target_path)
                if parent != target_path:
                    items.append({"name": "..", "path": parent, "is_dir": True})

                for f in sorted(os.listdir(target_path)):
                    full_p = os.path.join(target_path, f)
                    if f.startswith('.') and f != '.env':
                        continue
                    items.append({
                        "name": f,
                        "path": full_p,
                        "is_dir": os.path.isdir(full_p)
                    })
                
                self.send_json({
                    "current": target_path,
                    "items": items
                })
            except Exception as e:
                self.send_json({"error": str(e)}, status=500)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length).decode('utf-8')
        try:
            body = json.loads(post_data) if post_data else {}
        except:
            body = {}

        if path == "/api/select_folder":
            folder = body.get("folder")
            if folder and os.path.exists(folder):
                manager.set_project_dir(folder)
                self.send_json({"status": "success", "message": f"Folder selected: {manager.project_dir}"})
            else:
                self.send_json({"status": "error", "message": "Invalid directory folder path."}, status=400)
            return

        elif path == "/api/apps/clone":
            repo_url = body.get("repo_url", "").strip()
            if not repo_url:
                self.send_json({"status": "error", "message": "Repo URL is required."}, status=400)
                return
            res = manager.clone_app(repo_url)
            self.send_json(res)
            return

        elif path == "/api/apps/select":
            app_path = body.get("path")
            if app_path and os.path.exists(app_path):
                manager.set_project_dir(app_path)
                self.send_json({"status": "success", "message": f"Switched to app context at: {manager.project_dir}"})
            else:
                self.send_json({"status": "error", "message": "App path does not exist."}, status=400)
            return

        elif path == "/api/settings":
            try:
                for env_item in manager.app_config.env_keys:
                    key = env_item["key"]
                    folder = env_item["dir"]
                    
                    if key in body:
                        val = str(body[key]).strip()
                        env_path = os.path.join(folder, ".env")
                        write_env(env_path, {key: val})
                
                manager.add_log("Environment configuration sync complete.", "system")
                self.send_json({"status": "success", "message": "Settings updated successfully."})
            except Exception as e:
                self.send_json({"status": "error", "message": f"Save failed: {str(e)}"}, status=500)
            return

        elif path == "/api/diagnose":
            res = manager.run_diagnostics()
            self.send_json(res)
            return

        elif path == "/api/start":
            name = body.get("service")
            res = manager.start_service(name)
            self.send_json(res)
            return

        elif path == "/api/stop":
            name = body.get("service")
            res = manager.stop_service(name)
            self.send_json(res)
            return

        elif path == "/api/install_prereqs":
            res = manager.install_prereqs()
            self.send_json(res)
            return

        elif path == "/api/install_deps":
            res = manager.install_deps()
            self.send_json(res)
            return

        self.send_error(404, "Not Found")

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

# The Single Page App Dashboard HTML/JS
HTML_UI = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>AstroDash Termux Launcher</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        .custom-scrollbar::-webkit-scrollbar {
            width: 4px;
            height: 4px;
        }
        .custom-scrollbar::-webkit-scrollbar-track {
            background: rgba(255, 255, 255, 0.03);
        }
        .custom-scrollbar::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.15);
            border-radius: 2px;
        }
    </style>
</head>
<body class="bg-[#0A0D1A] text-slate-100 font-sans min-h-screen pb-10 antialiased selection:bg-indigo-500/30">

    <!-- Top Navigation Header -->
    <header class="border-b border-white/5 bg-slate-900/60 backdrop-blur-md sticky top-0 z-40 px-4 py-3 flex items-center justify-between">
        <div class="flex items-center space-x-2.5">
            <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-lg shadow-indigo-500/20">
                <i class="fa-solid fa-compass text-white text-lg animate-spin-slow"></i>
            </div>
            <div>
                <h1 class="text-base font-bold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-white to-slate-300">AstroDash Mobile</h1>
                <p class="text-[10px] text-indigo-400 font-semibold tracking-wider uppercase">Generic Controller</p>
            </div>
        </div>
        <div class="flex items-center space-x-2">
            <button onclick="runDiagnostics()" class="text-[10px] font-bold px-2.5 py-1 bg-amber-500/10 text-amber-400 border border-amber-500/20 rounded-lg hover:bg-amber-500/20 transition-all flex items-center gap-1">
                <i class="fa-solid fa-stethoscope"></i> Check Health
            </button>
        </div>
    </header>

    <!-- Main Container -->
    <main class="max-w-md mx-auto px-4 mt-6 space-y-6">

        <!-- 1. App Library & GitHub Cloning Card -->
        <section class="bg-white/5 border border-white/10 rounded-2xl p-5 shadow-xl backdrop-blur-sm space-y-4">
            <h2 class="text-sm font-bold tracking-wide text-cyan-300 uppercase flex items-center gap-1.5">
                <i class="fa-solid fa-square-rss text-xs"></i> App Library
            </h2>
            <div class="space-y-3">
                <div class="flex gap-2">
                    <input type="text" id="repoUrlInput" placeholder="Paste GitHub Repository URL" class="flex-1 bg-black/20 border border-white/5 rounded-xl px-4 py-2.5 text-xs focus:outline-none focus:border-indigo-500/50 transition-all font-mono" />
                    <button id="btn_import_repo" onclick="cloneApp()" class="px-4 py-2.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl text-xs font-bold transition-all shadow-md active:scale-95">
                        Import
                    </button>
                </div>
                <div class="space-y-2 mt-2 max-h-48 overflow-y-auto custom-scrollbar" id="appListContainer">
                    <!-- Populated by JS -->
                </div>
            </div>
        </section>

        <!-- 2. Dynamic Configuration Settings Card -->
        <section class="bg-white/5 border border-white/10 rounded-2xl p-5 shadow-xl backdrop-blur-sm space-y-4">
            <div class="flex items-center justify-between">
                <h2 class="text-sm font-bold tracking-wide text-amber-400 uppercase flex items-center gap-1.5">
                    <i class="fa-solid fa-sliders text-xs"></i> App Configurations
                </h2>
                <span class="text-[9px] text-slate-500 uppercase font-bold">Dynamic Env</span>
            </div>
            
            <div class="space-y-3.5" id="dynamicSettingsForm">
                <!-- Dynamically generated input fields -->
                <div class="text-xs text-slate-500 text-center py-4">Scanning configurations...</div>
            </div>
        </section>

        <!-- 3. Folder Selection Card (Advanced) -->
        <section class="bg-white/5 border border-white/10 rounded-2xl p-5 shadow-xl backdrop-blur-sm space-y-4">
            <div class="flex items-center justify-between">
                <h2 class="text-sm font-bold tracking-wide text-indigo-300 uppercase flex items-center gap-1.5">
                    <i class="fa-solid fa-folder-open text-xs"></i> Active Path Context
                </h2>
                <button onclick="toggleFolderModal(true)" class="text-[10px] px-2 py-1 bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg text-slate-300 transition-colors">
                    <i class="fa-solid fa-search mr-1"></i> Browse Local
                </button>
            </div>
            <div class="p-3 bg-black/30 border border-white/5 rounded-xl flex items-center justify-between text-xs overflow-hidden">
                <span id="projectPathDisplay" class="font-mono truncate select-all mr-2">Loading...</span>
                <i class="fa-solid fa-circle-check text-emerald-500 text-xs shrink-0" id="dirCheck"></i>
            </div>
        </section>

        <!-- 4. Services Management -->
        <section class="space-y-3">
            <h2 class="text-xs font-bold tracking-widest text-slate-400 uppercase px-1">Active Port Channels</h2>
            
            <div class="grid gap-3" id="servicesGrid">
                <!-- Template service cards generated via JS -->
            </div>
        </section>

        <!-- 5. Setup Commands Pipeline -->
        <section class="bg-white/5 border border-white/10 rounded-2xl p-5 shadow-xl backdrop-blur-sm space-y-4">
            <div class="flex items-center justify-between">
                <h2 class="text-sm font-bold tracking-wide text-purple-300 uppercase flex items-center gap-1.5">
                    <i class="fa-solid fa-gears text-xs"></i> Installation Pipeline
                </h2>
            </div>
            
            <!-- Live Progress Indicator Banner -->
            <div id="pipelineStatusBanner" class="p-3 bg-slate-900/80 border border-white/10 rounded-xl flex items-center justify-between text-xs font-medium">
                <div class="flex items-center space-x-2.5">
                    <span id="pipelineStatusIcon" class="w-2 h-2 rounded-full bg-slate-500"></span>
                    <span id="pipelineStatusText" class="text-slate-400">Pipeline Idle</span>
                </div>
                <div id="pipelineSpinner" class="hidden">
                    <i class="fa-solid fa-circle-notch animate-spin text-indigo-400 text-sm"></i>
                </div>
            </div>

            <p class="text-xs text-slate-400 leading-relaxed">
                Run stages sequentially to configure dependencies for the currently active app.
            </p>
            <div class="grid grid-cols-2 gap-3 pt-1">
                <button id="btn_prereqs" onclick="triggerSetup('install_prereqs')" class="p-3 bg-slate-900 border border-white/15 hover:border-indigo-500/50 rounded-xl flex flex-col items-center justify-center text-center group transition-all active:scale-95">
                    <i id="icon_prereqs" class="fa-solid fa-cubes text-indigo-400 text-base mb-2 group-hover:scale-110 transition-transform"></i>
                    <span class="text-xs font-bold">1. System Pkgs</span>
                    <span class="text-[9px] text-slate-500 mt-0.5">Node, Python, Git</span>
                </button>
                <button id="btn_deps" onclick="triggerSetup('install_deps')" class="p-3 bg-slate-900 border border-white/15 hover:border-purple-500/50 rounded-xl flex flex-col items-center justify-center text-center group transition-all active:scale-95">
                    <i id="icon_deps" class="fa-solid fa-code-branch text-purple-400 text-base mb-2 group-hover:scale-110 transition-transform"></i>
                    <span class="text-xs font-bold">2. App Modules</span>
                    <span class="text-[9px] text-slate-500 mt-0.5">pip & npm scripts</span>
                </button>
            </div>
        </section>

        <!-- 6. Terminal Log Output -->
        <section class="bg-black/40 border border-white/15 rounded-2xl p-5 shadow-2xl space-y-4">
            <div class="flex items-center justify-between">
                <div class="flex items-center space-x-2">
                    <h2 class="text-sm font-bold tracking-wide text-amber-300 uppercase flex items-center gap-1.5">
                        <i class="fa-solid fa-terminal text-xs"></i> System Stream Logs
                    </h2>
                    <span id="logTaskBadge" class="hidden text-[9px] font-bold px-2 py-0.5 bg-indigo-500/20 text-indigo-400 border border-indigo-500/30 rounded-md animate-pulse flex items-center gap-1">
                        <i class="fa-solid fa-spinner animate-spin text-[8px]"></i> <span id="logTaskBadgeText">RUNNING</span>
                    </span>
                </div>
                <button onclick="clearUIStatusLogs()" class="text-[10px] px-2.5 py-0.5 text-slate-500 hover:text-slate-300 transition-colors">
                    Clear Logs
                </button>
            </div>
            
            <div id="logsTerminal" class="h-64 overflow-y-auto bg-black/60 border border-white/5 rounded-xl p-3.5 font-mono text-[10px] text-slate-300 leading-relaxed custom-scrollbar space-y-1">
                <div class="text-indigo-400/80">[SYSTEM] Terminal initialized. Waiting for process output logs...</div>
            </div>
        </section>

    </main>

    <!-- Overlay Folder Browser Modal -->
    <div id="folderModal" class="fixed inset-0 bg-black/80 backdrop-blur-md z-50 flex items-end justify-center hidden">
        <div class="bg-slate-900 border-t border-white/10 rounded-t-3xl w-full max-w-md h-[80vh] flex flex-col shadow-2xl">
            <!-- Modal Header -->
            <div class="px-5 py-4 border-b border-white/5 flex items-center justify-between">
                <div>
                    <h3 class="text-sm font-bold text-slate-200">Select Project Folder</h3>
                    <p class="text-[10px] text-slate-500" id="currentBrowsePath">Loading directory...</p>
                </div>
                <button onclick="toggleFolderModal(false)" class="w-8 h-8 rounded-full bg-white/5 flex items-center justify-center text-slate-400 hover:text-white transition-colors">
                    <i class="fa-solid fa-xmark"></i>
                </button>
            </div>
            <!-- Browser List -->
            <div class="flex-1 overflow-y-auto p-4 space-y-1.5 custom-scrollbar" id="directoryList">
                <!-- Populated by JS -->
            </div>
            <!-- Modal Footer -->
            <div class="p-4 border-t border-white/5 bg-slate-950/40 flex items-center gap-3">
                <button onclick="selectCurrentFolder()" class="flex-1 py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-xl text-xs transition-colors shadow-lg shadow-indigo-500/20 active:scale-98">
                    Confirm Selected Folder
                </button>
            </div>
        </div>
    </div>

    <!-- Toast Notifications -->
    <div id="toastContainer" class="fixed bottom-5 left-1/2 -translate-x-1/2 z-50 w-72 max-w-full space-y-2 pointer-events-none"></div>

    <script>
        let currentPath = '';
        let logsLength = 0;
        let settingsSchema = [];
        let isTaskActive = false;

        // Toast Helper
        function showToast(message, type = 'info') {
            const container = document.getElementById('toastContainer');
            const toast = document.createElement('div');
            toast.className = `p-3 rounded-xl border text-[11px] font-semibold shadow-2xl flex items-center space-x-2 pointer-events-auto animate-fade-in transition-all duration-300 bg-slate-900 ${
                type === 'success' ? 'border-emerald-500/30 text-emerald-400' :
                type === 'error' ? 'border-red-500/30 text-red-400' : 'border-indigo-500/30 text-indigo-400'
            }`;
            
            const icon = type === 'success' ? 'circle-check' : type === 'error' ? 'circle-exclamation' : 'circle-info';
            toast.innerHTML = `<i class="fa-solid fa-${icon} text-xs"></i> <span>${message}</span>`;
            container.appendChild(toast);
            
            setTimeout(() => {
                toast.classList.add('opacity-0', 'scale-95');
                setTimeout(() => toast.remove(), 300);
            }, 3000);
        }

        // Folder Modal Management
        function toggleFolderModal(show) {
            const modal = document.getElementById('folderModal');
            if (show) {
                modal.classList.remove('hidden');
                browseDirectory(currentPath || '');
            } else {
                modal.classList.add('hidden');
            }
        }

        async function browseDirectory(path) {
            const listEl = document.getElementById('directoryList');
            const pathDisplay = document.getElementById('currentBrowsePath');
            listEl.innerHTML = '<div class="py-10 text-center text-slate-500"><i class="fa-solid fa-spinner animate-spin text-lg"></i></div>';
            
            try {
                const res = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
                const data = await res.json();
                
                if (data.error) {
                    showToast(data.error, 'error');
                    return;
                }

                currentPath = data.current;
                pathDisplay.textContent = currentPath;
                
                listEl.innerHTML = '';
                data.items.forEach(item => {
                    const btn = document.createElement('button');
                    btn.className = `w-full p-3.5 rounded-xl border text-left flex items-center justify-between text-xs transition-colors ${
                        item.name === '..' ? 'bg-white/5 border-transparent text-indigo-400' : 
                        item.is_dir ? 'bg-slate-900 border-white/5 hover:border-indigo-500/30' : 'bg-transparent border-transparent opacity-60 pointer-events-none'
                    }`;
                    
                    btn.onclick = () => {
                        if (item.is_dir) browseDirectory(item.path);
                    };

                    const icon = item.name === '..' ? 'fa-arrow-left' : (item.is_dir ? 'fa-folder text-indigo-400' : 'fa-file-code');
                    btn.innerHTML = `
                        <span class="flex items-center space-x-2.5">
                            <i class="fa-solid ${icon}"></i>
                            <span class="font-medium">${item.name}</span>
                        </span>
                        ${item.is_dir && item.name !== '..' ? '<i class="fa-solid fa-chevron-right text-[9px] text-slate-500"></i>' : ''}
                    `;
                    listEl.appendChild(btn);
                });
            } catch (e) {
                listEl.innerHTML = '<div class="p-4 text-center text-red-500">Error loading files</div>';
            }
        }

        async function selectCurrentFolder() {
            try {
                const res = await fetch('/api/select_folder', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ folder: currentPath })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    showToast(data.message, 'success');
                    toggleFolderModal(false);
                    fetchStatus();
                    fetchApps();
                    loadSettings();
                } else {
                    showToast(data.message, 'error');
                }
            } catch (e) {
                showToast("Request failed", 'error');
            }
        }

        // App Management UI
        async function fetchApps() {
            try {
                const res = await fetch('/api/apps');
                const data = await res.json();
                
                const list = document.getElementById('appListContainer');
                list.innerHTML = '';
                
                if (data.apps.length === 0) {
                    list.innerHTML = '<div class="text-xs text-slate-500 text-center py-2">No apps imported yet.</div>';
                    return;
                }

                data.apps.forEach(app => {
                    const row = document.createElement('div');
                    row.className = `flex items-center justify-between p-3 rounded-xl border text-xs transition-colors ${
                        app.is_active ? 'bg-indigo-650/15 border-indigo-500/30' : 'bg-slate-900 border-white/5 hover:border-white/10'
                    }`;
                    
                    row.innerHTML = `
                        <div class="flex items-center space-x-2 min-w-0">
                            <i class="fa-solid ${app.name.includes('Local') ? 'fa-house-laptop text-indigo-400' : 'fa-mobile-screen-button text-cyan-400'} text-xs"></i>
                            <span class="font-bold truncate text-[11px]">${app.name}</span>
                        </div>
                        <button onclick="selectApp('${app.path}')" class="px-3 py-1.5 rounded-lg text-[10px] font-bold shadow-md transition-all active:scale-95 ${
                            app.is_active ? 'bg-emerald-600 text-white cursor-default' : 'bg-slate-800 hover:bg-slate-700 text-slate-300'
                        }" ${app.is_active ? 'disabled' : ''}>
                            ${app.is_active ? 'Active' : 'Launch'}
                        </button>
                    `;
                    list.appendChild(row);
                });
            } catch (e) {
                console.error("Failed to load apps", e);
            }
        }

        async function cloneApp() {
            const urlInput = document.getElementById('repoUrlInput');
            const repoUrl = urlInput.value.trim();
            if (!repoUrl) {
                showToast("Please enter a valid git repo URL", "error");
                return;
            }
            
            showToast("Cloning repository from GitHub...", "info");
            urlInput.value = '';
            
            try {
                const res = await fetch('/api/apps/clone', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ repo_url: repoUrl })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    showToast(data.message, 'success');
                    fetchApps();
                } else {
                    showToast(data.message, 'error');
                }
            } catch (e) {
                showToast("Cloning command failed", "error");
            }
        }

        async function selectApp(path) {
            showToast("Activating selected app...", "info");
            try {
                const res = await fetch('/api/apps/select', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: path })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    showToast(data.message, 'success');
                    fetchApps();
                    fetchStatus();
                    loadSettings();
                } else {
                    showToast(data.message, 'error');
                }
            } catch (e) {
                showToast("Failed to switch app", "error");
            }
        }

        // Settings Handling
        async function loadSettings() {
            try {
                const res = await fetch('/api/settings');
                const data = await res.json();
                
                settingsSchema = data.schema || [];
                const form = document.getElementById('dynamicSettingsForm');
                form.innerHTML = '';
                
                if (settingsSchema.length === 0) {
                    form.innerHTML = '<div class="text-xs text-slate-500 text-center py-4">No configuration variables needed.</div>';
                    return;
                }

                settingsSchema.forEach(field => {
                    const div = document.createElement('div');
                    div.className = "space-y-1";
                    
                    const isSecret = field.secret;
                    const inputType = isSecret ? "password" : "text";
                    
                    div.innerHTML = `
                        <label class="text-[9px] font-bold text-slate-400 uppercase tracking-wider block">${field.key}</label>
                        <div class="relative flex items-center">
                            <input type="${inputType}" id="env_${field.key}" value="${field.value}" placeholder="${field.description}" class="w-full bg-black/20 border border-white/5 rounded-xl px-3 py-2 text-xs focus:outline-none focus:border-indigo-500/50 font-mono text-slate-300 pr-8" />
                            ${isSecret ? `
                                <button type="button" onclick="togglePasswordVisibility('env_${field.key}')" class="absolute right-3 text-slate-500 hover:text-slate-300">
                                    <i class="fa-solid fa-eye text-[10px]"></i>
                                </button>
                            ` : ''}
                        </div>
                    `;
                    form.appendChild(div);
                });

                const saveBtn = document.createElement('button');
                saveBtn.className = "w-full py-2.5 bg-indigo-650 hover:bg-indigo-600 border border-indigo-500/20 text-white rounded-xl text-xs font-bold transition-all shadow-md active:scale-98 mt-2";
                saveBtn.onclick = saveSettings;
                saveBtn.textContent = "Save Config Settings";
                form.appendChild(saveBtn);
                
            } catch (e) {
                console.error("Failed to load settings", e);
            }
        }

        function togglePasswordVisibility(id) {
            const el = document.getElementById(id);
            if (el.type === "password") {
                el.type = "text";
            } else {
                el.type = "password";
            }
        }

        async function saveSettings() {
            const payload = {};
            settingsSchema.forEach(field => {
                payload[field.key] = document.getElementById(`env_${field.key}`).value.trim();
            });
            
            showToast("Saving environment settings...", "info");
            try {
                const res = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (data.status === 'success') {
                    showToast(data.message, 'success');
                    loadSettings();
                } else {
                    showToast(data.message, 'error');
                }
            } catch (e) {
                showToast("Failed to save settings", "error");
            }
        }

        async function runDiagnostics() {
            showToast("Running system diagnostics tests...", "info");
            try {
                const res = await fetch('/api/diagnose', { method: 'POST' });
                const data = await res.json();
                if (data.status === 'success') {
                    showToast(data.message, 'success');
                } else {
                    showToast(data.message, 'error');
                }
            } catch (e) {
                showToast("Failed to execute diagnostic check", "error");
            }
        }

        // Services Operations
        async function toggleService(name, isRunning) {
            const action = isRunning ? 'stop' : 'start';
            try {
                const res = await fetch(`/api/${action}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ service: name })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    showToast(data.message, 'success');
                    fetchStatus();
                } else {
                    showToast(data.message, 'error');
                }
            } catch (e) {
                showToast("Failed to communicate with service.", 'error');
            }
        }

        // Setup triggers
        async function triggerSetup(action) {
            showToast("Starting pipeline task...", "info");
            try {
                const res = await fetch(`/api/${action}`, { method: 'POST' });
                const data = await res.json();
                if (data.status === 'success') {
                    showToast(data.message, 'success');
                    fetchStatus();
                } else {
                    showToast(data.message, 'error');
                }
            } catch (e) {
                showToast("Command request failed.", 'error');
            }
        }

        // Fetch updates
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                document.getElementById('projectPathDisplay').textContent = data.project_dir;
                currentPath = data.project_dir;

                // Handle task progress state
                const taskState = data.task_state || { running: false, name: null };
                const bannerText = document.getElementById('pipelineStatusText');
                const bannerIcon = document.getElementById('pipelineStatusIcon');
                const bannerSpinner = document.getElementById('pipelineSpinner');
                const logBadge = document.getElementById('logTaskBadge');
                const logBadgeText = document.getElementById('logTaskBadgeText');
                const btnPrereqs = document.getElementById('btn_prereqs');
                const btnDeps = document.getElementById('btn_deps');

                if (taskState.running) {
                    bannerText.textContent = `Processing: ${taskState.name || 'Running Background Task'}...`;
                    bannerText.className = "text-indigo-400 font-bold animate-pulse";
                    bannerIcon.className = "w-2 h-2 rounded-full bg-indigo-500 animate-ping";
                    bannerSpinner.classList.remove('hidden');
                    
                    logBadge.classList.remove('hidden');
                    logBadgeText.textContent = (taskState.name || 'PROCESSING').toUpperCase();

                    btnPrereqs.classList.add('opacity-50', 'pointer-events-none');
                    btnDeps.classList.add('opacity-50', 'pointer-events-none');
                } else {
                    bannerText.textContent = "Pipeline Idle / Ready";
                    bannerText.className = "text-slate-400 font-normal";
                    bannerIcon.className = "w-2 h-2 rounded-full bg-emerald-500";
                    bannerSpinner.classList.add('hidden');
                    
                    logBadge.classList.add('hidden');

                    btnPrereqs.classList.remove('opacity-50', 'pointer-events-none');
                    btnDeps.classList.remove('opacity-50', 'pointer-events-none');
                }

                const grid = document.getElementById('servicesGrid');
                grid.innerHTML = '';
                
                const services = Object.keys(data.services);
                if (services.length === 0) {
                    grid.innerHTML = '<div class="text-xs text-slate-500 text-center py-4 bg-white/5 border border-white/5 rounded-xl">No services detected in this app context.</div>';
                    return;
                }

                services.forEach(name => {
                    const svc = data.services[name];
                    const isRunning = svc.status === 'RUNNING';
                    const isBusy = svc.status === 'BUSY (EXTERNAL)';
                    
                    let dotColor = 'bg-rose-500';
                    let statusLabel = 'Stopped';
                    let statusClass = 'text-rose-400 bg-rose-500/10 border-rose-500/20';
                    
                    if (isRunning) {
                        dotColor = 'bg-emerald-500';
                        statusLabel = 'Running';
                        statusClass = 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20';
                    } else if (isBusy) {
                        dotColor = 'bg-amber-500';
                        statusLabel = 'Busy (Port Occupied)';
                        statusClass = 'text-amber-400 bg-amber-500/10 border-amber-500/20';
                    }

                    const card = document.createElement('div');
                    card.className = `p-4 rounded-2xl border transition-all ${
                        isRunning ? 'bg-indigo-950/10 border-indigo-500/20 shadow-lg shadow-indigo-500/5' : 'bg-white/5 border-white/10'
                    }`;

                    const hostname = window.location.hostname;
                    const url = name === 'Backend' ? `http://${hostname}:${svc.port}/docs` : `http://${hostname}:${svc.port}`;

                    card.innerHTML = `
                        <div class="flex items-center justify-between">
                            <div class="flex items-center space-x-2.5">
                                <span class="w-2.5 h-2.5 rounded-full ${dotColor} ${isRunning ? 'animate-pulse' : ''}"></span>
                                <div>
                                    <h3 class="text-sm font-bold font-mono tracking-wide">${name}</h3>
                                    <span class="inline-block mt-0.5 px-2 py-0.5 border rounded-md text-[9px] font-bold ${statusClass}">
                                        ${statusLabel} ${svc.port ? `(:${svc.port})` : ''}
                                    </span>
                                </div>
                            </div>
                            <div class="flex items-center space-x-2">
                                ${isRunning || isBusy ? `
                                    <a href="${url}" target="_blank" class="w-8 h-8 rounded-lg bg-indigo-600/15 border border-indigo-500/30 flex items-center justify-center text-indigo-400 hover:text-white hover:bg-indigo-600 transition-colors">
                                        <i class="fa-solid fa-arrow-up-right-from-square text-xs"></i>
                                    </a>
                                ` : ''}
                                <button onclick="toggleService('${name}', ${isRunning})" class="px-3.5 py-1.5 rounded-lg text-xs font-bold shadow-md transition-all active:scale-95 ${
                                    isRunning ? 'bg-rose-600 hover:bg-rose-500 text-white' : 'bg-indigo-600 hover:bg-indigo-500 text-white'
                                }">
                                    ${isRunning ? 'Stop' : 'Start'}
                                </button>
                            </div>
                        </div>
                    `;
                    grid.appendChild(card);
                });
            } catch (e) {
                console.error("Status fetch failed", e);
            }
        }

        async function fetchLogs() {
            try {
                const res = await fetch('/api/logs');
                const data = await res.json();
                
                if (data.logs && data.logs.length > logsLength) {
                    const terminal = document.getElementById('logsTerminal');
                    const addedLogs = data.logs.slice(logsLength);
                    
                    addedLogs.forEach(log => {
                        const row = document.createElement('div');
                        let streamColor = 'text-indigo-400';
                        if (log.stream === 'error') streamColor = 'text-rose-400';
                        if (log.stream === 'backend') streamColor = 'text-cyan-400';
                        if (log.stream === 'ai_engine') streamColor = 'text-purple-400';
                        if (log.stream === 'frontend') streamColor = 'text-emerald-400';

                        row.innerHTML = `<span class="text-slate-500">[${log.time}]</span> <span class="font-bold ${streamColor}">${log.stream.toUpperCase()}:</span> <span class="text-slate-300 font-mono">${log.text}</span>`;
                        terminal.appendChild(row);
                    });

                    logsLength = data.logs.length;
                    terminal.scrollTop = terminal.scrollHeight;
                }
            } catch (e) {
                console.error("Logs fetch failed", e);
            }
        }

        function clearUIStatusLogs() {
            document.getElementById('logsTerminal').innerHTML = '<div class="text-indigo-400/80">[SYSTEM] Terminal logs cleared.</div>';
            logsLength = 0;
        }

        // Init
        fetchStatus();
        fetchApps();
        loadSettings();
        setInterval(fetchStatus, 2000);
        setInterval(fetchApps, 6000);
        setInterval(fetchLogs, 1000);
    </script>
</body>
</html>
"""

def get_ip_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

def start_server():
    socketserver.TCPServer.allow_reuse_address = True
    server_port = 8080
    while True:
        try:
            handler = WebLauncherHandler
            server = socketserver.TCPServer(("0.0.0.0", server_port), handler)
            local_ip = get_ip_address()
            
            print("="*60, flush=True)
            print(" BYO-AGENT TERMUX WEB LAUNCHER ACTIVE", flush=True)
            print("="*60, flush=True)
            print(f" Local Loopback Address:  http://localhost:{server_port}", flush=True)
            print(f" Mobile Network Link:    http://{local_ip}:{server_port}", flush=True)
            print("="*60, flush=True)
            print(" Press Ctrl+C to shutdown.", flush=True)
            print("="*60, flush=True)
            
            server.serve_forever()
            break
        except OSError as e:
            if server_port < 8100:
                print(f"[!] Port {server_port} in use/busy, trying {server_port + 1}...", flush=True)
                server_port += 1
            else:
                print(f"[!] Startup Error: {str(e)}", flush=True)
                break

if __name__ == "__main__":
    try:
        start_server()
    except KeyboardInterrupt:
        print("\n[!] Shutting down launcher web controller server. Clearing running processes...")
        for name in list(manager.processes.keys()):
            manager.stop_service(name)
        sys.exit(0)
