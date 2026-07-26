# AstroDash Generic Termux Launcher (BYO-Agent)

A zero-dependency, mobile-first Web Dashboard Launcher designed to host, configure, and execute **any** Python + Node.js agentic application locally on Android via Termux.

## Features
- **App Library**: Import or clone multiple agentic apps directly from GitHub.
- **Stack Heuristics & Auto-Detection**: Automatically detects Backend (FastAPI, Flask, Express), Frontend (Next.js, Vite/React, Vue), and AI engines, configures ports, and builds the launch scripts.
- **Optional Manifest (`launcher.json`)**: Developers can configure a JSON file in their repository root to customize directory paths, runtime settings, starting commands, and required keys.
- **Dynamic Configuration Form**: Scans for `.env.example` files across directories and automatically generates a custom web form inside the browser to input and save your API keys.
- **Port Hotlinks**: Renders clickable buttons mapping active ports directly to URLs for easy opening in your mobile browser.
- **One-click Pipeline**: Script automation buttons for both `pkg install` (installs compiling tools like `clang`, `make`, `rust`, etc. required for Termux ARM64) and `pip/npm install`.
- **Log Stream Console**: Real-time log capture pane showing stdout logs of active services.
- **Self-Diagnostics**: Click **Check Health** to test python libraries, node_modules, and free ports.

## Installation in Termux

1. **Open Termux** on your Android phone.
2. **Download the launcher script**:
   ```bash
   curl -O https://raw.githubusercontent.com/letentvish/termuxlauncher/main/termux_launcher.py
   ```
3. **Run the launcher**:
   ```bash
   python termux_launcher.py
   ```

## Usage

1. Open your mobile browser and navigate to:
   * **`http://localhost:8080`**
2. In the **App Library** panel, paste a GitHub URL and click **Import**.
3. Tap **Launch** next to the imported app to switch your active context.
4. Fill in the dynamically generated inputs in the **App Configurations** section and click **Save Config Settings**.
5. Go to **Installation Pipeline** and run **1. System Pkgs**, then **2. App Modules**.
6. Start your ports under **Active Port Channels** and tap the link icon to open the app!
