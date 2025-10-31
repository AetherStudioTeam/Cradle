import os
import sys
import subprocess
import time
import threading
import hashlib
import json
import shutil
import traceback
import fnmatch
from pathlib import Path
from datetime import datetime
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional, Set

class Color:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    GRAY = '\033[90m'

    @staticmethod
    def disable():
        for attr in ['HEADER', 'OKBLUE', 'OKCYAN', 'OKGREEN', 'WARNING', 
                     'FAIL', 'ENDC', 'BOLD', 'UNDERLINE', 'GRAY']:
            setattr(Color, attr, '')
if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except:
        Color.disable()

class BuildConfig:
    DEFAULT_CONFIG = {
        "project": {
            "name": "C Project",
            "output": "program"
        },
        "directories": {
            "source": "src",
            "objects": "obj",
            "binary": "bin",
            "include": ["src"]
        },
        "compiler": {
            "cc": "gcc",
            "alternatives": ["gcc", "clang"],
            "standard": "c99",
            "debug_flags": ["-g", "-DDEBUG", "-Wall", "-Wextra"],
            "release_flags": ["-O2", "-DNDEBUG"],
            "extra_flags": [],
            "defines": [],
            "libraries": []
        },
        "build": {
            "max_workers": 4,
            "timeout": 60,
            "incremental": True,
            "auto_detect_sources": True,
            "source_extensions": [".c"],
            "header_extensions": [".h"],
            "exclude_patterns": []
        },
        "features": {
            "colored_output": True,
            "progress_bar": True,
            "build_statistics": True,
            "dependency_tracking": True
        }
    }
    
    def __init__(self, config_file: str = "build.config.json"):
        self.config_file = Path(config_file)
        self.config = self.load_config()
    
    def load_config(self) -> dict:
        config = self.DEFAULT_CONFIG.copy()
        
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                    config = self.deep_merge(config, user_config)
                print(f"{Color.OKGREEN}Loaded configuration from {self.config_file}{Color.ENDC}")
            except Exception as e:
                print(f"{Color.WARNING}Could not load config file: {e}{Color.ENDC}")
                print(f"{Color.GRAY}  Using default configuration{Color.ENDC}")
        else:
            print(f"{Color.WARNING}No config file found: {self.config_file}{Color.ENDC}")
            print(f"{Color.GRAY}  Using default configuration{Color.ENDC}")
            self.create_default_config()
        
        return config
    
    def deep_merge(self, base: dict, override: dict) -> dict:
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self.deep_merge(result[key], value)
            else:
                result[key] = value
        return result
    
    def create_default_config(self):
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.DEFAULT_CONFIG, f, indent=2)
            print(f"{Color.OKGREEN}Created default config: {self.config_file}{Color.ENDC}")
        except Exception as e:
            print(f"{Color.WARNING}Could not create config file: {e}{Color.ENDC}")
    
    def get(self, *keys, default=None):
        value = self.config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

class BuildTask:
    def __init__(self, task_id: int, name: str, source: Path, output: Path, description: str = None):
        self.task_id = task_id
        self.name = name
        self.source = source
        self.output = output
        self.description = description or f"Compiling {name}"
        self.duration = 0.0
        self.skipped = False
        self.dependencies: List[Path] = []

