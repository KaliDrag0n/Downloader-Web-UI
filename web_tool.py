# ==============================================================================
#
#  Downloader Web UI - Main Application
#  - Now with integrated dependency management -
#
# ==============================================================================

import os
import sys
import platform
import subprocess
import requests
import zipfile
import io
import shutil
import threading
import queue
import re
import json
import atexit
import datetime
import time
import signal
from flask import Flask, request, render_template, jsonify, redirect, url_for
from zipfile import ZipFile, ZIP_DEFLATED

# --- Dependency Management & Startup Checks (Runs on every start) ---

def _print_header(title):
    """Prints a formatted header to the console."""
    print("\n" + "="*50)
    print(f" {title}")
    print("="*50)

def _print_status(message, is_ok=True):
    """Prints a status message with a checkmark or cross."""
    symbol = "[✓]" if is_ok else "[✗]"
    print(f" {symbol} {message}")

def _run_command(command, message, silent=True):
    """Runs a command, printing a message and handling errors."""
    print(f" ▶️  {message}")
    try:
        stdout_pipe = subprocess.DEVNULL if silent else None
        subprocess.check_call(command, stdout=stdout_pipe, stderr=subprocess.STDOUT)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"    ERROR: {e}")
        return False

def _manage_windows_dependencies():
    """Handles downloading and updating dependencies for Windows."""
    _print_header("Checking Windows Dependencies")
    
    deps_dir_name = "deps"
    app_root = os.path.dirname(os.path.abspath(__file__))
    deps_dir = os.path.join(app_root, deps_dir_name)
    os.makedirs(deps_dir, exist_ok=True)

    yt_dlp_path = os.path.join(deps_dir, 'yt-dlp.exe')
    ffmpeg_path = os.path.join(deps_dir, 'ffmpeg.exe')

    # 1. Check/Install yt-dlp
    if not os.path.exists(yt_dlp_path):
        print(" ▶️  yt-dlp not found. Downloading...")
        try:
            url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
            r = requests.get(url, stream=True)
            r.raise_for_status()
            with open(yt_dlp_path, 'wb') as f: shutil.copyfileobj(r.raw, f)
            _print_status("yt-dlp downloaded successfully.")
        except Exception as e:
            _print_status(f"Failed to download yt-dlp: {e}", is_ok=False)
            sys.exit(1)
    
    # 2. Update yt-dlp
    if not _run_command([yt_dlp_path, "-U"], "Updating yt-dlp...", silent=False):
        _print_status("Could not update yt-dlp. It might be in use or there could be a network issue.", is_ok=False)
    else:
        _print_status("yt-dlp is up to date.")

    # 3. Check/Install FFmpeg
    if not os.path.exists(ffmpeg_path):
        print(" ▶️  FFmpeg not found. Downloading...")
        try:
            url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip"
            r = requests.get(url, stream=True)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                for file_info in z.infolist():
                    if file_info.filename.endswith('bin/ffmpeg.exe'):
                        with z.open(file_info) as source, open(ffmpeg_path, 'wb') as target:
                            shutil.copyfileobj(source, target)
                        _print_status("FFmpeg downloaded and extracted successfully.")
                        break
        except Exception as e:
            _print_status(f"Failed to download or extract FFmpeg: {e}", is_ok=False)
            sys.exit(1)
    else:
        _print_status("FFmpeg found.")
    
    # Add the local deps folder to the PATH for this session
    os.environ['PATH'] = deps_dir + os.pathsep + os.environ['PATH']
    _print_status("Local dependency path configured.")

def _manage_linux_dependencies():
    """Checks for dependencies on Linux and provides instructions if missing."""
    _print_header("Checking Linux Dependencies")
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    ytdlp_ok = shutil.which("yt-dlp") is not None

    _print_status("Checking for FFmpeg...", is_ok=ffmpeg_ok)
    _print_status("Checking for yt-dlp...", is_ok=ytdlp_ok)

    if not all([ffmpeg_ok, ytdlp_ok]):
        print("\n[!] Some system dependencies are missing!")
        print("    If you are running for the first time, please run the setup script:")
        print(f"    sudo bash {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'install.sh')}")
        sys.exit(1)

