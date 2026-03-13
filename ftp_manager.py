#!/usr/bin/env python3
"""
FTPForge - High-Performance FTP Management Desktop Application
A modern, fast, and intuitive file explorer for FTP server management.
"""

import sys
import os
import ftplib
import threading
import queue
import time
import json
import shutil
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from dataclasses import dataclass, field
from enum import Enum, auto

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTreeView, QListView, QSplitter,
    QFrame, QProgressBar, QStatusBar, QMenuBar, QMenu, QToolBar,
    QAbstractItemView, QHeaderView, QDialog,
    QDialogButtonBox, QFormLayout, QComboBox, QCheckBox, QScrollArea,
    QTabWidget, QTextEdit, QSizePolicy, QMessageBox, QInputDialog,
    QFileDialog, QGridLayout, QStackedWidget, QSpinBox, QListWidget,
    QListWidgetItem, QTreeWidget, QTreeWidgetItem, QTableWidget,
    QTableWidgetItem, QGroupBox
)

# QFileSystemModel moved to QtGui in PyQt6 6.4+, with fallback for older versions
try:
    from PyQt6.QtGui import QFileSystemModel
except ImportError:
    from PyQt6.QtWidgets import QFileSystemModel
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize, QModelIndex, QMimeData,
    QFileInfo, QDir, QSortFilterProxyModel, QAbstractTableModel,
    QVariant, pyqtSlot, QPoint, QRect, QUrl, QObject, QRunnable,
    QThreadPool, QMutex, QMutexLocker
)
from PyQt6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QFontDatabase, QPalette,
    QDragEnterEvent, QDropEvent, QDrag, QCursor, QAction, QKeySequence,
    QStandardItemModel, QStandardItem, QBrush, QPen, QLinearGradient,
    QRadialGradient, QTextCursor
)


# ─────────────────────────────────────────────
#  DATA MODELS
# ─────────────────────────────────────────────

class TransferType(Enum):
    UPLOAD = auto()
    DOWNLOAD = auto()

class TransferStatus(Enum):
    QUEUED = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()

@dataclass
class FTPEntry:
    """Represents a file or directory on the FTP server."""
    name: str
    size: int = 0
    modified: str = ""
    is_dir: bool = False
    permissions: str = ""
    path: str = ""

@dataclass
class TransferTask:
    """Represents a file transfer task."""
    id: int
    transfer_type: TransferType
    local_path: str
    remote_path: str
    filename: str
    total_size: int = 0
    transferred: int = 0
    status: TransferStatus = TransferStatus.QUEUED
    speed: float = 0.0
    error_msg: str = ""
    start_time: float = 0.0

@dataclass
class FTPConnection:
    """Stores FTP connection details."""
    host: str
    port: int
    username: str
    password: str
    name: str = ""

    def to_dict(self) -> dict:
        return {
            "host": self.host, "port": self.port,
            "username": self.username, "name": self.name or self.host
        }


# ─────────────────────────────────────────────
#  FTP WORKER (Thread-safe FTP operations)
# ─────────────────────────────────────────────

