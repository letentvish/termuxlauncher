# AstroDash Termux Launcher

A zero-dependency, mobile-friendly Web Dashboard Launcher designed to run the **AstroDash** agentic application locally on Android via Termux.

## Installation in Termux

1. **Open Termux** on your Android phone.
2. **Download the launcher script** directly using `curl` or `wget`:
   ```bash
   curl -O https://raw.githubusercontent.com/letentvish/termuxlauncher/main/termux_launcher.py
   ```
3. **Run the launcher**:
   ```bash
   python termux_launcher.py
   ```

## Usage

1. Once started, open your mobile browser and navigate to:
   * **`http://localhost:8080`**
2. Use the **Directory Browser** in the Web UI to select your `AstroDash_OpenSource` folder.
3. Click **1. System Pkgs** to install python, nodejs, and git.
4. Click **2. App Modules** to install python dependencies and node_modules.
5. Under **Active Port Channels**, toggle the services to **Start**.
6. When a service status indicator turns **Green (Running)**, tap the launch icon to open it!