def run_startup_checks():
    """Main function to run all pre-flight checks."""
    if platform.system() == "Windows":
        _manage_windows_dependencies()
    elif platform.system() == "Linux":
        _manage_linux_dependencies()
    else:
        print(f"Unsupported OS: {platform.system()}. Manual installation of ffmpeg and yt-dlp is required.")

    _print_header("Checking Python Packages")
    if not _run_command([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], "Installing/updating Python packages..."):
         _print_status("Failed to install Python packages. Please check your Python/pip installation.", is_ok=False)
         sys.exit(1)
    else:
        _print_status("Python packages are up to date.")
    
    _print_header("Pre-flight checks complete. Starting application.")

# --- Run all checks before the application is defined ---
run_startup_checks()

# ==============================================================================
#
#  Flask Application Logic (Original Code)
#
# ==============================================================================

app = Flask(__name__)

#~ --- Configuration --- ~#
APP_VERSION = "1.0.0" # The current version of this application
GITHUB_REPO_SLUG = "KaliDrag0n/Downloader-Web-UI" # Your GitHub repo slug
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = {
    "download_dir": os.path.join(APP_ROOT, "downloads"),
    "temp_dir": os.path.join(APP_ROOT, ".temp"), # New configurable setting
    "cookie_file_content": ""
}
CONF_CONFIG_FILE = os.path.join(APP_ROOT, "config.json")
CONF_STATE_FILE = os.path.join(APP_ROOT, "state.json")
CONF_COOKIE_FILE = os.path.join(APP_ROOT, "cookies.txt")
LOG_DIR = os.path.join(APP_ROOT, "logs")

#~ --- Global State & Threading --- ~#
download_lock = threading.RLock()
download_queue = queue.Queue()
cancel_event = threading.Event()
queue_paused_event = threading.Event()
stop_mode = "CANCEL" 

current_download = {
    "url": None, "job_data": None, "progress": 0, "status": "", "title": None,
    "playlist_title": None, "track_title": None,
    "playlist_count": 0, "playlist_index": 0,
    "speed": None, "eta": None, "file_size": None
}
download_history = []
next_log_id = 0
next_queue_id = 0
history_state_version = 0

# New global state for update checking
update_status = {
    "update_available": False,
    "latest_version": APP_VERSION,
    "release_url": "",
    "release_notes": ""
}

#~ --- Update Checker --- ~#
def _run_update_check():
    """The core logic for checking GitHub for updates."""
    global update_status
    api_url = f"https://api.github.com/repos/{GITHUB_REPO_SLUG}/releases/latest"
    try:
        print("UPDATE: Checking for new version...")
        res = requests.get(api_url, timeout=15)
        res.raise_for_status()
        
        latest_release = res.json()
        latest_version_tag = latest_release.get("tag_name", "").lstrip('v')
        
        if latest_version_tag and latest_version_tag != APP_VERSION:
            print(f"UPDATE: New version found! Latest: {latest_version_tag}, Current: {APP_VERSION}")
            with download_lock:
                update_status["update_available"] = True
                update_status["latest_version"] = latest_version_tag
                update_status["release_url"] = latest_release.get("html_url")
                update_status["release_notes"] = latest_release.get("body")
        else:
            print("UPDATE: You are on the latest version.")
            with download_lock:
                 update_status["update_available"] = False
        return True
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            print("UPDATE: No releases found for this repository on GitHub.")
        else:
            print(f"UPDATE: HTTP Error checking for updates: {e}")
    except Exception as e:
        print(f"UPDATE: An unexpected error occurred while checking for updates: {e}")
    return False

def scheduled_update_check():
    """Runs the update check on a schedule."""
    while True:
        _run_update_check()
        time.sleep(3600)