class ProgressDisplay:
    def __init__(self, max_workers: int = 4, show_progress_bar: bool = True):
        self.max_workers = max_workers
        self.show_progress_bar = show_progress_bar
        self.total_tasks = 0
        self.completed_tasks = 0
        self.skipped_tasks = 0
        self.workers = ["> IDLE"] * max_workers
        self.lock = threading.Lock()
        self.running = False
        self.lines_printed = 0
        
    def update_worker(self, worker_id: int, status: str):
        with self.lock:
            if 0 <= worker_id < self.max_workers:
                self.workers[worker_id] = status
                
    def update_progress(self, completed: int, total: int, skipped: int = 0):
        with self.lock:
            self.completed_tasks = completed
            self.total_tasks = total
            self.skipped_tasks = skipped
            
    def clear_lines(self, n: int):
        for _ in range(n):
            sys.stdout.write('\033[F')
            sys.stdout.write('\033[K')
            
    def render(self):
        with self.lock:
            if self.running and self.lines_printed > 0:
                self.clear_lines(self.lines_printed)
            else:
                self.running = True
            percentage = (self.completed_tasks / self.total_tasks * 100) if self.total_tasks > 0 else 0
            progress = f"{Color.OKBLUE}Building{Color.ENDC} [{self.completed_tasks}/{self.total_tasks}] {percentage:.1f}%"
            print(f"{Color.BOLD}{progress}{Color.ENDC}")
            if self.show_progress_bar:
                bar_width = 40
                filled = int(bar_width * self.completed_tasks / self.total_tasks) if self.total_tasks > 0 else 0
                bar = '█' * filled + '░' * (bar_width - filled)
                print(f"{Color.OKCYAN}{bar}{Color.ENDC}")
            if self.skipped_tasks > 0:
                print(f"{Color.GRAY}({self.skipped_tasks} up-to-date){Color.ENDC}")
            else:
                print()
            for worker in self.workers:
                print(worker)
            print()
            self.lines_printed = 4 + self.max_workers
            sys.stdout.flush()
            
    def finalize(self, success: bool):
        with self.lock:
            if self.lines_printed > 0:
                self.clear_lines(self.lines_printed)
            
            status = f"{Color.OKGREEN}BUILD SUCCESSFUL{Color.ENDC}" if success else f"{Color.FAIL}BUILD FAILED{Color.ENDC}"
            print(f"{Color.BOLD}{status} [{self.total_tasks}/{self.total_tasks}]{Color.ENDC}")
            
            if self.skipped_tasks > 0:
                print(f"{Color.GRAY}({self.skipped_tasks} up-to-date){Color.ENDC}")
            else:
                print()
            
            for _ in range(self.max_workers):
                print(f"{Color.GRAY}> IDLE{Color.ENDC}")
            print()
            
            sys.stdout.flush()
            self.running = False

class SourceScanner:
    def __init__(self, config: BuildConfig):
        self.config = config
        self.source_dir = Path(config.get('directories', 'source', default='src'))
        self.obj_dir = Path(config.get('directories', 'objects', default='obj'))
        self.source_exts = config.get('build', 'source_extensions', default=['.c'])
        self.exclude_patterns = config.get('build', 'exclude_patterns', default=[])
    
    def should_exclude(self, filepath: Path) -> bool:
        name = filepath.name
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(name, pattern):
                return True
        return False
    
    def scan_sources(self) -> List[Path]:
        sources = []
        
        if not self.source_dir.exists():
            print(f"{Color.FAIL}Source directory not found: {self.source_dir}{Color.ENDC}")
            return sources
        
        for ext in self.source_exts:
            for source in self.source_dir.rglob(f'*{ext}'):
                if not self.should_exclude(source):
                    sources.append(source)
        
        return sorted(sources)
    
    def get_object_path(self, source: Path) -> Path:
        try:
            rel_path = source.relative_to(self.source_dir)
        except ValueError:
            rel_path = Path(source.name)
        obj_path = self.obj_dir / rel_path.with_suffix('.o')
        return obj_path
    
    def create_tasks(self) -> List[BuildTask]:
        sources = self.scan_sources()
        tasks = []
        
        for i, source in enumerate(sources, 1):
            obj_path = self.get_object_path(source)
            name = source.stem
            desc = f"Compiling {source.relative_to(self.source_dir)}"
            
            task = BuildTask(i, name, source, obj_path, desc)
            tasks.append(task)
        
        return tasks

