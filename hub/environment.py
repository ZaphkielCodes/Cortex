"""
hub/environment.py
------------------
Local environment inspection, canonical known-folder resolution,
and bounded filesystem capabilities for Cortex (Phase 9G).

Capabilities:
  - fs.list : List direct children of a directory with bounded results and metadata.
  - fs.stat : Inspect metadata of a specific file or directory.
  - fs.delete: Safely delete a file or directory (destructive; requires confirmation).

Well-Known User Folders:
  - Desktop, Downloads, Documents, Pictures
  - Discovered dynamically via OS environment / Windows Shell Folders / Path.home()
  - Never hardcoded to specific usernames
"""

import os
import platform
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Registry of runtime overrides for known folders (useful for tests / sandboxing)
_FOLDER_OVERRIDES: Dict[str, Path] = {}


def set_known_folder_override(name: str, path: Union[str, Path]) -> None:
    """Set a runtime override for a known folder (e.g. for testing with isolated fixtures)."""
    norm = name.lower().strip()
    _FOLDER_OVERRIDES[norm] = Path(path).resolve()


def clear_known_folder_overrides() -> None:
    """Clear all active known folder overrides."""
    _FOLDER_OVERRIDES.clear()


def get_known_folder(name: str, base_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Resolve semantic known folder names (e.g. 'desktop', 'downloads', 'documents', 'pictures')
    to canonical absolute paths using the operating system / user environment.

    Never hardcodes user paths like C:\\Users\\abhin\\Desktop.
    Uses Windows Registry User Shell Folders where available, falling back to Path.home().
    """
    if not name:
        return None

    norm = name.lower().strip()

    # Strip conversational prefixes and suffixes
    for prefix in ("my ", "the "):
        if norm.startswith(prefix):
            norm = norm[len(prefix):].strip()
    for suffix in (" folder", " directory", " dir"):
        if norm.endswith(suffix):
            norm = norm[:-len(suffix)].strip()

    # Check manual overrides first (e.g. test fixtures)
    if norm in _FOLDER_OVERRIDES:
        return _FOLDER_OVERRIDES[norm]

    # If base_dir is supplied, resolve within base_dir
    if base_dir is not None:
        cand = (base_dir / norm.capitalize()).resolve()
        if cand.exists():
            return cand
        # Also try exact lowercase or uppercase
        for child in base_dir.iterdir():
            if child.is_dir() and child.name.lower() == norm:
                return child.resolve()
        return (base_dir / norm).resolve()

    # Check environment variable for test home
    test_home = os.environ.get("CORTEX_TEST_USER_HOME")
    if test_home:
        cand = Path(test_home) / norm.capitalize()
        return cand.resolve()

    canonical_names = {
        "desktop": "Desktop",
        "downloads": "Downloads",
        "download": "Downloads",
        "documents": "Documents",
        "document": "Documents",
        "docs": "Documents",
        "pictures": "Pictures",
        "picture": "Pictures",
        "photos": "Pictures",
    }

    canonical = canonical_names.get(norm)
    if not canonical:
        return None

    # On Windows, attempt to read canonical User Shell Folders from Registry
    if platform.system() == "Windows":
        try:
            import winreg

            reg_keys = {
                "Desktop": "Desktop",
                "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
                "Documents": "Personal",
                "Pictures": "My Pictures",
            }
            sub_key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
                val_name = reg_keys.get(canonical)
                if val_name:
                    try:
                        raw_val, _ = winreg.QueryValueEx(key, val_name)
                        if raw_val:
                            expanded = os.path.expandvars(str(raw_val))
                            p = Path(expanded).resolve()
                            if p.exists():
                                return p
                    except FileNotFoundError:
                        pass
        except Exception:
            pass

    # Standard fallback via Path.home()
    home = Path.home()
    resolved = (home / canonical).resolve()
    return resolved


def resolve_location_path(location_name: str, base_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Resolve a user location reference (e.g. 'desktop', 'downloads', or an explicit path)
    to a validated canonical path.
    """
    if not location_name:
        return None

    clean = location_name.strip()

    # Check if it matches a known folder
    folder = get_known_folder(clean, base_dir=base_dir)
    if folder:
        return folder

    # Check if clean is an explicit absolute or relative path
    cand = Path(clean).expanduser()
    if cand.is_absolute():
        return cand.resolve()
    elif base_dir:
        return (base_dir / clean).resolve()

    return cand.resolve()


# ---------------------------------------------------------------------------
# Filesystem Inspection Primitives
# ---------------------------------------------------------------------------

def fs_list(
    path: Union[str, Path],
    max_results: int = 50,
    include_hidden: bool = False,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    List direct children of a validated directory.
    Strictly bounded: does NOT recursively walk drives.

    Returns:
      {
        "status": "success" | "failure",
        "path": canonical_path,
        "count": int,
        "total_count": int,
        "truncated": bool,
        "items": [
           {
             "name": str,
             "type": "file" | "directory" | "other",
             "extension": str (e.g. ".lnk"),
             "size": int (bytes),
             "modified": str (ISO 8601),
             "path": str (canonical absolute path)
           }
        ],
        "message": str
      }
    """
    try:
        # Resolve location string if known folder passed as name
        target_path: Optional[Path] = None
        if isinstance(path, str):
            target_path = resolve_location_path(path, base_dir=base_dir)
        else:
            target_path = Path(path).resolve()

        if not target_path or not target_path.exists():
            return {
                "status": "failure",
                "message": f"Directory does not exist: {path}",
                "path": str(path),
                "items": [],
                "count": 0,
            }

        if not target_path.is_dir():
            return {
                "status": "failure",
                "message": f"Path is not a directory: {target_path}",
                "path": str(target_path),
                "items": [],
                "count": 0,
            }

        max_res = max(1, int(max_results)) if max_results else 50

        raw_items: List[Dict[str, Any]] = []
        is_windows = platform.system() == "Windows"

        for entry in target_path.iterdir():
            # Check hidden attribute
            is_hidden = False
            if entry.name.startswith("."):
                is_hidden = True
            elif is_windows:
                try:
                    attrs = entry.stat().st_file_attributes
                    if attrs & stat.FILE_ATTRIBUTE_HIDDEN:
                        is_hidden = True
                except Exception:
                    pass

            if is_hidden and not include_hidden:
                continue

            try:
                st = entry.stat()
                item_type = "file" if entry.is_file() else "directory" if entry.is_dir() else "other"
                mod_dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                item_size = st.st_size if entry.is_file() else 0

                raw_items.append({
                    "name": entry.name,
                    "type": item_type,
                    "extension": entry.suffix.lower(),
                    "size": item_size,
                    "modified": mod_dt,
                    "path": str(entry.resolve()),
                })
            except Exception:
                # File may have been removed or permission denied
                continue

        # Sort deterministically by name (case-insensitive)
        raw_items.sort(key=lambda x: x["name"].lower())

        total_count = len(raw_items)
        truncated = total_count > max_res
        bounded_items = raw_items[:max_res]

        return {
            "status": "success",
            "message": f"Found {total_count} items in {target_path}",
            "path": str(target_path),
            "count": len(bounded_items),
            "total_count": total_count,
            "truncated": truncated,
            "items": bounded_items,
        }
    except Exception as exc:
        return {
            "status": "failure",
            "message": f"fs.list error: {exc}",
            "path": str(path),
            "items": [],
            "count": 0,
        }


def fs_stat(path: Union[str, Path], base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Inspect metadata of one specific path.
    Returns canonical path, name, type, extension, size, modified timestamp, and exists.
    """
    try:
        target_path: Optional[Path] = None
        if isinstance(path, str):
            target_path = resolve_location_path(path, base_dir=base_dir)
        else:
            target_path = Path(path).resolve()

        if not target_path or not target_path.exists():
            return {
                "status": "failure",
                "exists": False,
                "message": f"Path does not exist: {path}",
                "path": str(path),
            }

        st = target_path.stat()
        item_type = "file" if target_path.is_file() else "directory" if target_path.is_dir() else "other"
        mod_dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()

        return {
            "status": "success",
            "exists": True,
            "message": f"Inspected {target_path.name}",
            "name": target_path.name,
            "path": str(target_path.resolve()),
            "type": item_type,
            "extension": target_path.suffix.lower(),
            "size": st.st_size if target_path.is_file() else 0,
            "modified": mod_dt,
        }
    except Exception as exc:
        return {
            "status": "failure",
            "exists": False,
            "message": f"fs.stat error: {exc}",
            "path": str(path),
        }


def fs_open(path: Union[str, Path], base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Open a file with its default associated application, or a directory in File Explorer.
    Verifies that the target path exists.
    """
    try:
        target_path: Optional[Path] = None
        if isinstance(path, str):
            target_path = resolve_location_path(path, base_dir=base_dir)
        else:
            target_path = Path(path).resolve()

        if not target_path or not target_path.exists():
            return {
                "status": "failure",
                "message": f"Path does not exist: {path}",
                "path": str(path),
                "error": "not_found",
            }

        resolved_str = str(target_path)
        is_directory = target_path.is_dir()
        sys_name = platform.system()

        if is_directory:
            if sys_name == "Windows":
                os.startfile(resolved_str)
            elif sys_name == "Darwin":
                subprocess.Popen(["open", resolved_str])
            else:
                subprocess.Popen(["xdg-open", resolved_str])
            return {
                "status": "success",
                "message": f"Opened folder '{target_path.name}' in File Explorer",
                "path": resolved_str,
                "is_directory": True,
                "type": "directory",
            }
        else:
            if sys_name == "Windows":
                os.startfile(resolved_str)
            elif sys_name == "Darwin":
                subprocess.Popen(["open", resolved_str])
            else:
                subprocess.Popen(["xdg-open", resolved_str])
            return {
                "status": "success",
                "message": f"Opened file '{target_path.name}' with default application",
                "path": resolved_str,
                "is_directory": False,
                "type": "file",
            }
    except Exception as exc:
        return {
            "status": "failure",
            "message": f"fs.open error: {exc}",
            "path": str(path),
        }


def fs_copy(
    src: Union[str, Path],
    dst: Union[str, Path],
    overwrite: bool = False,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Copy a file or directory.
    - Source must exist.
    - Destination must be validated.
    - No silent overwrite: if destination exists and overwrite=False, fails with explanation.
    - Verifies actual destination and source state afterward.
    """
    try:
        src_path = resolve_location_path(src, base_dir=base_dir) if isinstance(src, str) else Path(src).resolve()
        if not src_path or not src_path.exists():
            return {
                "status": "failure",
                "message": f"Source does not exist: '{src}'",
                "error": "source_not_found",
            }

        dst_path = resolve_location_path(dst, base_dir=base_dir) if isinstance(dst, str) else Path(dst).resolve()
        if not dst_path:
            dst_path = (src_path.parent / dst).resolve()

        # If dst is an existing directory, copy into it preserving name
        if dst_path.exists() and dst_path.is_dir():
            target_dst = dst_path / src_path.name
        else:
            target_dst = dst_path

        # Overwrite protection
        if target_dst.exists():
            if not overwrite:
                return {
                    "status": "failure",
                    "message": f"Destination already exists: '{target_dst}'. Overwrite is not enabled.",
                    "error": "destination_exists",
                    "overwrite_blocked": True,
                    "src": str(src_path),
                    "dst": str(target_dst),
                }
            if target_dst.is_file():
                target_dst.unlink()
            elif target_dst.is_dir():
                shutil.rmtree(target_dst)

        target_dst.parent.mkdir(parents=True, exist_ok=True)

        if src_path.is_file():
            shutil.copy2(str(src_path), str(target_dst))
        elif src_path.is_dir():
            shutil.copytree(str(src_path), str(target_dst))

        # Verification: both source and destination must exist
        if not target_dst.exists():
            return {
                "status": "failure",
                "message": f"Copy failed: destination '{target_dst}' was not created.",
                "error": "verification_failed",
            }
        if not src_path.exists():
            return {
                "status": "failure",
                "message": f"Copy failed: source '{src_path}' unexpectedly vanished.",
                "error": "verification_failed",
            }

        return {
            "status": "success",
            "message": f"Copied '{src_path.name}' to '{target_dst}'",
            "src": str(src_path),
            "dst": str(target_dst),
            "verified": True,
        }
    except Exception as exc:
        return {
            "status": "failure",
            "message": f"fs.copy error: {exc}",
            "src": str(src),
            "dst": str(dst),
        }


def fs_move(
    src: Union[str, Path],
    dst: Union[str, Path],
    overwrite: bool = False,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Move a file or directory.
    - Source must exist.
    - Destination must be validated.
    - No silent overwrite: if destination exists and overwrite=False, fails with explanation.
    - Verifies destination exists AND source no longer exists afterward.
    """
    try:
        src_path = resolve_location_path(src, base_dir=base_dir) if isinstance(src, str) else Path(src).resolve()
        if not src_path or not src_path.exists():
            return {
                "status": "failure",
                "message": f"Source does not exist: '{src}'",
                "error": "source_not_found",
            }

        dst_path = resolve_location_path(dst, base_dir=base_dir) if isinstance(dst, str) else Path(dst).resolve()
        if not dst_path:
            dst_path = (src_path.parent / dst).resolve()

        if dst_path.exists() and dst_path.is_dir():
            target_dst = dst_path / src_path.name
        else:
            target_dst = dst_path

        # Overwrite protection
        if target_dst.exists() and target_dst.resolve() != src_path.resolve():
            if not overwrite:
                return {
                    "status": "failure",
                    "message": f"Destination already exists: '{target_dst}'. Overwrite is not enabled.",
                    "error": "destination_exists",
                    "overwrite_blocked": True,
                    "src": str(src_path),
                    "dst": str(target_dst),
                }
            if target_dst.is_file():
                target_dst.unlink()
            elif target_dst.is_dir():
                shutil.rmtree(target_dst)

        target_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(target_dst))

        # Verification: destination exists AND source absent
        if not target_dst.exists():
            return {
                "status": "failure",
                "message": f"Move failed: destination '{target_dst}' does not exist.",
                "error": "verification_failed",
            }
        if src_path.exists() and src_path.resolve() != target_dst.resolve():
            return {
                "status": "failure",
                "message": f"Move failed: source '{src_path}' is still present.",
                "error": "verification_failed",
            }

        return {
            "status": "success",
            "message": f"Moved '{src_path.name}' to '{target_dst}'",
            "src": str(src_path),
            "dst": str(target_dst),
            "verified": True,
        }
    except Exception as exc:
        return {
            "status": "failure",
            "message": f"fs.move error: {exc}",
            "src": str(src),
            "dst": str(dst),
        }


def fs_rename(
    src: Union[str, Path],
    new_name: Union[str, Path],
    overwrite: bool = False,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Rename a file or directory.
    - If new_name is a relative name without parent, renames within same directory.
    - Overwrite protection if new name already exists.
    - Verifies new name exists and old name absent.
    """
    try:
        src_path = resolve_location_path(src, base_dir=base_dir) if isinstance(src, str) else Path(src).resolve()
        if not src_path or not src_path.exists():
            return {
                "status": "failure",
                "message": f"Source does not exist: '{src}'",
                "error": "source_not_found",
            }

        new_name_str = str(new_name).strip()
        if "/" not in new_name_str and "\\" not in new_name_str:
            target_dst = src_path.parent / new_name_str
        else:
            target_dst = resolve_location_path(new_name_str, base_dir=base_dir) or (src_path.parent / new_name_str)

        # Overwrite protection
        if target_dst.exists() and target_dst.resolve() != src_path.resolve():
            if not overwrite:
                return {
                    "status": "failure",
                    "message": f"Destination already exists: '{target_dst}'. Overwrite is not enabled.",
                    "error": "destination_exists",
                    "overwrite_blocked": True,
                    "src": str(src_path),
                    "dst": str(target_dst),
                }
            if target_dst.is_file():
                target_dst.unlink()
            elif target_dst.is_dir():
                shutil.rmtree(target_dst)

        src_path.rename(target_dst)

        # Verification
        if not target_dst.exists():
            return {
                "status": "failure",
                "message": f"Rename failed: target '{target_dst}' does not exist.",
                "error": "verification_failed",
            }
        if src_path.exists() and src_path.resolve() != target_dst.resolve():
            return {
                "status": "failure",
                "message": f"Rename failed: original '{src_path}' is still present.",
                "error": "verification_failed",
            }

        return {
            "status": "success",
            "message": f"Renamed '{src_path.name}' to '{target_dst.name}'",
            "src": str(src_path),
            "dst": str(target_dst),
            "verified": True,
        }
    except Exception as exc:
        return {
            "status": "failure",
            "message": f"fs.rename error: {exc}",
            "src": str(src),
            "new_name": str(new_name),
        }


def fs_delete(path: Union[str, Path], base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Permanently delete a file or directory.
    DESTRUCTIVE ACTION (Tier 3).
    Must only report success after verifying target is absent.
    """
    try:
        target_path: Optional[Path] = None
        if isinstance(path, str):
            target_path = resolve_location_path(path, base_dir=base_dir)
        else:
            target_path = Path(path).resolve()

        if not target_path or not target_path.exists():
            return {
                "status": "failure",
                "message": f"Target does not exist for deletion: {path}",
                "path": str(path),
            }

        resolved_str = str(target_path)
        if target_path.is_file():
            target_path.unlink()
        elif target_path.is_dir():
            shutil.rmtree(target_path)
        else:
            target_path.unlink()

        # Strict post-action verification: target MUST be absent
        if target_path.exists():
            return {
                "status": "failure",
                "message": f"Could not delete '{target_path.name}'. Target is still present.",
                "path": resolved_str,
                "error": "delete_verification_failed",
            }

        return {
            "status": "success",
            "message": f"Successfully deleted '{target_path.name}'",
            "path": resolved_str,
            "verified": True,
        }
    except Exception as exc:
        return {
            "status": "failure",
            "message": f"fs.delete error: {exc}",
            "path": str(path),
        }


def fs_shortcut_info(path: Union[str, Path], base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Inspect metadata of a Windows shortcut (.lnk file).
    Read-only helper. Never executes shortcut targets.
    """
    try:
        target_path: Optional[Path] = None
        if isinstance(path, str):
            target_path = resolve_location_path(path, base_dir=base_dir)
        else:
            target_path = Path(path).resolve()

        if not target_path or not target_path.exists():
            return {
                "status": "failure",
                "exists": False,
                "message": f"Shortcut does not exist: {path}",
                "path": str(path),
            }

        display_name = target_path.stem
        raw_target = ""
        arguments = ""
        description = ""
        working_dir = ""

        if target_path.suffix.lower() == ".lnk":
            try:
                import win32com.client
                shell = win32com.client.Dispatch("WScript.Shell")
                shortcut = shell.CreateShortCut(str(target_path))
                raw_target = str(shortcut.TargetPath or "")
                arguments = str(shortcut.Arguments or "")
                description = str(shortcut.Description or "")
                working_dir = str(shortcut.WorkingDirectory or "")
            except Exception:
                pass

        target_exe = Path(raw_target).name if raw_target else ""

        return {
            "status": "success",
            "exists": True,
            "path": str(target_path),
            "name": target_path.name,
            "display_name": display_name,
            "target_path": raw_target,
            "target_executable": target_exe,
            "arguments": arguments,
            "description": description,
            "working_directory": working_dir,
            "is_shortcut": target_path.suffix.lower() == ".lnk",
        }
    except Exception as exc:
        return {
            "status": "failure",
            "message": f"fs.shortcut_info error: {exc}",
            "path": str(path),
        }