#~ --- Config & State Persistence --- ~#
def save_config():
    with open(CONF_CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(CONFIG, f, indent=4)

def load_config():
    global CONFIG
    if os.path.exists(CONF_CONFIG_FILE):
        try:
            with open(CONF_CONFIG_FILE, 'r', encoding='utf-8') as f: CONFIG.update(json.load(f))
        except Exception as e: print(f"Error loading config: {e}")

def save_state():
    state_to_save = {}
    with download_lock:
        job_data_copy = current_download.get("job_data")
        if job_data_copy:
            job_data_copy = job_data_copy.copy()

        state_to_save = {
            "queue": [job.copy() for job in download_queue.queue],
            "history": [item.copy() for item in download_history],
            "current_job": job_data_copy,
            "next_log_id": next_log_id,
            "next_queue_id": next_queue_id,
            "history_state_version": history_state_version
        }
    
    try:
        with open(CONF_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state_to_save, f, indent=4)
    except Exception as e:
        print(f"ERROR: Could not save state to file: {e}")


def load_state():
    global download_history, next_log_id, next_queue_id, history_state_version
    if not os.path.exists(CONF_STATE_FILE): return
    try:
        with open(CONF_STATE_FILE, 'r', encoding='utf-8') as f: state = json.load(f)
        with download_lock:
            abandoned_job = state.get("current_job")
            if abandoned_job:
                print(f"Re-queueing abandoned job: {abandoned_job.get('id')}")
                download_queue.put(abandoned_job)
            
            download_history = state.get("history", [])
            next_log_id = state.get("next_log_id", len(download_history))
            next_queue_id = state.get("next_queue_id", 0)
            history_state_version = state.get("history_state_version", 0)
            for job in state.get("queue", []):
                if 'id' not in job: job['id'] = next_queue_id; next_queue_id += 1
                download_queue.put(job)
            print(f"Loaded {download_queue.qsize()} items from queue and {len(download_history)} history entries.")
    except Exception as e:
        print(f"Could not load state file. Error: {e}")
        corrupted_path = CONF_STATE_FILE + ".bak"
        if os.path.exists(CONF_STATE_FILE): os.rename(CONF_STATE_FILE, corrupted_path)
        print(f"Backed up corrupted state file to {corrupted_path}")

#~ --- Worker Helper Functions --- ~#
def format_bytes(b):
    if b is None: return "N/A";
    b = float(b)
    if b < 1024: return f"{b} B"
    if b < 1024**2: return f"{b/1024:.2f} KiB"
    return f"{b/1024**2:.2f} MiB"

def format_seconds(s):
    if s is None: return "N/A";
    try: return str(datetime.timedelta(seconds=int(s)))
    except: return "N/A"

def build_yt_dlp_command(job, temp_dir_path):
    cmd = ['yt-dlp']
    mode = job.get("mode")
    cmd.extend(['--sleep-interval', '3', '--max-sleep-interval', '10'])
    
    final_folder_name = job.get("folder") or "Misc Downloads"
    is_playlist = "playlist?list=" in job.get("url", "")

    if mode == 'music':
        album_metadata = final_folder_name if is_playlist else '%(album)s'
        cmd.extend([
            '-f', 'bestaudio', '-x', '--audio-format', job.get("format", "mp3"),
            '--audio-quality', job.get("quality", "0"), '--embed-metadata', '--embed-thumbnail',
            '--postprocessor-args', f'-metadata album="{album_metadata}" -metadata date="{datetime.datetime.now().year}"',
            '--parse-metadata', 'playlist_index:%(track_number)s',
            '--parse-metadata', 'uploader:%(artist)s'
        ])
    elif mode == 'video':
        quality = job.get('quality', 'best')
        video_format = job.get('format', 'mp4')
        format_str = f"bestvideo[height<={quality[:-1]}]+bestaudio/best" if quality != 'best' else 'bestvideo+bestaudio/best'
        cmd.extend(['-f', format_str, '--merge-output-format', video_format])
        if job.get('embed_subs'):
            cmd.extend(['--embed-subs', '--sub-langs', 'en.*,en-US,en-GB'])
    elif mode == 'clip':
        if job.get('format') == 'audio':
            cmd.extend(['-f', 'bestaudio/best', '-x', '--audio-format', 'mp3', '--audio-quality', '0'])
        else:
            cmd.extend(['-f', 'bestvideo+bestaudio/best', '--merge-output-format', 'mp4'])

    output_template = os.path.join(temp_dir_path, "%(playlist_index)02d - %(title)s.%(ext)s" if is_playlist else "%(title)s.%(ext)s")
    cmd.extend(['--progress-template', '%(progress)j', '-o', output_template])
    
    start = job.get("playlist_start")
    end = job.get("playlist_end")
    if start and end: cmd.extend(['--playlist-items', f'{start}-{end}'])
    elif start: cmd.extend(['--playlist-items', f'{start}:'])
    elif end: cmd.extend(['--playlist-items', f':{end}'])

    if "playlist?list=" in job["url"] or job.get("refetch"): cmd.append('--ignore-errors')
    if os.path.exists(CONF_COOKIE_FILE) and CONFIG.get("cookie_file_content"): cmd.extend(['--cookies', CONF_COOKIE_FILE])
    
    if job.get("archive"):
        temp_archive_file = os.path.join(temp_dir_path, "archive.temp.txt")
        cmd.extend(['--download-archive', temp_archive_file])

    cmd.append(job["url"])
    return cmd

def _finalize_job(job, final_status, log_output):
    temp_dir_path = os.path.join(CONFIG["temp_dir"], f"job_{job['id']}")
    
    final_folder_name = job.get("folder") or "Misc Downloads"
    final_dest_dir = os.path.join(CONFIG["download_dir"], final_folder_name)
    os.makedirs(final_dest_dir, exist_ok=True)
    
    final_title = "Unknown"
    moved_count = 0
    
    target_format = job.get("format", "mp4")
    if job.get("mode") == 'clip' and target_format == 'audio':
        target_format = 'mp3'
    elif job.get("mode") == 'music':
        target_format = job.get("format", "mp3")

    log_output.append(f"Finalizing job. Looking for completed files with extension '.{target_format}'...")

    if os.path.exists(temp_dir_path):
        for f in os.listdir(temp_dir_path):
            if f.endswith(f'.{target_format}'):
                source_path = os.path.join(temp_dir_path, f)
                dest_path = os.path.join(final_dest_dir, f)

                if os.path.exists(dest_path):
                    base, ext = os.path.splitext(f)
                    counter = 1
                    while os.path.exists(dest_path):
                        dest_path = os.path.join(final_dest_dir, f"{base} ({counter}){ext}")
                        counter += 1
                    log_output.append(f"WARNING: File '{f}' already existed. Saving as '{os.path.basename(dest_path)}'.")

                try:
                    shutil.move(source_path, dest_path)
                    final_title = os.path.basename(dest_path).rsplit('.', 1)[0]
                    moved_count += 1
                except Exception as e:
                    log_output.append(f"ERROR: Could not move completed file {f}: {e}")
        
        if moved_count > 0:
            log_output.append(f"Moved {moved_count} completed file(s).")
            if final_status == "FAILED":
                final_status = "PARTIAL"
                log_output.append("Status updated to PARTIAL due to partial success.")
        else:
            log_output.append("No completed files matching target format found to move.")

        temp_archive_file = os.path.join(temp_dir_path, "archive.temp.txt")
        if os.path.exists(temp_archive_file):
            main_archive_file = os.path.join(final_dest_dir, "archive.txt")
            try:
                shutil.copy2(temp_archive_file, main_archive_file)
                log_output.append(f"Updated archive file at {main_archive_file}")
            except Exception as e:
                log_output.append(f"ERROR: Could not update archive file: {e}")

    log_output.append("Cleaning up temporary folder.")
    if os.path.exists(temp_dir_path):
        try:
            shutil.rmtree(temp_dir_path)
        except OSError as e:
            log_output.append(f"Error removing temp folder {temp_dir_path}: {e}")

    # --- GARBAGE COLLECTION FOR CANCELLED JOBS ---
    # If the job was cancelled and the final destination folder is empty, remove it.
    if final_status == "CANCELLED" and os.path.exists(final_dest_dir):
        try:
            if not os.listdir(final_dest_dir):
                log_output.append(f"Destination folder '{final_dest_dir}' is empty after cancellation. Removing it.")
                os.rmdir(final_dest_dir)
            else:
                log_output.append(f"Destination folder '{final_dest_dir}' contains files and will not be removed.")
        except Exception as e:
            log_output.append(f"Error during empty folder cleanup: {e}")

    return final_status, final_folder_name, final_title


def yt_dlp_worker():
    global download_history, next_log_id, history_state_version, stop_mode
    while True:
        queue_paused_event.wait()
        job = download_queue.get()
        cancel_event.clear()
        stop_mode = "CANCEL"
        
        log_output = []
        status = "PENDING"
        temp_dir_path = os.path.join(CONFIG["temp_dir"], f"job_{job['id']}")

        try:
            os.makedirs(temp_dir_path, exist_ok=True)
            log_output.append(f"Created temporary directory: {temp_dir_path}")
            
            if job.get("archive"):
                final_folder_name = job.get("folder") or "Misc Downloads"
                main_archive_file = os.path.join(CONFIG["download_dir"], final_folder_name, "archive.txt")
                if os.path.exists(main_archive_file):
                    temp_archive_file = os.path.join(temp_dir_path, "archive.temp.txt")
                    shutil.copy2(main_archive_file, temp_archive_file)
                    log_output.append(f"Copied existing archive to temp directory for processing.")

            cmd = build_yt_dlp_command(job, temp_dir_path)
            
            with download_lock:
                current_download.update({ 
                    "url": job["url"], "progress": 0, "status": "Starting...", 
                    "title": "Starting Download...", "job_data": job,
                    "playlist_title": None, "track_title": None,
                    "playlist_count": 0, "playlist_index": 0,
                    "speed": None, "eta": None, "file_size": None
                })

            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1)
            
            local_playlist_index = 0
            local_playlist_count = 0
            
            for line in iter(process.stdout.readline, ''):
                if cancel_event.is_set(): process.kill(); break
                line = line.strip(); log_output.append(line)
                with download_lock:
                    if line.startswith('{'):
                        try:
                            progress = json.loads(line)
                            current_download["status"] = progress.get("status", "Downloading...").capitalize()
                            if current_download["status"] == 'Downloading':
                                progress_percent = progress.get("_percent_str") or progress.get("progress", {}).get("fraction")
                                if progress_percent: current_download["progress"] = float(progress_percent) * 100 if isinstance(progress_percent, float) else float(progress_percent.replace('%',''))
                                
                                total_size = progress.get("total_bytes") or progress.get("total_bytes_estimate")
                                current_download["file_size"] = format_bytes(total_size)
                                current_track_title = os.path.basename(progress.get("filename", "...")).rsplit('.',1)[0]
                                
                                is_playlist = "playlist?list=" in job.get("url", "")
                                if is_playlist:
                                    current_download.update({
                                        'playlist_title': job.get("folder"),
                                        'track_title': current_track_title,
                                        'playlist_index': local_playlist_index,
                                        'playlist_count': local_playlist_count
                                    })
                                else:
                                    current_download["title"] = current_track_title

                                current_download.update({"speed": progress.get("_speed_str", "N/A"), "eta": progress.get("_eta_str", "N/A")})
                        except (json.JSONDecodeError, TypeError): pass
                    elif '[download] Downloading item' in line:
                        match = re.search(r'Downloading item (\d+) of (\d+)', line)
                        if match: 
                            local_playlist_index = int(match.group(1))
                            local_playlist_count = int(match.group(2))
                    elif any(s in line for s in ("[ExtractAudio]", "[Merger]")):
                        current_download.update({"status": 'Processing...'})

            process.wait(timeout=7200)
            
            if cancel_event.is_set(): status = "STOPPED" if stop_mode == "SAVE" else "CANCELLED"
            elif process.returncode == 0: status = "COMPLETED"
            else: status = "FAILED"

        except Exception as e:
            status = "ERROR"; log_output.append(f"\nWORKER EXCEPTION: {e}")
        
        finally:
            status, final_folder, final_title = _finalize_job(job, status, log_output)
            
            log_content = "\n".join(log_output)
            log_file_path = os.path.join(LOG_DIR, f"job_{next_log_id}.log")
            try:
                with open(log_file_path, 'w', encoding='utf-8') as f:
                    f.write(log_content)
            except Exception as e:
                print(f"ERROR: Could not write log file {log_file_path}: {e}")
                log_file_path = "LOG_SAVE_ERROR"

            with download_lock:
                history_item = {
                    "url": job["url"], 
                    "title": final_title, 
                    "folder": final_folder, 
                    "job_data": job, 
                    "log_path": log_file_path,
                    "log_id": next_log_id, 
                    "status": status
                }
                download_history.append(history_item)
                next_log_id += 1; history_state_version += 1
                current_download.update({"url": None, "job_data": None})
                download_queue.task_done()
            
            save_state()

