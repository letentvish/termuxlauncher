# AstroDash Termux Launcher

A zero-dependency, mobile-friendly Web Dashboard Launcher designed to run and manage **AstroDash** agentic applications locally on Android via Termux.

## Features
- **App Library**: Import or clone multiple AstroDash variants (such as `AstroDashlinux`) directly from GitHub.
- **Git Cloning Panel**: Paste any repository URL and clone it with live terminal feedback logs.
- **Port Hotlinks**: Renders clickable buttons mapping active ports (Frontend on `3000`, API on `8000`, AI Engine on `8010`) to direct URLs so you can launch them in your browser.
- **One-click Pipeline**: Script automation buttons for both `pkg install` (Termux system requirements) and `pip/npm install` (app-specific requirements).
- **Log Stream Console**: Real-time console logger showing service output (FastAPI, Next.js, and npm install).

## Installation in Termux

1. **Open Termux** on your Android phone.
2. **Download the launcher script** directly:
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
2. In the **App Library** panel, paste a GitHub URL (e.g. `https://github.com/letentvish/AstroDashlinux`) and click **Import**.
3. Once cloned, it will appear under the library list. Click **Launch** next to it to set it as the active context.
4. Go to **Installation Pipeline** and run **1. System Pkgs**, then **2. App Modules**.
5. Start your ports under **Active Port Channels** and tap the link icon to open the app!
