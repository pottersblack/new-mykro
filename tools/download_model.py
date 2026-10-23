"""
MYKRO Model Downloader
Downloads a GGUF model from HuggingFace with progress reporting.
Usage: python tools/download_model.py
Requires a free HuggingFace account token from: https://huggingface.co/settings/tokens
"""
import os
import sys

os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"  # Use hf_transfer for max speed

from huggingface_hub import snapshot_download, hf_hub_download, list_repo_files, login
import re

REPO_ID   = "MaziyarPanahi/Mixtral-8x22B-Instruct-v0.1-GGUF"
PATTERN   = r"Mixtral-8x22B-Instruct-v0\.1\.Q4_K_M.*\.gguf"
LOCAL_DIR = os.path.join(os.path.expanduser("~"), "models", "mega")

# Authenticate — reads from HF_TOKEN env var, or prompts interactively
hf_token = os.environ.get("HF_TOKEN", "").strip()
if hf_token:
    login(token=hf_token, add_to_git_credential=False)
    print("[MYKRO Downloader] Logged in via HF_TOKEN env var.")
else:
    print("[MYKRO Downloader] No HF_TOKEN env var found.")
    print("  Get a free read token from: https://huggingface.co/settings/tokens")
    print("  Then re-run with: $env:HF_TOKEN='hf_xxxx'; python tools\\download_model.py")
    print()
    token_input = input("Or paste your token now (or press Enter to try anonymously): ").strip()
    if token_input:
        login(token=token_input, add_to_git_credential=False)
        print("[MYKRO Downloader] Logged in.")

os.makedirs(LOCAL_DIR, exist_ok=True)

print(f"[MYKRO Downloader] Scanning repo: {REPO_ID}")
print(f"[MYKRO Downloader] Pattern: {PATTERN}")
print(f"[MYKRO Downloader] Destination: {LOCAL_DIR}")
print()

# List all matching files
files = [f for f in list_repo_files(REPO_ID) if re.search(PATTERN, f, re.IGNORECASE)]

if not files:
    print("No files matched the pattern. Listing all .gguf files in repo:")
    for f in list_repo_files(REPO_ID):
        if f.endswith(".gguf"):
            print(f"  {f}")
    sys.exit(1)

print(f"Found {len(files)} file(s) to download:")
for f in files:
    print(f"  {f}")
print()

for i, filename in enumerate(files, 1):
    dest = os.path.join(LOCAL_DIR, os.path.basename(filename))
    if os.path.exists(dest) and os.path.getsize(dest) > 1_000_000:
        print(f"[{i}/{len(files)}] Already exists, skipping: {filename}")
        continue
    print(f"[{i}/{len(files)}] Downloading: {filename}")
    hf_hub_download(
        repo_id=REPO_ID,
        filename=filename,
        local_dir=LOCAL_DIR,
        resume_download=True,
    )
    print(f"[{i}/{len(files)}] Done: {filename}")

print()
print("[MYKRO Downloader] All files downloaded.")
print(f"[MYKRO Downloader] Files in {LOCAL_DIR}:")
for f in os.listdir(LOCAL_DIR):
    size = os.path.getsize(os.path.join(LOCAL_DIR, f))
    print(f"  {f}  ({size/1e9:.2f} GB)")