#~ --- Flask Routes --- ~#
@app.route("/")
def index_route(): return render_template("index.html")

@app.route("/settings", methods=["GET", "POST"])
def settings_route():
    if request.method == "POST":
        with download_lock:
            CONFIG["download_dir"] = request.form.get("download_dir", CONFIG["download_dir"])
            CONFIG["temp_dir"] = request.form.get("temp_dir", CONFIG["temp_dir"])
            CONFIG["cookie_file_content"] = request.form.get("cookie_content", "")
            with open(CONF_COOKIE_FILE, 'w', encoding='utf-8') as f: f.write(CONFIG["cookie_file_content"])
        save_config()
        return redirect(url_for('settings_route', saved='true'))
    
    with download_lock:
        # Make a copy to avoid issues with threading
        current_update_status = update_status.copy()

    return render_template("settings.html", 
                           config=CONFIG, 
                           saved=request.args.get('saved'),
                           app_version=APP_VERSION,
                           update_info=current_update_status)

@app.route("/status")
def status_route():
    with download_lock:
        return jsonify({"queue": list(download_queue.queue),"current": current_download if current_download["url"] else None,"history_version": history_state_version})

@app.route("/api/update_check")
def update_check_route():
    """Endpoint for the frontend to check for updates."""
    with download_lock:
        return jsonify(update_status)

