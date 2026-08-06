@echo off
setlocal

cd /d "%~dp0"

:: ---- memory allocator: biggest single win against fragmentation OOMs ----
set PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

:: ---- keep HF/torch caches off the C: drive if space is tight ----
set HF_HOME=D:\ai\cache\huggingface
set TORCH_HOME=D:\ai\cache\torch

:: ---- silence the tokenizers fork warning ----
set TOKENIZERS_PARALLELISM=false

call venv\Scripts\activate.bat

python main.py ^
  --use-sage-attention ^
  --fast ^
  --disable-smart-memory ^
  --reserve-vram 0.9 ^
  --preview-method none ^
  --listen 127.0.0.1 ^
  --port 8188

pause
