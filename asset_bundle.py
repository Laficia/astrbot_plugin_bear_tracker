"""Losslessly restore bundled maps to the plugin data cache."""
import hashlib,json
from pathlib import Path
from PIL import Image

def map_directory(component_root,data_dir,configured):
    component_root=Path(component_root);original=component_root/'assets/map_images'
    encoded=component_root/'assets/map_images_webp';manifest=encoded/'manifest.json'
    if Path(configured).resolve()!=original.resolve() or not manifest.is_file():return str(configured)
    rows=json.loads(manifest.read_text('utf-8'))
    if all((original/r['file']).is_file() for r in rows):return str(original)
    directory=Path(data_dir)/('bundled_maps_'+hashlib.sha256(manifest.read_bytes()).hexdigest()[:12]);directory.mkdir(parents=True,exist_ok=True)
    for row in rows:
        if Path(row['file']).name!=row['file'] or Path(row['encoded']).name!=row['encoded']:raise ValueError('地图资源名称无效')
        target=directory/row['file']
        if target.is_file():continue
        with Image.open(encoded/row['encoded']) as image:
            image=image.convert('RGB')
            if hashlib.sha256(image.tobytes()).hexdigest()!=row['pixels']:raise ValueError('地图资源校验失败')
            temp=target.with_suffix('.tmp');image.save(temp,format='PNG',optimize=True,compress_level=9);temp.replace(target)
    return str(directory)