@app.route("/api/force_update_check", methods=['POST'])
def force_update_check_route():
    """Forces an immediate check for updates."""
    success = _run_update_check()
    if success:
        return jsonify({"message": "Update check completed."})
    else:
        return jsonify({"message": "Update check failed. See server logs for details."}), 500

@app.route('/api/shutdown', methods=['POST'])
def shutdown_route():
    """Shuts down the server process."""
    def shutdown_server():
        print("SHUTDOWN: Server is shutting down...")
        # Send a SIGINT signal to the current process, which is what Ctrl+C does.
        # This allows atexit handlers to run for a graceful shutdown.
        os.kill(os.getpid(), signal.SIGINT)

    # Use a timer to delay the shutdown slightly, allowing the HTTP response to be sent.
    threading.Timer(1.0, shutdown_server).start()
    return jsonify({"message": "Server is shutting down."})

@app.route('/api/install_update', methods=['POST'])
def install_update_route():
    """Triggers the update.bat script to update the application (Windows)."""
    update_script_path = os.path.join(APP_ROOT, "update.bat")
    if os.path.exists(update_script_path):
        try:
            # Launch the batch script in a new console window.
            # This is crucial because it will kill the current process.
            subprocess.Popen([update_script_path], creationflags=subprocess.CREATE_NEW_CONSOLE)
            return jsonify({"message": "Update process initiated. The server will restart shortly."})
        except Exception as e:
            print(f"ERROR: Failed to start update script: {e}")
            return jsonify({"message": f"Failed to start update script: {e}"}), 500
    else:
        return jsonify({"message": "update.bat not found!"}), 404


