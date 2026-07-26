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

class ProcessManager:
    def __init__(self):
        self.processes = {
            "Frontend": None,
            "Backend": None,
            "AI Engine": None
        }
        self.logs = []
        self.lock = threading.Lock()
        self.project_dir = os.path.dirname(os.path.abspath(__file__))
        self.log_limit = 1000

    def add_log(self, text, stream="system"):
        with self.lock:
            self.logs.append({"time": os.popen("date +%T").read().strip(), "stream": stream, "text": text})
            if len(self.logs) > self.log_limit:
                self.logs.pop(0)

    def is_port_in_use(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            return s.connect_ex(('127.0.0.1', port)) == 0

    def get_status(self, name):
        port = PORTS.get(name)
        if not port:
            return "UNKNOWN"
        
        in_use = self.is_port_in_use(port)
        with self.lock:
            proc = self.processes.get(name)
        
        if proc and proc.poll() is None:
            return "RUNNING"
        elif in_use:
            return "BUSY (EXTERNAL)"
        return "STOPPED"

    def run_command(self, cmd, cwd, name=None):
        def worker():
            try:
                self.add_log(f"Starting process: {' '.join(cmd)} in {cwd}", "system")
                use_shell = (os.name == 'nt')
                
                # Check for virtual environment inside backend or ai_engine
                cmd_to_run = list(cmd)
                if name in ["Backend", "AI Engine"] and cmd_to_run[0] == "python":
                    # Check for local venv
                    venv_py = os.path.join(cwd, "venv", "bin", "python")
                    if not os.path.exists(venv_py):
                        venv_py = os.path.join(cwd, ".venv", "bin", "python")
                    if not os.path.exists(venv_py) and os.name == 'nt':
                        venv_py = os.path.join(cwd, "venv", "Scripts", "python.exe")
                    if os.path.exists(venv_py):
                        cmd_to_run[0] = venv_py
                        self.add_log(f"Using virtual environment python: {venv_py}", "system")

                process = subprocess.Popen(
                    cmd_to_run,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    shell=use_shell
                )
                
                if name:
                    with self.lock:
                        self.processes[name] = process
                
                # Stream logs
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

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def start_service(self, name):
        status = self.get_status(name)
        if status == "RUNNING":
            return {"status": "success", "message": f"{name} is already running."}
        
        # Build path structure
        cwd = os.path.join(self.project_dir, name.lower().replace(" ", "_"))
        if name == "Frontend":
            cmd = ["npm", "run", "dev"]
        elif name == "Backend":
            # Bind to 0.0.0.0 for LAN/mobile testing
            cmd = ["python", "-m", "uvicorn", "main:app", "--port", "8000", "--host", "0.0.0.0"]
        elif name == "AI Engine":
            cmd = ["python", "-m", "uvicorn", "main:app", "--port", "8010", "--host", "0.0.0.0"]
        else:
            return {"status": "error", "message": "Unknown service"}

        self.run_command(cmd, cwd, name)
        return {"status": "success", "message": f"Started {name}"}

    def stop_service(self, name):
        with self.lock:
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
            
            with self.lock:
                self.processes[name] = None
            return {"status": "success", "message": f"Stopped {name}"}
        return {"status": "error", "message": f"{name} was not running."}

    def install_prereqs(self):
        # Determine packages based on termux environment
        is_termux = os.path.exists("/data/data/com.termux")
        if not is_termux:
            return {"status": "error", "message": "This operation is optimized for Termux. Please install python, git, and nodejs manually on your system."}
        
        cmd = ["pkg", "install", "-y", "nodejs", "python", "git", "tur-repo"]
        self.run_command(cmd, self.project_dir)
        return {"status": "success", "message": "Triggered Termux packages installation."}

    def install_deps(self):
        def worker():
            use_shell = (os.name == 'nt')
            # Install Backend packages
            be_dir = os.path.join(self.project_dir, "backend")
            if os.path.exists(be_dir):
                self.add_log("Installing backend requirements...", "system")
                proc = subprocess.Popen(["pip", "install", "-r", "requirements.txt"], cwd=be_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=use_shell)
                for line in iter(proc.stdout.readline, ''): self.add_log(line.strip(), "setup")
                proc.wait()

            # Install AI Engine packages
            ai_dir = os.path.join(self.project_dir, "ai_engine")
            if os.path.exists(ai_dir):
                self.add_log("Installing AI Engine requirements...", "system")
                proc = subprocess.Popen(["pip", "install", "-r", "requirements.txt"], cwd=ai_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=use_shell)
                for line in iter(proc.stdout.readline, ''): self.add_log(line.strip(), "setup")
                proc.wait()

            # Install Frontend packages
            fe_dir = os.path.join(self.project_dir, "frontend")
            if os.path.exists(fe_dir):
                self.add_log("Installing Frontend dependencies (npm install)...", "system")
                proc = subprocess.Popen(["npm", "install"], cwd=fe_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=use_shell)
                for line in iter(proc.stdout.readline, ''): self.add_log(line.strip(), "setup")
                proc.wait()
            
            self.add_log("All dependencies installation processes complete.", "system")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return {"status": "success", "message": "Triggered pip & npm installation scripts."}

# Initialize global process manager
manager = ProcessManager()

class WebLauncherHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass # Suppress command line logging of requests to keep console clean

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
            data = {
                "project_dir": manager.project_dir,
                "services": {
                    name: {
                        "status": manager.get_status(name),
                        "port": PORTS[name]
                    } for name in PORTS
                }
            }
            self.send_json(data)
            return

        elif path == "/api/logs":
            with manager.lock:
                logs_copy = list(manager.logs)
            self.send_json({"logs": logs_copy})
            return

        elif path == "/api/browse":
            target_path = query.get('path', [os.path.expanduser("~")])[0]
            try:
                if not os.path.exists(target_path):
                    target_path = os.path.expanduser("~")
                
                items = []
                # Add parent directory
                parent = os.path.dirname(target_path)
                if parent != target_path:
                    items.append({"name": "..", "path": parent, "is_dir": True})

                for f in sorted(os.listdir(target_path)):
                    full_p = os.path.join(target_path, f)
                    # Skip hidden items except in venv
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

        # Serve static assets or fall back
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
                manager.project_dir = os.path.abspath(folder)
                manager.add_log(f"Project root folder changed to: {manager.project_dir}", "system")
                self.send_json({"status": "success", "message": f"Folder selected: {manager.project_dir}"})
            else:
                self.send_json({"status": "error", "message": "Invalid directory folder path."}, status=400)
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
                <p class="text-[10px] text-indigo-400 font-semibold tracking-wider uppercase">Termux Controller</p>
            </div>
        </div>
        <div class="flex items-center space-x-2">
            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                <span class="w-1.5 h-1.5 mr-1 rounded-full bg-emerald-500 animate-pulse"></span>
                Active
            </span>
        </div>
    </header>

    <!-- Main Container -->
    <main class="max-w-md mx-auto px-4 mt-6 space-y-6">

        <!-- 1. Folder Selection Card -->
        <section class="bg-white/5 border border-white/10 rounded-2xl p-5 shadow-xl backdrop-blur-sm space-y-4">
            <div class="flex items-center justify-between">
                <h2 class="text-sm font-bold tracking-wide text-indigo-300 uppercase flex items-center gap-1.5">
                    <i class="fa-solid fa-folder-open text-xs"></i> Project Directory
                </h2>
                <button onclick="toggleFolderModal(true)" class="text-xs px-2.5 py-1 bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg text-slate-300 transition-colors">
                    <i class="fa-solid fa-search mr-1"></i> Browse
                </button>
            </div>
            <div class="p-3 bg-black/30 border border-white/5 rounded-xl flex items-center justify-between text-xs overflow-hidden">
                <span id="projectPathDisplay" class="font-mono truncate select-all mr-2">Loading...</span>
                <i class="fa-solid fa-circle-check text-emerald-500 text-xs shrink-0" id="dirCheck"></i>
            </div>
        </section>

        <!-- 2. Services Management -->
        <section class="space-y-3">
            <h2 class="text-xs font-bold tracking-widest text-slate-400 uppercase px-1">Active Port Channels</h2>
            
            <div class="grid gap-3" id="servicesGrid">
                <!-- Template service cards generated via JS -->
            </div>
        </section>

        <!-- 3. Setup Commands Pipeline -->
        <section class="bg-white/5 border border-white/10 rounded-2xl p-5 shadow-xl backdrop-blur-sm space-y-4">
            <h2 class="text-sm font-bold tracking-wide text-purple-300 uppercase flex items-center gap-1.5">
                <i class="fa-solid fa-gears text-xs"></i> Installation Pipeline
            </h2>
            <p class="text-xs text-slate-400 leading-relaxed">
                If configuring the application for the first time in Termux, execute the stages sequentially to register system dependencies.
            </p>
            <div class="grid grid-cols-2 gap-3 pt-1">
                <button onclick="triggerSetup('install_prereqs')" class="p-3 bg-slate-900 border border-white/15 hover:border-indigo-500/50 rounded-xl flex flex-col items-center justify-center text-center group transition-all active:scale-95">
                    <i class="fa-solid fa-cubes text-indigo-400 text-base mb-2 group-hover:scale-110 transition-transform"></i>
                    <span class="text-xs font-bold">1. System Pkgs</span>
                    <span class="text-[9px] text-slate-500 mt-0.5">Node, Python, Git</span>
                </button>
                <button onclick="triggerSetup('install_deps')" class="p-3 bg-slate-900 border border-white/15 hover:border-purple-500/50 rounded-xl flex flex-col items-center justify-center text-center group transition-all active:scale-95">
                    <i class="fa-solid fa-code-branch text-purple-400 text-base mb-2 group-hover:scale-110 transition-transform"></i>
                    <span class="text-xs font-bold">2. App Modules</span>
                    <span class="text-[9px] text-slate-500 mt-0.5">pip & npm scripts</span>
                </button>
            </div>
        </section>

        <!-- 4. Terminal Log Output -->
        <section class="bg-black/40 border border-white/15 rounded-2xl p-5 shadow-2xl space-y-4">
            <div class="flex items-center justify-between">
                <h2 class="text-sm font-bold tracking-wide text-amber-300 uppercase flex items-center gap-1.5">
                    <i class="fa-solid fa-terminal text-xs"></i> System Stream Logs
                </h2>
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
                } else {
                    showToast(data.message, 'error');
                }
            } catch (e) {
                showToast("Request failed", 'error');
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
            showToast("Starting pipeline action...", "info");
            try {
                const res = await fetch(`/api/${action}`, { method: 'POST' });
                const data = await res.json();
                if (data.status === 'success') {
                    showToast("Pipeline task successfully started.", 'success');
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

                const grid = document.getElementById('servicesGrid');
                grid.innerHTML = '';

                Object.keys(data.services).forEach(name => {
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

                    // Auto-resolve hostname for links to make opening apps in mobile convenient
                    const hostname = window.location.hostname;
                    const url = name === 'Backend' ? `http://${hostname}:${svc.port}/docs` : `http://${hostname}:${svc.port}`;

                    card.innerHTML = `
                        <div class="flex items-center justify-between">
                            <div class="flex items-center space-x-2.5">
                                <span class="w-2.5 h-2.5 rounded-full ${dotColor} ${isRunning ? 'animate-pulse' : ''}"></span>
                                <div>
                                    <h3 class="text-sm font-bold font-mono tracking-wide">${name}</h3>
                                    <span class="inline-block mt-0.5 px-2 py-0.5 border rounded-md text-[9px] font-bold ${statusClass}">
                                        ${statusLabel}
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
                    // Auto scroll
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
        setInterval(fetchStatus, 3000);
        setInterval(fetchLogs, 1500);
    </script>
</body>
</html>
"""

def get_ip_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Doesn't need to connect, just resolves local route
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

def start_server():
    server_port = 8080
    while True:
        try:
            handler = WebLauncherHandler
            server = socketserver.TCPServer(("0.0.0.0", server_port), handler)
            local_ip = get_ip_address()
            
            print("="*60)
            print(" ASTRODASH TERMUX WEB LAUNCHER ACTIVE")
            print("="*60)
            print(f" Local Loopback Address:  http://localhost:{server_port}")
            print(f" Mobile Network Link:    http://{local_ip}:{server_port}")
            print("="*60)
            print(" Press Ctrl+C to shutdown.")
            print("="*60)
            
            # Start background monitoring loops if needed
            server.serve_forever()
            break
        except OSError as e:
            if e.errno == 98: # Port occupied
                server_port += 1
            else:
                print(f"[!] Startup Error: {str(e)}")
                break

if __name__ == "__main__":
    try:
        start_server()
    except KeyboardInterrupt:
        print("\n[!] Shutting down launcher web controller server. Clearing running processes...")
        # Kill all running subprocesses
        for name in PORTS:
            manager.stop_service(name)
        sys.exit(0)
