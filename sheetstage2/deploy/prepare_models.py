"""Download pinned models/assets for the system-Python deployment.

Run after sourcing service.env, with HF_HUB_OFFLINE=0. Credentials are read
from HF_HOME/token by huggingface_hub; this script never prints tokens.
"""
from pathlib import Path
import os
import shutil
import urllib.request

from huggingface_hub import snapshot_download

MODELS = (
    ("m-a-p/SheetSage2", "4f89269db831bdc1880124164a00d4f9385cd129", "sheet"),
    ("m-a-p/MERT-v2-FullSong", "d8ba1c745e733b3908ce6ad16ebeb17ac7600a42", "sheet"),
    ("MuScriptor/muscriptor-large", "8809fdfbed2affa7ade94a7059e746e3880720e7", "hf"),
    ("m-a-p/SheetSage2", "32c7c7473e7b59e1ed36bd8a8fefbd11ae8d468a", "render"),
)


def main() -> None:
    root = Path(os.environ["SHEETSTAGE2_DATA_DIR"])
    hf = Path(os.environ["HF_HOME"])
    modules = Path(os.environ["HF_MODULES_CACHE"])
    for repo, revision, kind in MODELS:
        cache = hf / "hub" if kind == "hf" else root / "models"
        if kind == "render":
            cache /= "rendering"
        snapshot = Path(snapshot_download(
            repo_id=repo,
            revision=revision,
            cache_dir=cache,
            max_workers=1,
            allow_patterns=["rendering_sheetsage2.py", "render_assets/**"]
            if kind == "render" else ["*.py", "*.json", "*.safetensors"],
        ))
        # Runtime consumers ask for main while offline. Pin that local ref too.
        refs = snapshot.parent.parent / "refs"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "main").write_text(revision, encoding="utf-8")
        if kind == "sheet":
            # Transformers 4.45 can miss nested relative imports in a fresh
            # dynamic-module cache. Seed all Python sources before first load.
            target = modules / "transformers_modules" / repo / revision
            target.mkdir(parents=True, exist_ok=True)
            for source in snapshot.glob("*.py"):
                if not source.name.startswith("._"):
                    shutil.copyfile(source, target / source.name)
        print(f"已准备 {repo} {revision} ({kind})", flush=True)

    checkpoint = Path(os.environ["TORCH_HOME"]) / "hub/checkpoints/beat_this-final0.ckpt"
    if not checkpoint.is_file():
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = checkpoint.with_suffix(".partial")
        url = "https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt"
        with urllib.request.urlopen(url, timeout=120) as source, temporary.open("wb") as out:
            shutil.copyfileobj(source, out)
        temporary.replace(checkpoint)
    print("已准备 Beat This final0", flush=True)


if __name__ == "__main__":
    main()