@app.route("/queue", methods=["POST"])
def add_to_queue_route():
    global next_queue_id
    urls = request.form.get("urls", "").strip().splitlines()
    if not any(urls):
        return jsonify({"message": "At least one URL is required."}), 400
    
    mode = request.form.get("download_mode")
    
    music_folder = request.form.get("music_foldername", "").strip()
    video_folder = request.form.get("video_foldername", "").strip()
    
    folder_name = ""
    if mode == 'music':
        folder_name = music_folder or video_folder
    elif mode == 'video':
        folder_name = video_folder or music_folder
    
    if not folder_name:
        try:
            first_url = next(url for url in urls if url)
            is_playlist = "playlist?list=" in first_url
            
            print_field = 'playlist_title' if is_playlist else 'title'
            fetch_cmd = ['yt-dlp', '--print', print_field, '--playlist-items', '1', '-s', first_url]
            
            result = subprocess.run(fetch_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, check=True)
            output_lines = result.stdout.strip().splitlines()
            if output_lines:
                raw_name = output_lines[0]
                safe_name = unicodedata.normalize('NFKC', raw_name)
                safe_name = safe_name.encode('ascii', 'ignore').decode('ascii')
                safe_name = re.sub(r'[\\/*?:"<>|]', '', safe_name)
                safe_name = safe_name.replace(':', '-')
                safe_name = re.sub(r'\s+', ' ', safe_name).strip().strip('.')
                if not safe_name:
                    folder_name = "Misc Download"
                else:
                    folder_name = safe_name

        except Exception as e:
            print(f"Could not auto-fetch title for folder name: {e}")
            if mode == 'music': folder_name = "Misc Music"
            else: folder_name = "Misc Videos"

    print(f"DEBUG: Mode: {mode}, Chosen Folder: '{folder_name}'")

    jobs_added = 0
    with download_lock:
        for url in urls:
            url = url.strip()
            if not url: continue
            
            job = { 
                "id": next_queue_id, "url": url, "mode": mode,
                "folder": folder_name,
                "archive": request.form.get("use_archive") == "yes",
                "playlist_start": request.form.get("playlist_start"),
                "playlist_end": request.form.get("playlist_end")
            }

            if mode == 'music':
                job.update({
                    "format": request.form.get("music_audio_format", "mp3"),
                    "quality": request.form.get("music_audio_quality", "0"),
                })
            elif mode == 'video':
                job.update({
                    "quality": request.form.get("video_quality", "best"),
                    "format": request.form.get("video_format", "mp4"),
                    "embed_subs": request.form.get("video_embed_subs") == "on"
                })
            elif mode == 'clip':
                job.update({ "format": request.form.get("clip_format", "video") })

            next_queue_id += 1
            download_queue.put(job)
            jobs_added += 1
    
    if jobs_added > 0:
        save_state()
        
    return jsonify({"message": f"Added {jobs_added} job(s) to the queue."})