class Builder:
    def __init__(self, config: BuildConfig, debug: bool = True, 
             max_workers: Optional[int] = None, incremental: Optional[bool] = None,
             verbose: bool = False):
        self.config = config
        self.debug = debug
        self.verbose = verbose        
        self.max_workers = max_workers or config.get('build', 'max_workers', default=4)
        self.incremental = incremental if incremental is not None else config.get('build', 'incremental', default=True)
        self.tasks: List[BuildTask] = []
        self.total_tasks = 0
        self.completed_tasks = 0
        self.skipped_tasks = 0
        self.failed = False
        self.start_time = None
        self.error_output = []
        self.lock = threading.Lock()
        self.src_dir = Path(config.get('directories', 'source', default='src'))
        self.obj_dir = Path(config.get('directories', 'objects', default='obj'))
        self.bin_dir = Path(config.get('directories', 'binary', default='bin'))
        self.include_dirs = [Path(d) for d in config.get('directories', 'include', default=['src'])]
        self.cc = self.find_compiler()
        self.cflags = self.build_compiler_flags()
        self.cache_file = self.obj_dir / ".build_cache.json"
        self.cache = self.load_cache() if self.incremental else {}
        show_progress = config.get('features', 'progress_bar', default=True)
        self.display = ProgressDisplay(max_workers=self.max_workers, show_progress_bar=show_progress)
        self.available_workers = Queue()
        for i in range(self.max_workers):
            self.available_workers.put(i)
    
    def find_compiler(self) -> str:
        cc = self.config.get('compiler', 'cc', default='gcc')
        alternatives = self.config.get('compiler', 'alternatives', default=['gcc', 'clang'])
        if shutil.which(cc):
            return cc
        for alt_cc in alternatives:
            if shutil.which(alt_cc):
                print(f"{Color.WARNING}'{cc}' not found, using '{alt_cc}'{Color.ENDC}")
                return alt_cc
        print(f"{Color.WARNING}No compiler found, using '{cc}' (may fail){Color.ENDC}")
        return cc
    
    def build_compiler_flags(self) -> List[str]:
        flags = []
        standard = self.config.get('compiler', 'standard', default='c99')
        flags.append(f'-std={standard}')
        if self.debug:
            flags.extend(self.config.get('compiler', 'debug_flags', default=['-g', '-DDEBUG']))
        else:
            flags.extend(self.config.get('compiler', 'release_flags', default=['-O2', '-DNDEBUG']))
        flags.extend(self.config.get('compiler', 'extra_flags', default=[]))
        for define in self.config.get('compiler', 'defines', default=[]):
            flags.append(f'-D{define}')
        for inc_dir in self.include_dirs:
            if inc_dir.exists():
                flags.append(f'-I{inc_dir}')
        
        return flags
    
    def load_cache(self) -> dict:
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return {}
    
    def save_cache(self):
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, indent=2)
        except Exception as e:
            if self.verbose:
                print(f"{Color.WARNING}Could not save cache: {e}{Color.ENDC}")
    
    def get_file_hash(self, filepath: Path) -> Optional[str]:
        try:
            with open(filepath, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()
        except:
            return None
    
    def parse_dependencies(self, source_file: Path) -> List[Path]:
        deps = []
        try:
            with open(source_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith('#include "'):
                        parts = stripped.split('"')
                        if len(parts) >= 2:
                            header = parts[1]
                            for inc_dir in self.include_dirs:
                                header_path = inc_dir / header
                                if header_path.exists():
                                    deps.append(header_path)
                                    break
        except Exception as e:
            if self.verbose:
                print(f"{Color.WARNING}Could not parse dependencies: {e}{Color.ENDC}")
        return deps
    
    def needs_rebuild(self, task: BuildTask) -> bool:
        if not self.incremental:
            return True
        
        if not task.output.exists():
            return True
        
        if not task.source.exists():
            return True
        cache_key = str(task.source)
        cached = self.cache.get(cache_key, {})
        current_hash = self.get_file_hash(task.source)
        
        if cached.get('hash') != current_hash:
            return True
        output_mtime = task.output.stat().st_mtime
        
        if task.source.stat().st_mtime > output_mtime:
            return True
        if self.config.get('features', 'dependency_tracking', default=True):
            task.dependencies = self.parse_dependencies(task.source)
            for dep in task.dependencies:
                if dep.exists() and dep.stat().st_mtime > output_mtime:
                    return True
        
        return False
    
    def update_cache(self, task: BuildTask):
        if not self.incremental:
            return
        
        cache_key = str(task.source)
        self.cache[cache_key] = {
            'hash': self.get_file_hash(task.source),
            'timestamp': time.time(),
            'dependencies': [str(d) for d in task.dependencies]
        }
    
    def get_display_width(self, text: str) -> int:
        import unicodedata
        width = 0
        for char in text:
            if unicodedata.east_asian_width(char) in ('F', 'W', 'A'):
                width += 2
            else:
                width += 1
        return width

    def print_header(self):
        from wcwidth import wcswidth
        projectn = self.config.get('project', 'name', default='C Project')
        box_inner_width = 37
        text_width = wcswidth(projectn)
        total_space = box_inner_width - text_width
        left_space = total_space // 2
        right_space = total_space - left_space
        print()
        print(f"{Color.BOLD}{Color.OKCYAN}╔═══════════════════════════════════════════╗{Color.ENDC}")
        print(f"{Color.BOLD}{Color.OKCYAN}║{' ' * left_space}{projectn}{' ' * right_space}║{Color.ENDC}")
        print(f"{Color.BOLD}{Color.OKCYAN}╚═══════════════════════════════════════════╝{Color.ENDC}")
        print()
        print(f"{Color.GRAY}Build mode:  {Color.ENDC}{Color.BOLD}{'DEBUG' if self.debug else 'RELEASE'}{Color.ENDC}")
        print(f"{Color.GRAY}Compiler:    {Color.ENDC}{self.cc}")
        print(f"{Color.GRAY}Flags:       {Color.ENDC}{' '.join(self.cflags)}")
        print(f"{Color.GRAY}Parallelism: {Color.ENDC}{self.max_workers} workers")
        print(f"{Color.GRAY}Incremental: {Color.ENDC}{'enabled' if self.incremental else 'disabled'}")
        print()
    
    def create_directories(self):
        for task in self.tasks:
            task.output.parent.mkdir(parents=True, exist_ok=True)
        self.bin_dir.mkdir(parents=True, exist_ok=True)
    def compile_task(self, task: BuildTask) -> tuple:
        start = time.time()
        if not self.needs_rebuild(task):
            task.skipped = True
            task.duration = 0
            with self.lock:
                self.completed_tasks += 1
                self.skipped_tasks += 1
                self.display.update_progress(self.completed_tasks, self.total_tasks, self.skipped_tasks)
                self.display.render()
            return True, task
        
        worker_id = self.available_workers.get()
        
        try:
            worker_status = f"{Color.OKCYAN}> Task :{task.task_id} {task.description}{Color.ENDC}"
            self.display.update_worker(worker_id, worker_status)
            self.display.render()
            
            cmd = [self.cc] + self.cflags + ["-c", str(task.source), "-o", str(task.output)]
            
            if self.verbose:
                print(f"\n{Color.GRAY}$ {' '.join(cmd)}{Color.ENDC}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.config.get('build', 'timeout', default=60)
            )
            
            task.duration = time.time() - start
            self.display.update_worker(worker_id, f"{Color.GRAY}> IDLE{Color.ENDC}")
            
            if result.returncode != 0:
                with self.lock:
                    self.error_output.append({
                        'task': task.description,
                        'stderr': result.stderr,
                        'stdout': result.stdout,
                        'duration': task.duration,
                        'command': ' '.join(cmd)
                    })
                return False, task
            
            self.update_cache(task)
            return True, task
            
        except subprocess.TimeoutExpired:
            task.duration = time.time() - start
            with self.lock:
                self.error_output.append({
                    'task': task.description,
                    'stderr': f'Compilation timeout after {task.duration:.1f}s',
                    'stdout': '',
                    'duration': task.duration,
                    'command': ' '.join(cmd) if 'cmd' in locals() else 'N/A'
                })
            self.display.update_worker(worker_id, f"{Color.GRAY}> IDLE{Color.ENDC}")
            return False, task
            
        except Exception as e:
            task.duration = time.time() - start
            with self.lock:
                self.error_output.append({
                    'task': task.description,
                    'stderr': f'{str(e)}\n{traceback.format_exc()}',
                    'stdout': '',
                    'duration': task.duration,
                    'command': ' '.join(cmd) if 'cmd' in locals() else 'N/A'
                })
            self.display.update_worker(worker_id, f"{Color.GRAY}> IDLE{Color.ENDC}")
            return False, task
            
        finally:
            self.available_workers.put(worker_id)
            with self.lock:
                self.completed_tasks += 1
                self.display.update_progress(self.completed_tasks, self.total_tasks, self.skipped_tasks)
                self.display.render()
    
    def link_executable(self) -> bool:
        start = time.time()
        worker_id = self.available_workers.get()
        
        try:
            task_id = self.total_tasks
            worker_status = f"{Color.OKCYAN}> Task :{task_id} Linking executable{Color.ENDC}"
            self.display.update_worker(worker_id, worker_status)
            self.display.render()
            
            obj_files = [str(task.output) for task in self.tasks]
            output_name = self.config.get('project', 'output', default='program')
            if sys.platform == 'win32':
                output_name += '.exe'
            output_path = self.bin_dir / output_name
            
            cmd = [self.cc] + obj_files + ["-o", str(output_path)]
            for lib in self.config.get('compiler', 'libraries', default=[]):
                cmd.append(f'-l{lib}')
            
            if self.verbose:
                print(f"\n{Color.GRAY}$ {' '.join(cmd)}{Color.ENDC}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.config.get('build', 'timeout', default=60)
            )
            
            duration = time.time() - start
            self.display.update_worker(worker_id, f"{Color.GRAY}> IDLE{Color.ENDC}")
            
            if result.returncode != 0:
                with self.lock:
                    self.error_output.append({
                        'task': 'Linking executable',
                        'stderr': result.stderr,
                        'stdout': result.stdout,
                        'duration': duration,
                        'command': ' '.join(cmd)
                    })
                return False
            
            return True
            
        except Exception as e:
            duration = time.time() - start
            with self.lock:
                self.error_output.append({
                    'task': 'Linking executable',
                    'stderr': f'{str(e)}\n{traceback.format_exc()}',
                    'stdout': '',
                    'duration': duration,
                    'command': ' '.join(cmd) if 'cmd' in locals() else 'N/A'
                })
            self.display.update_worker(worker_id, f"{Color.GRAY}> IDLE{Color.ENDC}")
            return False
            
        finally:
            self.available_workers.put(worker_id)
            with self.lock:
                self.completed_tasks += 1
                self.display.update_progress(self.completed_tasks, self.total_tasks, self.skipped_tasks)
                self.display.render()
    
    def print_summary(self):
        total_duration = time.time() - self.start_time
        if self.error_output:
            print()
            for error in self.error_output:
                print(f"{Color.FAIL}{Color.BOLD}Error in {error['task']}:{Color.ENDC}")
                if self.verbose and error.get('command'):
                    print(f"{Color.GRAY}Command: {error['command']}{Color.ENDC}")
                if error.get('stderr'):
                    print(f"{Color.FAIL}{error['stderr']}{Color.ENDC}")
                print(f"  {Color.GRAY}Duration: {error['duration']:.2f}s{Color.ENDC}")
                print()
        
        print(f"{Color.BOLD}{'═' * 50}{Color.ENDC}")
        
        if self.failed:
            print(f"{Color.FAIL}{Color.BOLD}BUILD FAILED{Color.ENDC}")
            print()
            print(f"{Color.FAIL}Build failed with errors{Color.ENDC}")
        else:
            print(f"{Color.OKGREEN}{Color.BOLD}BUILD SUCCESSFUL{Color.ENDC}")
            print()
            output_name = self.config.get('project', 'output', default='program')
            if sys.platform == 'win32':
                output_name += '.exe'
            print(f"{Color.OKGREEN}Built successfully!{Color.ENDC}")
            print(f"{Color.GRAY}  Output: {self.bin_dir / output_name}{Color.ENDC}")
            if self.config.get('features', 'build_statistics', default=True):
                print()
                print(f"{Color.BOLD}Build Statistics:{Color.ENDC}")
                
                compiled = [t for t in self.tasks if not t.skipped]
                if compiled:
                    avg_time = sum(t.duration for t in compiled) / len(compiled)
                    max_task = max(compiled, key=lambda t: t.duration)
                    total_compile = sum(t.duration for t in compiled)
                    
                    print(f"  Tasks:       {len(self.tasks)} total")
                    print(f"  Compiled:    {len(compiled)} files")
                    print(f"  Up-to-date:  {self.skipped_tasks} files")
                    print(f"  Avg time:    {avg_time:.2f}s per task")
                    print(f"  Slowest:     {max_task.name} ({max_task.duration:.2f}s)")
                    if total_duration > 0:
                        efficiency = total_compile / total_duration
                        print(f"  Efficiency:  {efficiency:.1f}x speedup")
                else:
                    print(f"  All {len(self.tasks)} files were up-to-date")
        
        print()
        print(f"{Color.GRAY}Total time: {Color.ENDC}{Color.BOLD}{total_duration:.2f}s{Color.ENDC}")
        print(f"{Color.GRAY}Finished:   {Color.ENDC}{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{Color.BOLD}{'═' * 50}{Color.ENDC}")
        print()
    
    def clean(self):
        print()
        print(f"{Color.BOLD}{Color.OKCYAN}Cleaning build artifacts...{Color.ENDC}")
        print()
        
        dirs_to_clean = [self.obj_dir, self.bin_dir]
        
        for d in dirs_to_clean:
            if d.exists():
                try:
                    shutil.rmtree(d)
                    print(f"  {Color.OKGREEN}Removed {d}{Color.ENDC}")
                except Exception as e:
                    print(f"  {Color.FAIL}Failed to remove {d}: {e}{Color.ENDC}")
        
        print()
        print(f"{Color.OKGREEN}{Color.BOLD}Clean complete!{Color.ENDC}")
        print()
    
    def build(self) -> int:
        self.start_time = time.time()
        self.print_header()
        if self.config.get('build', 'auto_detect_sources', default=True):
            print(f"{Color.BOLD}{Color.OKCYAN}> Scanning for source files{Color.ENDC}")
            scanner = SourceScanner(self.config)
            self.tasks = scanner.create_tasks()
            print(f"  {Color.OKGREEN}Found {len(self.tasks)} source files{Color.ENDC}")
            
            if self.verbose:
                for task in self.tasks:
                    print(f"    {Color.GRAY}{task.source}{Color.ENDC}")
            print()
        
        if not self.tasks:
            print(f"{Color.FAIL}No source files found!{Color.ENDC}")
            return 1
        print(f"{Color.BOLD}{Color.OKCYAN}> Preparing build environment{Color.ENDC}")
        self.create_directories()
        print(f"  {Color.OKGREEN}Directories ready{Color.ENDC}")
        print()
        
        self.total_tasks = len(self.tasks) + 1
        self.display.update_progress(0, self.total_tasks, 0)
        self.display.render()
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.compile_task, task): task for task in self.tasks}
            
            for future in as_completed(futures):
                try:
                    success, task = future.result()
                    if not success:
                        self.failed = True
                except Exception as e:
                    self.failed = True
                    if self.verbose:
                        print(f"\n{Color.FAIL}Unexpected error: {e}{Color.ENDC}")
                        traceback.print_exc()
        
        if self.failed:
            self.display.finalize(False)
            self.print_summary()
            return 1
        if not self.link_executable():
            self.failed = True
            self.display.finalize(False)
            self.print_summary()
            return 1
        if self.incremental:
            self.save_cache()
        
        self.display.finalize(True)
        self.print_summary()
        return 0

