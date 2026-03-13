# ⚡ FTPForge — High-Performance FTP Manager

A modern, dark-themed desktop FTP client built with Python and PyQt6.

## Features

- **Two-pane layout** — Local filesystem (left) + Remote FTP server (right)
- **Dark mode UI** — Sleek, professional interface
- **Threaded transfers** — UI never freezes during uploads/downloads
- **Transfer queue** — Multiple files queued and processed in order
- **Real-time speed indicator** — Live transfer speed in the toolbar
- **Breadcrumb navigation** — Click any path segment to navigate
- **Context menus** — Right-click for upload, download, rename, delete
- **Recent connections** — Saved to `~/.ftpforge_config.json`
- **Activity log** — Timestamped log of all operations

## Installation

```bash
pip install PyQt6
python ftp_manager.py
```

## Usage

1. Click **Connect to Server** (or press `Ctrl+K`)
2. Enter your FTP host, port (default 21), username, and password
3. Click **⚡ Connect**
4. Browse remote files in the right panel
5. Double-click folders to navigate, double-click files to download
6. Right-click for context menu operations

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+K` | Open connection dialog |
| `F5` | Refresh remote directory |

## Architecture

- `FTPWorker` — Thread-safe FTP operations using `ftplib`
- `FTPThread` — Dedicated Qt thread for the worker
- `RemoteFileModel` — Qt table model for remote file display
- `TransferQueueModel` — Qt table model for transfer queue
- `MainWindow` — Main application window and signal coordinator

## Requirements

- Python 3.9+
- PyQt6 6.4+
- No additional FTP libraries needed (uses stdlib `ftplib`)