@app.route('/queue/clear', methods=['POST'])
def clear_queue_route():
    with download_lock:
        while not download_queue.empty():
            try:
                download_queue.get_nowait()
            except queue.Empty:
                break
    save_state()
    return jsonify({"message": "Queue cleared."})

@app.route('/queue/delete/by-id/<int:job_id>', methods=['POST'])
def delete_from_queue_route(job_id):
    with download_lock:
        items = []
        while not download_queue.empty():
            try:
                items.append(download_queue.get_nowait())
            except queue.Empty:
                break
        
        updated_queue = [job for job in items if job.get('id') != job_id]
        
        for job in updated_queue:
            download_queue.put(job)

    save_state()
    return jsonify({"message": "Queue item removed."})


@app.route("/queue/continue", methods=['POST'])
def continue_job_route():
    global next_queue_id
    job = request.get_json()
    if not job or "url" not in job:
        return jsonify({"message": "Invalid job data."}), 400

    with download_lock:
        job['id'] = next_queue_id
        next_queue_id += 1
        download_queue.put(job)
    save_state()

    return jsonify({"message": f"Re-queued job: {job.get('title', job['url'])}"})

@app.route('/preview')
def preview_route():
    url = request.args.get('url')
    if not url: return jsonify({"message": "URL is required."}), 400
    try:
        is_playlist = 'playlist?list=' in url
        title = ""
        thumbnail_url = ""

        if is_playlist:
            title_cmd = ['yt-dlp', '--print', 'playlist_title', '--playlist-items', '1', '-s', url]
            proc_title = subprocess.run(title_cmd, capture_output=True, text=True, timeout=60, check=True, encoding='utf-8', errors='replace')
            output_lines = proc_title.stdout.strip().splitlines()
            if not output_lines: raise Exception("Could not fetch playlist title.")
            title = output_lines[0]
            
            thumb_cmd = ['yt-dlp', '--print', '%(thumbnail)s', '--playlist-items', '1', '-s', url]
            proc_thumb = subprocess.run(thumb_cmd, capture_output=True, text=True, timeout=60, check=True, encoding='utf-8', errors='replace')
            thumb_lines = proc_thumb.stdout.strip().splitlines()
            if thumb_lines: thumbnail_url = thumb_lines[0]
        else:
            json_cmd = ['yt-dlp', '--print-json', '--skip-download', url]
            proc_json = subprocess.run(json_cmd, capture_output=True, text=True, timeout=60, check=True, encoding='utf-8', errors='replace')
            data = json.loads(proc_json.stdout)
            title = data.get('title', 'No Title Found')
            thumbnail_url = data.get('thumbnail', '')
        
        return jsonify({"title": title, "thumbnail": thumbnail_url})
    except Exception as e:
        return jsonify({"message": f"Could not get preview: {e}"}), 500