def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Universal C Build System',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python build.py                      Build in DEBUG mode
  python build.py --release            Build in RELEASE mode
  python build.py --clean              Clean and build
  python build.py --clean-only         Only clean
  python build.py -j 8                 Use 8 workers
  python build.py -v                   Verbose output
  python build.py --config my.json     Use custom config
        """
    )
    
    parser.add_argument('--release', action='store_true',
                       help='Build in release mode')
    parser.add_argument('--clean', action='store_true',
                       help='Clean before building')
    parser.add_argument('--clean-only', action='store_true',
                       help='Only clean, do not build')
    parser.add_argument('--no-color', action='store_true',
                       help='Disable colored output')
    parser.add_argument('--no-incremental', action='store_true',
                       help='Disable incremental builds')
    parser.add_argument('-j', '--jobs', type=int, dest='workers',
                       help='Number of parallel workers')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Verbose output')
    parser.add_argument('--config', default='build.config.json',
                       help='Configuration file (default: build.config.json)')
    
    args = parser.parse_args()
    
    if args.no_color or not sys.stdout.isatty():
        Color.disable()
    config = BuildConfig(args.config)
    if not config.get('features', 'colored_output', default=True):
        Color.disable()
    builder = Builder(
        config=config,
        debug=not args.release,
        max_workers=args.workers,
        incremental=not args.no_incremental,
        verbose=args.verbose
    )
    if args.clean or args.clean_only:
        builder.clean()
    
    if args.clean_only:
        return 0
    return builder.build()

if __name__ == '__main__':
    sys.exit(main())
