"""Plugin-owned output paths; configured shared roots remain read-only inputs."""
from pathlib import Path
from astrbot.api.star import StarTools


def data_directory():
    directory=Path(StarTools.get_data_dir('astrbot_plugin_bear_tracker'))/'components'/'magic_bucket'
    directory.mkdir(parents=True,exist_ok=True)
    return directory


def output_path(base,value,fallback):
    base=Path(base).resolve();path=Path(value) if value else Path(fallback)
    resolved=(path if path.is_absolute() else base/path).resolve()
    if resolved.is_relative_to(base):return str(resolved)
    # Keep configuration intact and retain existing records on first migration.
    destination=base/fallback
    if resolved.is_file() and not destination.exists():
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.with_suffix(destination.suffix+'.tmp')
        temporary.write_bytes(resolved.read_bytes());temporary.replace(destination)
    return str(destination)