class FTPWorker(QObject):
    """Handles all FTP operations in a background thread."""

    # Signals
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    connection_error = pyqtSignal(str)
    directory_loaded = pyqtSignal(str, list)        # path, entries
    operation_complete = pyqtSignal(str, bool, str)  # op_name, success, message
    transfer_progress = pyqtSignal(int, int, float)  # task_id, bytes, speed
    transfer_complete = pyqtSignal(int, bool, str)   # task_id, success, msg
    log_message = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._ftp: Optional[ftplib.FTP] = None
        self._mutex = QMutex()
        self._connected = False
        self._transfer_queue: queue.Queue = queue.Queue()
        self._transfer_thread: Optional[threading.Thread] = None
        self._stop_transfer = False
        self._task_counter = 0

    # ── Connection ──────────────────────────────

    def connect(self, conn: FTPConnection):
        """Connect to FTP server in a thread-safe manner."""
        try:
            self.log_message.emit(f"Connecting to {conn.host}:{conn.port}...")
            ftp = ftplib.FTP()
            ftp.connect(conn.host, conn.port, timeout=30)
            ftp.login(conn.username, conn.password)
            ftp.set_pasv(True)

            with QMutexLocker(self._mutex):
                self._ftp = ftp
                self._connected = True

            welcome = ftp.getwelcome()
            self.log_message.emit(f"Connected: {welcome}")
            self.connected.emit()

            # Start transfer queue processor
            self._stop_transfer = False
            self._transfer_thread = threading.Thread(
                target=self._process_transfer_queue, daemon=True
            )
            self._transfer_thread.start()

        except ftplib.all_errors as e:
            self.connection_error.emit(str(e))
        except Exception as e:
            self.connection_error.emit(f"Unexpected error: {e}")

    def disconnect(self):
        """Gracefully disconnect from FTP server."""
        self._stop_transfer = True
        with QMutexLocker(self._mutex):
            if self._ftp:
                try:
                    self._ftp.quit()
                except Exception:
                    pass
                finally:
                    self._ftp = None
                    self._connected = False
        self.disconnected.emit()
        self.log_message.emit("Disconnected from server.")

    def is_connected(self) -> bool:
        with QMutexLocker(self._mutex):
            return self._connected and self._ftp is not None

    # ── Directory Operations ─────────────────────

    def list_directory(self, path: str = "."):
        """List directory contents and emit results."""
        with QMutexLocker(self._mutex):
            if not self._ftp:
                return
            ftp = self._ftp

        try:
            entries: List[FTPEntry] = []
            raw_lines: List[str] = []

            try:
                ftp.retrlines(f"LIST {path}", raw_lines.append)
            except ftplib.error_perm:
                self.directory_loaded.emit(path, [])
                return

            for line in raw_lines:
                entry = self._parse_list_line(line, path)
                if entry and entry.name not in (".", ".."):
                    entries.append(entry)

            # Sort: directories first, then files
            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
            self.directory_loaded.emit(path, entries)

        except ftplib.all_errors as e:
            self.log_message.emit(f"Error listing {path}: {e}")
            self.directory_loaded.emit(path, [])

    def _parse_list_line(self, line: str, base_path: str) -> Optional[FTPEntry]:
        """Parse a LIST response line into an FTPEntry."""
        try:
            parts = line.split(None, 8)
            if len(parts) < 9:
                return None

            permissions = parts[0]
            size_str = parts[4]
            month = parts[5]
            day = parts[6]
            year_or_time = parts[7]
            name = parts[8]

            is_dir = permissions.startswith("d")
            try:
                size = int(size_str)
            except ValueError:
                size = 0

            modified = f"{month} {day} {year_or_time}"
            path = f"{base_path.rstrip('/')}/{name}" if base_path != "/" else f"/{name}"

            return FTPEntry(
                name=name, size=size, modified=modified,
                is_dir=is_dir, permissions=permissions, path=path
            )
        except Exception:
            return None

    def change_directory(self, path: str):
        """Change to a directory and list its contents."""
        with QMutexLocker(self._mutex):
            if not self._ftp:
                return
            ftp = self._ftp
        try:
            ftp.cwd(path)
            current = ftp.pwd()
            self.list_directory(current)
        except ftplib.all_errors as e:
            self.log_message.emit(f"Cannot change to {path}: {e}")

    def get_current_directory(self) -> str:
        with QMutexLocker(self._mutex):
            if not self._ftp:
                return "/"
            try:
                return self._ftp.pwd()
            except Exception:
                return "/"

    def create_directory(self, path: str):
        with QMutexLocker(self._mutex):
            if not self._ftp:
                return
            ftp = self._ftp
        try:
            ftp.mkd(path)
            self.operation_complete.emit("create_dir", True, f"Created: {path}")
            parent = str(Path(path).parent)
            self.list_directory(parent)
        except ftplib.all_errors as e:
            self.operation_complete.emit("create_dir", False, str(e))

    def delete_entry(self, path: str, is_dir: bool):
        with QMutexLocker(self._mutex):
            if not self._ftp:
                return
            ftp = self._ftp
        try:
            if is_dir:
                self._delete_directory_recursive(ftp, path)
            else:
                ftp.delete(path)
            self.operation_complete.emit("delete", True, f"Deleted: {path}")
            parent = str(Path(path).parent)
            self.list_directory(parent)
        except ftplib.all_errors as e:
            self.operation_complete.emit("delete", False, str(e))

    def _delete_directory_recursive(self, ftp: ftplib.FTP, path: str):
        """Recursively delete a remote directory."""
        raw_lines: List[str] = []
        ftp.retrlines(f"LIST {path}", raw_lines.append)
        for line in raw_lines:
            entry = self._parse_list_line(line, path)
            if entry and entry.name not in (".", ".."):
                if entry.is_dir:
                    self._delete_directory_recursive(ftp, entry.path)
                else:
                    ftp.delete(entry.path)
        ftp.rmd(path)

    def rename_entry(self, old_path: str, new_path: str):
        with QMutexLocker(self._mutex):
            if not self._ftp:
                return
            ftp = self._ftp
        try:
            ftp.rename(old_path, new_path)
            self.operation_complete.emit("rename", True, f"Renamed to: {new_path}")
            parent = str(Path(new_path).parent)
            self.list_directory(parent)
        except ftplib.all_errors as e:
            self.operation_complete.emit("rename", False, str(e))

    # ── Transfer Queue ───────────────────────────

    def queue_upload(self, local_path: str, remote_path: str) -> int:
        self._task_counter += 1
        task = TransferTask(
            id=self._task_counter,
            transfer_type=TransferType.UPLOAD,
            local_path=local_path,
            remote_path=remote_path,
            filename=os.path.basename(local_path),
            total_size=os.path.getsize(local_path) if os.path.isfile(local_path) else 0
        )
        self._transfer_queue.put(task)
        return task.id

    def queue_download(self, remote_path: str, local_path: str, size: int = 0) -> int:
        self._task_counter += 1
        task = TransferTask(
            id=self._task_counter,
            transfer_type=TransferType.DOWNLOAD,
            local_path=local_path,
            remote_path=remote_path,
            filename=os.path.basename(remote_path),
            total_size=size
        )
        self._transfer_queue.put(task)
        return task.id

    def _process_transfer_queue(self):
        """Background thread that processes transfer tasks one by one."""
        while not self._stop_transfer:
            try:
                task: TransferTask = self._transfer_queue.get(timeout=1)
                task.status = TransferStatus.IN_PROGRESS
                task.start_time = time.time()

                if task.transfer_type == TransferType.UPLOAD:
                    self._execute_upload(task)
                else:
                    self._execute_download(task)

                self._transfer_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                self.log_message.emit(f"Transfer queue error: {e}")

    def _execute_upload(self, task: TransferTask):
        """Upload a file to the FTP server."""
        with QMutexLocker(self._mutex):
            ftp = self._ftp

        if not ftp:
            self.transfer_complete.emit(task.id, False, "Not connected")
            return

        try:
            transferred = 0
            last_time = time.time()
            last_bytes = 0
            chunk_size = 32768  # 32KB chunks for good throughput

            with open(task.local_path, "rb") as f:
                def callback(data):
                    nonlocal transferred, last_time, last_bytes
                    transferred += len(data)
                    task.transferred = transferred

                    now = time.time()
                    elapsed = now - last_time
                    if elapsed >= 0.2:  # Update speed every 200ms
                        speed = (transferred - last_bytes) / elapsed
                        task.speed = speed
                        last_time = now
                        last_bytes = transferred
                        self.transfer_progress.emit(task.id, transferred, speed)

                ftp.storbinary(
                    f"STOR {task.remote_path}", f,
                    blocksize=chunk_size, callback=callback
                )

            task.status = TransferStatus.COMPLETED
            self.transfer_complete.emit(task.id, True, "Upload complete")
            # Refresh the remote directory
            parent = str(Path(task.remote_path).parent)
            self.list_directory(parent)

        except Exception as e:
            task.status = TransferStatus.FAILED
            task.error_msg = str(e)
            self.transfer_complete.emit(task.id, False, str(e))

    def _execute_download(self, task: TransferTask):
        """Download a file from the FTP server."""
        with QMutexLocker(self._mutex):
            ftp = self._ftp

        if not ftp:
            self.transfer_complete.emit(task.id, False, "Not connected")
            return

        try:
            os.makedirs(os.path.dirname(task.local_path), exist_ok=True)
            transferred = 0
            last_time = time.time()
            last_bytes = 0

            with open(task.local_path, "wb") as f:
                def callback(data):
                    nonlocal transferred, last_time, last_bytes
                    f.write(data)
                    transferred += len(data)
                    task.transferred = transferred

                    now = time.time()
                    elapsed = now - last_time
                    if elapsed >= 0.2:
                        speed = (transferred - last_bytes) / elapsed
                        task.speed = speed
                        last_time = now
                        last_bytes = transferred
                        self.transfer_progress.emit(task.id, transferred, speed)

                ftp.retrbinary(
                    f"RETR {task.remote_path}", callback, blocksize=32768
                )

            task.status = TransferStatus.COMPLETED
            self.transfer_complete.emit(task.id, True, "Download complete")

        except Exception as e:
            task.status = TransferStatus.FAILED
            task.error_msg = str(e)
            if os.path.exists(task.local_path):
                try:
                    os.remove(task.local_path)
                except Exception:
                    pass
            self.transfer_complete.emit(task.id, False, str(e))