@app.route('/history')
def get_history_route():
    with download_lock:
        history_summary = [h.copy() for h in download_history]
        for item in history_summary:
            item.pop("log_path", None)
    return jsonify({"history": history_summary})

@app.route('/history/log/<int:log_id>')
def history_log_route(log_id):
    log_content = "Log not found or could not be read."
    log_path = None

    with download_lock:
        item = next((h for h in download_history if h.get("log_id") == log_id), None)
        if item:
            log_path = item.get("log_path")

    if log_path and log_path != "LOG_SAVE_ERROR" and os.path.exists(log_path):
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                log_content = f.read()
        except Exception as e:
            log_content = f"ERROR: Could not read log file at {log_path}. Reason: {e}"
    elif log_path == "LOG_SAVE_ERROR":
        log_content = "There was an error saving the log file for this job."
    
    return jsonify({"log": log_content})

@app.route('/history/clear', methods=['POST'])
def clear_history_route():
    global history_state_version
    paths_to_delete = []
    with download_lock:
        for item in download_history:
            if item.get("log_path") and item.get("log_path") != "LOG_SAVE_ERROR":
                paths_to_delete.append(item["log_path"])
        download_history.clear()
        history_state_version += 1
    
    save_state()

    for path in paths_to_delete:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print(f"ERROR: Could not delete log file {path}: {e}")

    return jsonify({"message": "History cleared."})

@app.route('/history/delete/<int:log_id>', methods=['POST'])
def delete_from_history_route(log_id):
    global history_state_version
    path_to_delete = None

    with download_lock:
        item_to_delete = next((h for h in download_history if h.get("log_id") == log_id), None)
        if not item_to_delete:
            return jsonify({"message": "Item not found."}), 404
        
        path_to_delete = item_to_delete.get("log_path")
        
        download_history[:] = [h for h in download_history if h.get("log_id") != log_id]
        history_state_version += 1

    save_state()
    
    if path_to_delete and path_to_delete != "LOG_SAVE_ERROR" and os.path.exists(path_to_delete):
        try:
            os.remove(path_to_delete)
        except Exception as e:
            print(f"ERROR: Could not delete log file {path_to_delete}: {e}")
            
    return jsonify({"message": "History item deleted."})

@app.route("/stop", methods=['POST'])
def stop_route():
    global stop_mode
    data = request.get_json() or {}
    mode = data.get('mode', 'cancel') 

    if mode == 'save':
        stop_mode = "SAVE"
        message = "Stop & Save signal sent. Completed files will be saved."
    else:
        stop_mode = "CANCEL"
        message = "Cancel signal sent. All temporary files will be deleted."
    
    cancel_event.set()
    return jsonify({"message": message})


#~ --- App Initialization (runs when imported by Waitress) --- ~#
load_config()
os.makedirs(CONFIG["download_dir"], exist_ok=True)
os.makedirs(CONFIG["temp_dir"], exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
atexit.register(save_state)
load_state()

# Start the background threads
update_thread = threading.Thread(target=scheduled_update_check, daemon=True)
update_thread.start()
queue_paused_event.set()
threading.Thread(target=yt_dlp_worker, daemon=True).start()


#~ --- Main Execution (for direct run, e.g. from an IDE) --- ~#
if __name__ == "__main__":
    from waitress import serve
    print("Starting server with Waitress for direct execution...")
    serve(app, host="0.0.0.0", port=8080)