# ─────────────────────────────────────────────
#  FTP WORKER THREAD
# ─────────────────────────────────────────────

class FTPThread(QThread):
    """Runs FTPWorker in a dedicated thread."""

    def __init__(self, worker: FTPWorker):
        super().__init__()
        self.worker = worker
        self.worker.moveToThread(self)

    def run(self):
        self.exec()


# ─────────────────────────────────────────────
#  REMOTE FILE MODEL
# ─────────────────────────────────────────────

class RemoteFileModel(QAbstractTableModel):
    """Table model for displaying remote FTP files."""

    COLUMNS = ["Name", "Size", "Modified", "Type", "Permissions"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: List[FTPEntry] = []

    def set_entries(self, entries: List[FTPEntry]):
        self.beginResetModel()
        self._entries = entries
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._entries)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._entries):
            return None

        entry = self._entries[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return entry.name
            elif col == 1:
                return self._format_size(entry.size) if not entry.is_dir else "<DIR>"
            elif col == 2:
                return entry.modified
            elif col == 3:
                return "Folder" if entry.is_dir else self._get_file_type(entry.name)
            elif col == 4:
                return entry.permissions

        elif role == Qt.ItemDataRole.DecorationRole and col == 0:
            return self._get_icon(entry)

        elif role == Qt.ItemDataRole.ForegroundRole:
            if entry.is_dir:
                return QBrush(QColor("#60A5FA"))  # Blue for folders
            return QBrush(QColor("#E2E8F0"))

        elif role == Qt.ItemDataRole.UserRole:
            return entry

        return None

    def get_entry(self, row: int) -> Optional[FTPEntry]:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def _format_size(self, size: int) -> str:
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"

    def _get_file_type(self, name: str) -> str:
        ext = Path(name).suffix.lower()
        type_map = {
            ".py": "Python Script", ".js": "JavaScript", ".ts": "TypeScript",
            ".html": "HTML", ".css": "CSS", ".json": "JSON", ".xml": "XML",
            ".txt": "Text", ".md": "Markdown", ".pdf": "PDF",
            ".zip": "ZIP Archive", ".tar": "TAR Archive", ".gz": "GZ Archive",
            ".jpg": "JPEG Image", ".jpeg": "JPEG Image", ".png": "PNG Image",
            ".gif": "GIF Image", ".svg": "SVG Image", ".webp": "WebP Image",
            ".mp4": "MP4 Video", ".avi": "AVI Video", ".mkv": "MKV Video",
            ".mp3": "MP3 Audio", ".wav": "WAV Audio", ".flac": "FLAC Audio",
            ".exe": "Executable", ".sh": "Shell Script", ".bat": "Batch Script",
            ".conf": "Config", ".log": "Log File", ".sql": "SQL Script",
            ".db": "Database", ".csv": "CSV Data", ".xls": "Excel",
            ".xlsx": "Excel", ".doc": "Word Doc", ".docx": "Word Doc",
        }
        return type_map.get(ext, f"{ext.upper()[1:]} File" if ext else "File")

    def _get_icon(self, entry: FTPEntry) -> QColor:
        return QColor("#60A5FA") if entry.is_dir else QColor("#94A3B8")


# ─────────────────────────────────────────────
#  TRANSFER QUEUE MODEL
# ─────────────────────────────────────────────

class TransferQueueModel(QAbstractTableModel):
    COLUMNS = ["File", "Type", "Size", "Progress", "Speed", "Status"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: List[TransferTask] = []
        self._task_map: Dict[int, int] = {}  # id -> row index

    def add_task(self, task: TransferTask):
        row = len(self._tasks)
        self.beginInsertRows(QModelIndex(), row, row)
        self._tasks.append(task)
        self._task_map[task.id] = row
        self.endInsertRows()

    def update_task(self, task_id: int, transferred: int, speed: float):
        if task_id in self._task_map:
            row = self._task_map[task_id]
            self._tasks[row].transferred = transferred
            self._tasks[row].speed = speed
            self._tasks[row].status = TransferStatus.IN_PROGRESS
            self.dataChanged.emit(
                self.index(row, 0), self.index(row, self.columnCount() - 1)
            )

    def complete_task(self, task_id: int, success: bool):
        if task_id in self._task_map:
            row = self._task_map[task_id]
            self._tasks[row].status = (
                TransferStatus.COMPLETED if success else TransferStatus.FAILED
            )
            self.dataChanged.emit(
                self.index(row, 0), self.index(row, self.columnCount() - 1)
            )

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._tasks)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        task = self._tasks[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return task.filename
            elif col == 1:
                return "↑ Upload" if task.transfer_type == TransferType.UPLOAD else "↓ Download"
            elif col == 2:
                return self._fmt_size(task.total_size)
            elif col == 3:
                if task.total_size > 0:
                    pct = (task.transferred / task.total_size) * 100
                    return f"{pct:.0f}%"
                return "—"
            elif col == 4:
                if task.speed > 0:
                    return self._fmt_size(int(task.speed)) + "/s"
                return "—"
            elif col == 5:
                status_map = {
                    TransferStatus.QUEUED: "Queued",
                    TransferStatus.IN_PROGRESS: "Transferring",
                    TransferStatus.COMPLETED: "✓ Done",
                    TransferStatus.FAILED: "✗ Failed",
                    TransferStatus.CANCELLED: "Cancelled",
                }
                return status_map.get(task.status, "Unknown")

        elif role == Qt.ItemDataRole.ForegroundRole:
            if task.status == TransferStatus.COMPLETED:
                return QBrush(QColor("#4ADE80"))
            elif task.status == TransferStatus.FAILED:
                return QBrush(QColor("#F87171"))
            elif task.status == TransferStatus.IN_PROGRESS:
                return QBrush(QColor("#60A5FA"))
            return QBrush(QColor("#94A3B8"))

        return None

    def _fmt_size(self, size: int) -> str:
        if size == 0:
            return "—"
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


# ─────────────────────────────────────────────
#  CONNECTION DIALOG
# ─────────────────────────────────────────────

class ConnectionDialog(QDialog):
    """Dialog for entering FTP connection details."""

    def __init__(self, recent: List[Dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to FTP Server")
        self.setFixedSize(480, 420)
        self.setModal(True)
        self._recent = recent
        self._setup_ui()
        self._apply_styles()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # Title
        title = QLabel("FTP CONNECTION")
        title.setObjectName("dialog_title")
        layout.addWidget(title)

        # Recent connections dropdown
        if self._recent:
            recent_group = QGroupBox("Recent Connections")
            recent_layout = QVBoxLayout(recent_group)
            self.recent_combo = QComboBox()
            self.recent_combo.addItem("— Select recent connection —")
            for conn in self._recent:
                self.recent_combo.addItem(f"{conn.get('name', conn.get('host', ''))} ({conn.get('host', '')})")
            self.recent_combo.currentIndexChanged.connect(self._load_recent)
            recent_layout.addWidget(self.recent_combo)
            layout.addWidget(recent_group)

        # Form
        form_group = QGroupBox("Server Details")
        form = QFormLayout(form_group)
        form.setSpacing(10)

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("e.g. ftp.example.com or 192.168.1.1")

        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(21)

        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("anonymous")
        self.user_input.setText("anonymous")

        self.pass_input = QLineEdit()
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_input.setPlaceholderText("Leave blank for anonymous")

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Optional friendly name")

        form.addRow("Host / IP:", self.host_input)
        form.addRow("Port:", self.port_input)
        form.addRow("Username:", self.user_input)
        form.addRow("Password:", self.pass_input)
        form.addRow("Save As:", self.name_input)
        layout.addWidget(form_group)

        # Buttons
        btn_layout = QHBoxLayout()
        self.connect_btn = QPushButton("⚡ Connect")
        self.connect_btn.setObjectName("primary_btn")
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("secondary_btn")
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(self.connect_btn)
        layout.addLayout(btn_layout)

        self.connect_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)

    def _load_recent(self, index: int):
        if index > 0 and self._recent:
            conn = self._recent[index - 1]
            self.host_input.setText(conn.get("host", ""))
            self.port_input.setValue(conn.get("port", 21))
            self.user_input.setText(conn.get("username", "anonymous"))
            self.name_input.setText(conn.get("name", ""))

    def get_connection(self) -> FTPConnection:
        return FTPConnection(
            host=self.host_input.text().strip(),
            port=self.port_input.value(),
            username=self.user_input.text().strip(),
            password=self.pass_input.text(),
            name=self.name_input.text().strip()
        )

    def _apply_styles(self):
        self.setStyleSheet("""
            QDialog {
                background: #0F172A;
                color: #E2E8F0;
            }
            QLabel#dialog_title {
                color: #60A5FA;
                font-size: 18px;
                font-weight: 800;
                letter-spacing: 4px;
            }
            QGroupBox {
                color: #94A3B8;
                border: 1px solid #1E293B;
                border-radius: 8px;
                margin-top: 8px;
                padding: 12px;
                font-size: 11px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 1px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
            }
            QLineEdit, QSpinBox, QComboBox {
                background: #1E293B;
                border: 1px solid #334155;
                border-radius: 6px;
                color: #E2E8F0;
                padding: 8px 12px;
                font-size: 13px;
            }
            QLineEdit:focus, QSpinBox:focus {
                border-color: #60A5FA;
            }
            QLabel {
                color: #94A3B8;
                font-size: 12px;
            }
            QPushButton#primary_btn {
                background: #2563EB;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 10px 24px;
                font-weight: 600;
                font-size: 13px;
                min-width: 120px;
            }
            QPushButton#primary_btn:hover { background: #3B82F6; }
            QPushButton#secondary_btn {
                background: #1E293B;
                color: #94A3B8;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 10px 24px;
                font-size: 13px;
            }
            QPushButton#secondary_btn:hover {
                background: #334155;
                color: #E2E8F0;
            }
        """)


# ─────────────────────────────────────────────
#  LOG PANEL
# ─────────────────────────────────────────────

class LogPanel(QWidget):
    """Shows real-time log messages."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        lbl = QLabel("ACTIVITY LOG")
        lbl.setStyleSheet("color: #475569; font-size: 10px; font-weight: 700; letter-spacing: 2px;")
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedHeight(22)
        clear_btn.setStyleSheet("""
            QPushButton {
                background: #1E293B; color: #64748B; border: none;
                border-radius: 3px; padding: 0 8px; font-size: 10px;
            }
            QPushButton:hover { color: #94A3B8; }
        """)
        header.addWidget(lbl)
        header.addStretch()
        header.addWidget(clear_btn)
        layout.addLayout(header)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("""
            QTextEdit {
                background: #020617;
                color: #64748B;
                border: 1px solid #1E293B;
                border-radius: 6px;
                font-family: 'Courier New', monospace;
                font-size: 11px;
                padding: 8px;
            }
        """)
        layout.addWidget(self.log_view)
        clear_btn.clicked.connect(self.log_view.clear)

    def add_message(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.append(f'<span style="color:#334155">[{timestamp}]</span> <span style="color:#64748B">{msg}</span>')
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)


# ─────────────────────────────────────────────
#  BREADCRUMB BAR
# ─────────────────────────────────────────────

class BreadcrumbBar(QWidget):
    """Path navigation breadcrumb bar."""
    path_clicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(8, 4, 8, 4)
        self._layout.setSpacing(2)
        self._current_path = "/"
        self._update_crumbs("/")

    def set_path(self, path: str):
        self._current_path = path
        self._update_crumbs(path)

    def _update_crumbs(self, path: str):
        # Clear existing
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        parts = [p for p in path.split("/") if p]
        accumulated = "/"

        # Root
        root_btn = QPushButton("/")
        root_btn.setObjectName("crumb_btn")
        root_btn.clicked.connect(lambda _, p="/": self.path_clicked.emit(p))
        self._layout.addWidget(root_btn)

        for part in parts:
            sep = QLabel("›")
            sep.setStyleSheet("color: #334155; font-size: 14px; padding: 0 2px;")
            self._layout.addWidget(sep)

            accumulated = accumulated.rstrip("/") + "/" + part
            path_copy = accumulated
            btn = QPushButton(part)
            btn.setObjectName("crumb_btn")
            btn.clicked.connect(lambda _, p=path_copy: self.path_clicked.emit(p))
            self._layout.addWidget(btn)

        self._layout.addStretch()

        self.setStyleSheet("""
            QPushButton#crumb_btn {
                background: transparent;
                color: #60A5FA;
                border: none;
                padding: 2px 6px;
                font-size: 12px;
                border-radius: 4px;
            }
            QPushButton#crumb_btn:hover {
                background: #1E293B;
                color: #93C5FD;
            }
        """)


# ─────────────────────────────────────────────
#  FILE PANEL (local or remote)
# ─────────────────────────────────────────────

class RemoteFilePanel(QWidget):
    """Panel for displaying and interacting with remote FTP files."""

    # Signals to request operations
    navigate_requested = pyqtSignal(str)
    upload_requested = pyqtSignal(list, str)  # local_paths, remote_dir
    download_requested = pyqtSignal(list)      # list of (remote_path, size)
    delete_requested = pyqtSignal(str, bool)
    rename_requested = pyqtSignal(str)
    mkdir_requested = pyqtSignal(str)
    refresh_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_path = "/"
        self._model = RemoteFileModel(self)
        self._setup_ui()
        self._setup_context_menu()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QFrame()
        header.setObjectName("panel_header")
        header.setFixedHeight(40)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(12, 0, 8, 0)

        icon = QLabel("🖥")
        icon.setFixedWidth(24)
        title = QLabel("REMOTE SERVER")
        title.setObjectName("panel_title")

        self.refresh_btn = QPushButton("⟳")
        self.refresh_btn.setObjectName("icon_btn")
        self.refresh_btn.setFixedSize(28, 28)
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        self.refresh_btn.setToolTip("Refresh (F5)")

        self.upload_btn = QPushButton("↑ Upload")
        self.upload_btn.setObjectName("action_btn")
        self.upload_btn.clicked.connect(self._on_upload_click)

        self.mkdir_btn = QPushButton("+ Folder")
        self.mkdir_btn.setObjectName("action_btn")
        self.mkdir_btn.clicked.connect(self._on_mkdir_click)

        h_layout.addWidget(icon)
        h_layout.addWidget(title)
        h_layout.addStretch()
        h_layout.addWidget(self.mkdir_btn)
        h_layout.addWidget(self.upload_btn)
        h_layout.addWidget(self.refresh_btn)
        layout.addWidget(header)

        # Breadcrumb
        self.breadcrumb = BreadcrumbBar()
        self.breadcrumb.path_clicked.connect(self.navigate_requested.emit)
        self.breadcrumb.setObjectName("breadcrumb_bar")
        layout.addWidget(self.breadcrumb)

        # Table view
        self.table = QTableView()
        self.table.setModel(self._model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.setShowGrid(False)
        self.table.setObjectName("file_table")

        # Column widths
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.verticalHeader().hide()

        # Drag & Drop
        self.table.setAcceptDrops(True)
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)

        layout.addWidget(self.table)

        # Status bar
        self.status_bar = QLabel("Not connected")
        self.status_bar.setObjectName("panel_status")
        layout.addWidget(self.status_bar)

    def _setup_context_menu(self):
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos: QPoint):
        index = self.table.indexAt(pos)
        menu = QMenu(self)

        if index.isValid():
            entry = self._model.get_entry(index.row())
            if entry:
                if not entry.is_dir:
                    dl_action = menu.addAction("⬇  Download")
                    dl_action.triggered.connect(lambda: self._on_download_selected())
                else:
                    open_action = menu.addAction("📂  Open Folder")
                    open_action.triggered.connect(lambda: self.navigate_requested.emit(entry.path))
                menu.addSeparator()
                rename_action = menu.addAction("✏  Rename")
                rename_action.triggered.connect(lambda: self._on_rename(entry))
                delete_action = menu.addAction("🗑  Delete")
                delete_action.triggered.connect(lambda: self._on_delete(entry))
                menu.addSeparator()

        upload_action = menu.addAction("⬆  Upload Files Here")
        upload_action.triggered.connect(self._on_upload_click)
        mkdir_action = menu.addAction("📁  New Folder")
        mkdir_action.triggered.connect(self._on_mkdir_click)
        menu.addSeparator()
        refresh_action = menu.addAction("⟳  Refresh")
        refresh_action.triggered.connect(self.refresh_requested.emit)

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _on_double_click(self, index: QModelIndex):
        entry = self._model.get_entry(index.row())
        if entry:
            if entry.is_dir:
                self.navigate_requested.emit(entry.path)
            else:
                self._on_download_selected()

    def _on_download_selected(self):
        indexes = self.table.selectedIndexes()
        rows = set(i.row() for i in indexes)
        entries = [(e.path, e.size) for r in rows if (e := self._model.get_entry(r)) and not e.is_dir]
        if entries:
            self.download_requested.emit(entries)

    def _on_upload_click(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select files to upload")
        if files:
            self.upload_requested.emit(files, self._current_path)

    def _on_mkdir_click(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if ok and name:
            path = f"{self._current_path.rstrip('/')}/{name}"
            self.mkdir_requested.emit(path)

    def _on_rename(self, entry: FTPEntry):
        new_name, ok = QInputDialog.getText(
            self, "Rename", "New name:", text=entry.name
        )
        if ok and new_name and new_name != entry.name:
            new_path = f"{str(Path(entry.path).parent).rstrip('/')}/{new_name}"
            self.rename_requested.emit(f"{entry.path}||{new_path}")

    def _on_delete(self, entry: FTPEntry):
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete '{entry.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(entry.path, entry.is_dir)

    def set_entries(self, path: str, entries: List[FTPEntry]):
        self._current_path = path
        self._model.set_entries(entries)
        self.breadcrumb.set_path(path)
        count = len(entries)
        dirs = sum(1 for e in entries if e.is_dir)
        files = count - dirs
        self.status_bar.setText(f"{dirs} folders, {files} files  •  {path}")

    def get_current_path(self) -> str:
        return self._current_path

    def get_selected_entries(self) -> List[FTPEntry]:
        indexes = self.table.selectedIndexes()
        rows = set(i.row() for i in indexes)
        return [e for r in rows if (e := self._model.get_entry(r))]


class LocalFilePanel(QWidget):
    """Panel for displaying local computer files."""

    upload_requested = pyqtSignal(list, str)  # local paths, remote destination

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_path = str(Path.home())
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QFrame()
        header.setObjectName("panel_header")
        header.setFixedHeight(40)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(12, 0, 8, 0)

        icon = QLabel("💻")
        icon.setFixedWidth(24)
        title = QLabel("LOCAL COMPUTER")
        title.setObjectName("panel_title")

        h_layout.addWidget(icon)
        h_layout.addWidget(title)
        h_layout.addStretch()
        layout.addWidget(header)

        # Path bar
        self.path_bar = QLineEdit(self._current_path)
        self.path_bar.setObjectName("path_bar")
        self.path_bar.returnPressed.connect(self._navigate_to_path)
        layout.addWidget(self.path_bar)

        # File system model
        self.fs_model = QFileSystemModel()
        self.fs_model.setRootPath(QDir.rootPath())

        self.tree = QTreeView()
        self.tree.setModel(self.fs_model)
        self.tree.setRootIndex(self.fs_model.index(self._current_path))
        self.tree.setColumnWidth(0, 200)
        self.tree.setAlternatingRowColors(True)
        self.tree.setObjectName("file_tree")
        self.tree.doubleClicked.connect(self._on_double_click)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)

        # Hide unnecessary columns
        for col in range(1, self.fs_model.columnCount()):
            pass  # keep all columns for info

        layout.addWidget(self.tree)

        self.status_bar = QLabel(f"📂 {self._current_path}")
        self.status_bar.setObjectName("panel_status")
        layout.addWidget(self.status_bar)

    def _navigate_to_path(self):
        path = self.path_bar.text()
        if os.path.isdir(path):
            self._current_path = path
            self.tree.setRootIndex(self.fs_model.index(path))
            self.status_bar.setText(f"📂 {path}")

    def _on_double_click(self, index: QModelIndex):
        path = self.fs_model.filePath(index)
        if os.path.isdir(path):
            self._current_path = path
            self.tree.setRootIndex(self.fs_model.index(path))
            self.path_bar.setText(path)
            self.status_bar.setText(f"📂 {path}")

    def _show_context_menu(self, pos: QPoint):
        index = self.tree.indexAt(pos)
        menu = QMenu(self)

        if index.isValid():
            path = self.fs_model.filePath(index)
            if os.path.isfile(path):
                upload_action = menu.addAction("⬆  Upload to Server")
                upload_action.triggered.connect(lambda: self.upload_requested.emit([path], ""))
            menu.addSeparator()

        open_action = menu.addAction("📁  Go Up")
        open_action.triggered.connect(self._go_up)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _go_up(self):
        parent = str(Path(self._current_path).parent)
        self._current_path = parent
        self.tree.setRootIndex(self.fs_model.index(parent))
        self.path_bar.setText(parent)

    def get_current_path(self) -> str:
        return self._current_path

    def get_selected_paths(self) -> List[str]:
        return [
            self.fs_model.filePath(i)
            for i in self.tree.selectedIndexes()
            if i.column() == 0
        ]


# ─────────────────────────────────────────────
#  MAIN WINDOW
# ─────────────────────────────────────────────

class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("FTPForge")
        self.setMinimumSize(1280, 760)
        self.resize(1400, 860)

        # State
        self._connected = False
        self._recent_connections: List[Dict] = []
        self._config_path = os.path.expanduser("~/.ftpforge_config.json")
        self._load_config()

        # FTP worker
        self._worker = FTPWorker()
        self._ftp_thread = FTPThread(self._worker)
        self._ftp_thread.start()
        self._connect_signals()

        # Transfer model
        self._transfer_model = TransferQueueModel(self)

        self._setup_ui()
        self._apply_styles()
        self._setup_shortcuts()
        self._update_connection_state(False)

    # ── Setup ────────────────────────────────────

    def _connect_signals(self):
        self._worker.connected.connect(self._on_connected)
        self._worker.disconnected.connect(self._on_disconnected)
        self._worker.connection_error.connect(self._on_connection_error)
        self._worker.directory_loaded.connect(self._on_directory_loaded)
        self._worker.operation_complete.connect(self._on_operation_complete)
        self._worker.transfer_progress.connect(self._on_transfer_progress)
        self._worker.transfer_complete.connect(self._on_transfer_complete)
        self._worker.log_message.connect(self._on_log_message)

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── Top Bar ──────────────────────────────
        top_bar = QFrame()
        top_bar.setObjectName("top_bar")
        top_bar.setFixedHeight(56)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(16, 0, 16, 0)
        top_layout.setSpacing(12)

        # Logo
        logo = QLabel("⚡ FTPForge")
        logo.setObjectName("logo")

        # Connection info
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("status_dot_disconnected")
        self.status_label = QLabel("Not Connected")
        self.status_label.setObjectName("status_label")

        # Connect button
        self.connect_btn = QPushButton("Connect to Server")
        self.connect_btn.setObjectName("connect_btn")
        self.connect_btn.clicked.connect(self._show_connection_dialog)

        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setObjectName("disconnect_btn")
        self.disconnect_btn.clicked.connect(self._disconnect)
        self.disconnect_btn.hide()

        # Speed indicator
        self.speed_label = QLabel("")
        self.speed_label.setObjectName("speed_label")

        top_layout.addWidget(logo)
        top_layout.addSpacing(24)
        top_layout.addWidget(self.status_dot)
        top_layout.addWidget(self.status_label)
        top_layout.addStretch()
        top_layout.addWidget(self.speed_label)
        top_layout.addWidget(self.connect_btn)
        top_layout.addWidget(self.disconnect_btn)
        root_layout.addWidget(top_bar)

        # ── Main Content ─────────────────────────
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.setObjectName("main_splitter")

        # Left: Local file panel
        self.local_panel = LocalFilePanel()
        self.local_panel.upload_requested.connect(self._on_local_upload_requested)
        main_splitter.addWidget(self.local_panel)

        # Right: Remote file panel
        self.remote_panel = RemoteFilePanel()
        self.remote_panel.navigate_requested.connect(self._navigate_remote)
        self.remote_panel.upload_requested.connect(self._on_remote_upload_requested)
        self.remote_panel.download_requested.connect(self._on_download_requested)
        self.remote_panel.delete_requested.connect(self._on_delete_requested)
        self.remote_panel.rename_requested.connect(self._on_rename_requested)
        self.remote_panel.mkdir_requested.connect(self._on_mkdir_requested)
        self.remote_panel.refresh_requested.connect(self._refresh_remote)
        main_splitter.addWidget(self.remote_panel)

        main_splitter.setSizes([480, 720])
        root_layout.addWidget(main_splitter, 1)

        # ── Bottom Panel ─────────────────────────
        bottom_splitter = QSplitter(Qt.Orientation.Horizontal)
        bottom_splitter.setObjectName("bottom_splitter")
        bottom_splitter.setFixedHeight(200)

        # Transfer queue
        transfer_widget = QWidget()
        transfer_layout = QVBoxLayout(transfer_widget)
        transfer_layout.setContentsMargins(8, 8, 4, 8)
        transfer_label = QLabel("TRANSFER QUEUE")
        transfer_label.setStyleSheet("color: #475569; font-size: 10px; font-weight: 700; letter-spacing: 2px;")
        self.transfer_table = QTableView()
        self.transfer_table.setModel(self._transfer_model)
        self.transfer_table.setObjectName("transfer_table")
        self.transfer_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.transfer_table.verticalHeader().hide()
        self.transfer_table.setShowGrid(False)
        self.transfer_table.setAlternatingRowColors(True)
        self.transfer_table.verticalHeader().setDefaultSectionSize(24)
        transfer_layout.addWidget(transfer_label)
        transfer_layout.addWidget(self.transfer_table)
        bottom_splitter.addWidget(transfer_widget)

        # Log panel
        log_widget = QWidget()
        log_layout = QVBoxLayout(log_widget)
        log_layout.setContentsMargins(4, 8, 8, 8)
        self.log_panel = LogPanel()
        log_layout.addWidget(self.log_panel)
        bottom_splitter.addWidget(log_widget)

        bottom_splitter.setSizes([700, 500])
        root_layout.addWidget(bottom_splitter)

    def _setup_shortcuts(self):
        QAction(self).setShortcut(QKeySequence("F5"))
        refresh = QAction("Refresh", self)
        refresh.setShortcut(QKeySequence("F5"))
        refresh.triggered.connect(self._refresh_remote)
        self.addAction(refresh)

        connect_shortcut = QAction("Connect", self)
        connect_shortcut.setShortcut(QKeySequence("Ctrl+K"))
        connect_shortcut.triggered.connect(self._show_connection_dialog)
        self.addAction(connect_shortcut)

    # ── Event Handlers ────────────────────────────

    def _show_connection_dialog(self):
        dialog = ConnectionDialog(self._recent_connections, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            conn = dialog.get_connection()
            if not conn.host:
                QMessageBox.warning(self, "Error", "Please enter a host address.")
                return
            self._save_recent(conn)
            # Run connect in worker thread
            QTimer.singleShot(0, lambda: self._worker.connect(conn))
            self.status_label.setText(f"Connecting to {conn.host}...")

    def _disconnect(self):
        QTimer.singleShot(0, self._worker.disconnect)

    def _navigate_remote(self, path: str):
        if self._connected:
            QTimer.singleShot(0, lambda: self._worker.change_directory(path))

    def _refresh_remote(self):
        if self._connected:
            path = self.remote_panel.get_current_path()
            QTimer.singleShot(0, lambda: self._worker.list_directory(path))

    def _on_local_upload_requested(self, local_paths: list, _: str):
        if not self._connected:
            QMessageBox.warning(self, "Not Connected", "Connect to an FTP server first.")
            return
        remote_dir = self.remote_panel.get_current_path()
        self._upload_files(local_paths, remote_dir)

    def _on_remote_upload_requested(self, local_paths: list, remote_dir: str):
        if not self._connected:
            QMessageBox.warning(self, "Not Connected", "Connect to an FTP server first.")
            return
        self._upload_files(local_paths, remote_dir)

    def _upload_files(self, local_paths: list, remote_dir: str):
        for path in local_paths:
            if os.path.isfile(path):
                remote_path = f"{remote_dir.rstrip('/')}/{os.path.basename(path)}"
                task_id = self._worker.queue_upload(path, remote_path)
                size = os.path.getsize(path)
                task = TransferTask(
                    id=task_id, transfer_type=TransferType.UPLOAD,
                    local_path=path, remote_path=remote_path,
                    filename=os.path.basename(path), total_size=size
                )
                self._transfer_model.add_task(task)
                self._on_log_message(f"Queued upload: {os.path.basename(path)} → {remote_dir}")

    def _on_download_requested(self, entries: list):
        if not self._connected:
            return
        local_dir = QFileDialog.getExistingDirectory(self, "Select download directory")
        if not local_dir:
            return
        for remote_path, size in entries:
            filename = os.path.basename(remote_path)
            local_path = os.path.join(local_dir, filename)
            task_id = self._worker.queue_download(remote_path, local_path, size)
            task = TransferTask(
                id=task_id, transfer_type=TransferType.DOWNLOAD,
                local_path=local_path, remote_path=remote_path,
                filename=filename, total_size=size
            )
            self._transfer_model.add_task(task)
            self._on_log_message(f"Queued download: {filename} → {local_dir}")

    def _on_delete_requested(self, path: str, is_dir: bool):
        if self._connected:
            QTimer.singleShot(0, lambda: self._worker.delete_entry(path, is_dir))

    def _on_rename_requested(self, paths: str):
        old, new = paths.split("||")
        if self._connected:
            QTimer.singleShot(0, lambda: self._worker.rename_entry(old, new))

    def _on_mkdir_requested(self, path: str):
        if self._connected:
            QTimer.singleShot(0, lambda: self._worker.create_directory(path))

    # ── Worker Signal Handlers ────────────────────

    @pyqtSlot()
    def _on_connected(self):
        self._connected = True
        self._update_connection_state(True)
        self._on_log_message("✓ Connection established")
        # Load root directory
        QTimer.singleShot(100, lambda: self._worker.list_directory("/"))

    @pyqtSlot()
    def _on_disconnected(self):
        self._connected = False
        self._update_connection_state(False)
        self.remote_panel.set_entries("/", [])

    @pyqtSlot(str)
    def _on_connection_error(self, error: str):
        self._update_connection_state(False)
        self.status_label.setText("Connection failed")
        QMessageBox.critical(self, "Connection Error", f"Failed to connect:\n{error}")
        self._on_log_message(f"✗ Connection error: {error}")

    @pyqtSlot(str, list)
    def _on_directory_loaded(self, path: str, entries: list):
        self.remote_panel.set_entries(path, entries)

    @pyqtSlot(str, bool, str)
    def _on_operation_complete(self, op: str, success: bool, msg: str):
        icon = "✓" if success else "✗"
        self._on_log_message(f"{icon} {msg}")

    @pyqtSlot(int, int, float)
    def _on_transfer_progress(self, task_id: int, transferred: int, speed: float):
        self._transfer_model.update_task(task_id, transferred, speed)
        # Show speed in top bar
        if speed > 0:
            speed_str = self._format_speed(speed)
            self.speed_label.setText(f"⚡ {speed_str}")

    @pyqtSlot(int, bool, str)
    def _on_transfer_complete(self, task_id: int, success: bool, msg: str):
        self._transfer_model.complete_task(task_id, success)
        icon = "✓" if success else "✗"
        self._on_log_message(f"{icon} Transfer: {msg}")
        self.speed_label.setText("")

    @pyqtSlot(str)
    def _on_log_message(self, msg: str):
        self.log_panel.add_message(msg)

    # ── UI State ─────────────────────────────────

    def _update_connection_state(self, connected: bool):
        self._connected = connected
        if connected:
            self.status_dot.setObjectName("status_dot_connected")
            self.status_dot.setStyleSheet("color: #4ADE80; font-size: 14px;")
            self.status_label.setText("Connected")
            self.connect_btn.hide()
            self.disconnect_btn.show()
        else:
            self.status_dot.setObjectName("status_dot_disconnected")
            self.status_dot.setStyleSheet("color: #475569; font-size: 14px;")
            self.status_label.setText("Not Connected")
            self.connect_btn.show()
            self.disconnect_btn.hide()

    def _format_speed(self, speed: float) -> str:
        for unit in ["B/s", "KB/s", "MB/s", "GB/s"]:
            if speed < 1024:
                return f"{speed:.1f} {unit}"
            speed /= 1024
        return f"{speed:.1f} TB/s"

    # ── Config / Recent Connections ───────────────

    def _load_config(self):
        try:
            if os.path.exists(self._config_path):
                with open(self._config_path, "r") as f:
                    data = json.load(f)
                    self._recent_connections = data.get("recent", [])
        except Exception:
            pass

    def _save_config(self):
        try:
            with open(self._config_path, "w") as f:
                json.dump({"recent": self._recent_connections}, f, indent=2)
        except Exception:
            pass

    def _save_recent(self, conn: FTPConnection):
        entry = conn.to_dict()
        # Remove duplicate
        self._recent_connections = [
            c for c in self._recent_connections if c.get("host") != conn.host
        ]
        self._recent_connections.insert(0, entry)
        self._recent_connections = self._recent_connections[:10]  # Keep 10
        self._save_config()

    # ── Styles ────────────────────────────────────

    def _apply_styles(self):
        self.setStyleSheet("""
            /* ─── Global ─────────────────────────── */
            QMainWindow, QWidget {
                background: #0F172A;
                color: #E2E8F0;
                font-family: 'Segoe UI', 'SF Pro Display', 'Helvetica Neue', sans-serif;
                font-size: 13px;
            }

            /* ─── Top Bar ─────────────────────────── */
            QFrame#top_bar {
                background: #020617;
                border-bottom: 1px solid #1E293B;
            }

            QLabel#logo {
                color: #60A5FA;
                font-size: 18px;
                font-weight: 800;
                letter-spacing: 2px;
            }

            QLabel#status_label {
                color: #64748B;
                font-size: 12px;
            }

            QLabel#speed_label {
                color: #34D399;
                font-size: 12px;
                font-weight: 600;
            }

            QPushButton#connect_btn {
                background: #2563EB;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 20px;
                font-weight: 600;
                font-size: 13px;
            }
            QPushButton#connect_btn:hover { background: #3B82F6; }

            QPushButton#disconnect_btn {
                background: transparent;
                color: #EF4444;
                border: 1px solid #EF4444;
                border-radius: 6px;
                padding: 8px 20px;
                font-weight: 600;
                font-size: 13px;
            }
            QPushButton#disconnect_btn:hover {
                background: #450A0A;
            }

            /* ─── Panel Headers ───────────────────── */
            QFrame#panel_header {
                background: #020617;
                border-bottom: 1px solid #1E293B;
            }

            QLabel#panel_title {
                color: #475569;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 2px;
            }

            QPushButton#action_btn {
                background: #1E293B;
                color: #94A3B8;
                border: 1px solid #334155;
                border-radius: 5px;
                padding: 4px 12px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#action_btn:hover {
                background: #2563EB;
                color: white;
                border-color: #2563EB;
            }

            QPushButton#icon_btn {
                background: transparent;
                color: #475569;
                border: none;
                border-radius: 4px;
                font-size: 16px;
            }
            QPushButton#icon_btn:hover {
                background: #1E293B;
                color: #94A3B8;
            }

            /* ─── Breadcrumb ───────────────────────── */
            QWidget#breadcrumb_bar {
                background: #050C1A;
                border-bottom: 1px solid #1E293B;
                max-height: 28px;
            }

            /* ─── File Table ──────────────────────── */
            QTableView#file_table, QTableView#transfer_table {
                background: #0A1628;
                alternate-background-color: #0D1E35;
                border: none;
                selection-background-color: #1E3A5F;
                selection-color: #E2E8F0;
                gridline-color: transparent;
            }

            QHeaderView::section {
                background: #020617;
                color: #475569;
                border: none;
                border-bottom: 1px solid #1E293B;
                border-right: 1px solid #1E293B;
                padding: 6px 8px;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1px;
                text-transform: uppercase;
            }

            QTreeView#file_tree {
                background: #0A1628;
                alternate-background-color: #0D1E35;
                border: none;
                selection-background-color: #1E3A5F;
                color: #CBD5E1;
            }

            /* ─── Path Bar ────────────────────────── */
            QLineEdit#path_bar {
                background: #050C1A;
                color: #64748B;
                border: none;
                border-bottom: 1px solid #1E293B;
                padding: 6px 12px;
                font-family: 'Courier New', monospace;
                font-size: 11px;
            }
            QLineEdit#path_bar:focus {
                color: #94A3B8;
                border-bottom-color: #2563EB;
            }

            /* ─── Status Bar ───────────────────────── */
            QLabel#panel_status {
                background: #020617;
                color: #334155;
                font-size: 10px;
                padding: 3px 12px;
                border-top: 1px solid #1E293B;
            }

            /* ─── Splitters ────────────────────────── */
            QSplitter#main_splitter::handle {
                background: #1E293B;
                width: 2px;
            }
            QSplitter#bottom_splitter::handle {
                background: #1E293B;
                width: 2px;
            }
            QSplitter#main_splitter, QSplitter#bottom_splitter {
                border-top: 1px solid #1E293B;
            }

            /* ─── Scrollbars ───────────────────────── */
            QScrollBar:vertical, QScrollBar:horizontal {
                background: transparent;
                width: 6px;
                height: 6px;
            }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
                background: #1E293B;
                border-radius: 3px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
                background: #334155;
            }
            QScrollBar::add-line, QScrollBar::sub-line {
                height: 0; width: 0;
            }

            /* ─── Menu ────────────────────────────── */
            QMenu {
                background: #0F172A;
                border: 1px solid #1E293B;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                color: #CBD5E1;
                padding: 7px 20px 7px 12px;
                border-radius: 4px;
                font-size: 12px;
            }
            QMenu::item:selected {
                background: #1E3A5F;
                color: #E2E8F0;
            }
            QMenu::separator {
                height: 1px;
                background: #1E293B;
                margin: 4px 0;
            }

            /* ─── Message Boxes ───────────────────── */
            QMessageBox {
                background: #0F172A;
                color: #E2E8F0;
            }

            /* ─── Input Dialog ────────────────────── */
            QInputDialog {
                background: #0F172A;
                color: #E2E8F0;
            }
            QInputDialog QLineEdit {
                background: #1E293B;
                border: 1px solid #334155;
                border-radius: 6px;
                color: #E2E8F0;
                padding: 8px;
            }
        """)

    def closeEvent(self, event):
        self._save_config()
        if self._connected:
            self._worker.disconnect()
        self._ftp_thread.quit()
        self._ftp_thread.wait(3000)
        event.accept()


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("FTPForge")
    app.setApplicationVersion("1.0.0")

    # Enable high DPI
    app.setStyle("Fusion")

    # Dark palette fallback
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#0F172A"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#E2E8F0"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0A1628"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#0D1E35"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#CBD5E1"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#E2E8F0"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#1E293B"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#1E3A5F"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#E2E8F0"))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()