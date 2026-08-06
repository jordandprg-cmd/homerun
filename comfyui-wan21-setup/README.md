# ComfyUI + WAN 2.1 (Q8 GGUF) — Unrestricted Local Talking-Video Rig

A complete, reproducible build log for running **Wan 2.1 14B at Q8_0 quantization** inside
ComfyUI on a single consumer GPU, with the full encoder stack for **audio-driven talking-head
video** (image + speech → lip-synced video), plus the hardware, launcher, and troubleshooting
work needed to make it actually run instead of OOM-ing at step 3.

This is a *local* rig. Nothing here calls a hosted API, nothing phones home, and nothing is
moderated at inference time. What that does and does not mean is covered honestly in
[§9 Unrestricted generation](#9-unrestricted-generation-what-is-actually-gating-you).

---

## Table of contents

1. [What this rig does](#1-what-this-rig-does)
2. [Hardware reality check](#2-hardware-reality-check)
3. [Software prerequisites](#3-software-prerequisites)
4. [ComfyUI install](#4-comfyui-install)
5. [Custom nodes](#5-custom-nodes)
6. [The model & encoder stack](#6-the-model--encoder-stack)
7. [The launcher batch file](#7-the-launcher-batch-file)
8. [Workflows](#8-workflows)
9. [Unrestricted generation](#9-unrestricted-generation-what-is-actually-gating-you)
10. [Audio: generating or supplying the track](#10-audio-generating-or-supplying-the-track)
11. [Performance tuning ladder](#11-performance-tuning-ladder)
12. [Troubleshooting log](#12-troubleshooting-log)
13. [Scope, consent, and law](#13-scope-consent-and-law)
14. [Appendix: session notes](#14-appendix-session-notes)

---

## 1. What this rig does

| Capability | Model | Notes |
|---|---|---|
| Text → video | Wan 2.1 T2V 14B (Q8_0 GGUF) | 480p / 720p, 81 frames @ 16fps default |
| Image → video | Wan 2.1 I2V 14B 480P / 720P (Q8_0 GGUF) | needs CLIP-Vision-H |
| First/last frame → video | Wan 2.1 FLF2V 14B | interpolates between two stills |
| Ref/control-guided video | Wan 2.1 VACE 14B / 1.3B | pose, depth, inpaint, reference |
| **Audio → talking video** | **MultiTalk** or **InfiniteTalk** on Wan 2.1 I2V | needs wav2vec2 audio encoder |
| Audio → talking video (native) | Wan 2.2 S2V 14B | newer, audio conditioning baked in |
| Video → audio (foley) | MMAudio | optional, generates a matching track |

The talking-head path is the point of this repo. Wan 2.1 by itself has **no audio conditioning** —
it is a pure visual diffusion transformer. Speech-driven lip sync comes from a *wrapper model*
trained on top of Wan 2.1 I2V:

- **MultiTalk** (MeiGen-AI) — multi-person, audio-driven conversational video. Best when you have
  two or more speakers in frame.
- **InfiniteTalk** (MeiGen-AI) — sparse-frame dubbing, effectively unbounded clip length, far less
  drift on long takes. This is the one to use for anything over ~10 seconds.
- **FantasyTalking** — single-portrait, lighter, faster, less expressive.

All three consume a **wav2vec2** audio embedding, not raw waveform. That encoder is a separate
download and is the single most commonly missed piece of the stack.

---

## 2. Hardware reality check

Wan 2.1 14B at Q8_0 is roughly **15–16 GB of weights** before any activation memory. Numbers below
are measured, not theoretical, at 480p × 81 frames.

| VRAM | Verdict | Configuration |
|---|---|---|
| 24 GB (3090 / 4090 / 5090) | **Target.** Q8_0 fits comfortably. | Native nodes, no block swap needed at 480p. Block swap 10–20 blocks for 720p. |
| 16 GB (4080 / 4060 Ti 16G) | Workable | Q8_0 with block swap 20–30, or drop to Q6_K. Tiled VAE decode mandatory. |
| 12 GB (3080 / 4070) | Painful but possible | Q4_K_M or Q5_K_M, block swap 30–40, `--lowvram`, 480p only. |
| 8 GB | Use the 1.3B model | 14B at any quant thrashes. Wan 2.1 T2V-1.3B or VACE-1.3B instead. |

**System RAM matters more than people expect.** Block swapping moves transformer blocks to system
RAM every step. With 32 GB you will swap to disk and lose the entire speedup. **64 GB is the real
floor** for 14B + block swap + a browser open. 96–128 GB if you keep multiple models resident.

**Storage:** the full stack below is ~120 GB. Put it on NVMe. Model load time off a SATA SSD adds
40–90 s to every cold start, and ComfyUI reloads on every model switch.

**Thermals:** a 14B video diffusion run pins the GPU at 100% for minutes at a time — this is a
sustained-load workload closer to mining than to gaming. If your card thermal-throttles at
83 °C under Furmark it will throttle here too, and a throttled 4090 loses ~25% of its
iterations/sec. Undervolt (MSI Afterburner curve, ~0.95 V @ 2600 MHz on Ada) rather than
overclock; you gain stability and lose almost nothing.

---

## 3. Software prerequisites

```
Python      3.12.x   (3.13 breaks several custom nodes; 3.11 also fine)
CUDA        12.4+    (12.8 for Blackwell / 50-series)
PyTorch     2.6+     (2.7+ for 50-series)
git
7-zip or equivalent
```

Windows-specific, and the source of most install failures:

- **Visual Studio Build Tools 2022** with "Desktop development with C++". Required by Triton,
  SageAttention, flash-attn — anything that compiles a kernel. Install this *first*.
- **triton-windows** — the Windows port of Triton. `pip install triton-windows`. Do not try to
  install upstream `triton` on Windows; it has no Windows wheels.
- Keep the ComfyUI venv **separate** from any system Python. Mixed torch versions are the
  number-one cause of "it worked yesterday."

Install torch matched to your CUDA before anything else, or pip will pull a CPU build:

```bat
python -m venv venv
venv\Scripts\activate
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
```

For 50-series (Blackwell, sm_120), use `cu128` and torch 2.7+ or you get
`no kernel image is available for execution on the device`.

---

## 4. ComfyUI install

```bat
git clone https://github.com/comfyanonymous/ComfyUI
cd ComfyUI
python -m venv venv
venv\Scripts\activate
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
```

Then ComfyUI-Manager, which everything else installs through:

```bat
cd custom_nodes
git clone https://github.com/ltdrdata/ComfyUI-Manager
cd ..
```

Attention backends — install in this order, each is optional but each is a real speedup:

```bat
pip install triton-windows
pip install sageattention
:: SageAttention 2.x needs compilation; build from source if the wheel is missing:
:: git clone https://github.com/thu-ml/SageAttention && cd SageAttention && pip install -e .
```

Verify before you trust it:

```bat
python -c "import torch, triton; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
python -c "import sageattention; print('sage ok')"
```

---

## 5. Custom nodes

Install via Manager or `git clone` into `custom_nodes/`:

| Node pack | Repo | Why |
|---|---|---|
| **ComfyUI-GGUF** | `city96/ComfyUI-GGUF` | **Required.** `UnetLoaderGGUF` — loads the Q8_0 quant. |
| **ComfyUI-WanVideoWrapper** | `kijai/ComfyUI-WanVideoWrapper` | Block swap, TeaCache, MultiTalk/InfiniteTalk support. The power-user path. |
| ComfyUI-VideoHelperSuite | `Kosinkadink/ComfyUI-VideoHelperSuite` | Video load/save/combine, audio muxing. |
| ComfyUI-KJNodes | `kijai/ComfyUI-KJNodes` | Utility nodes half the workflows depend on. |
| ComfyUI-Frame-Interpolation | `Fannovel16/ComfyUI-Frame-Interpolation` | RIFE/FILM — 16fps → 32/48fps. |
| ComfyUI_essentials | `cubiq/ComfyUI_essentials` | Image prep, resize, mask ops. |
| ComfyUI-Crystools | `crystian/ComfyUI-Crystools` | Live VRAM/RAM/temp readout in the UI. Invaluable while tuning. |

**Two competing paths, pick one per workflow and don't mix:**

- **Native ComfyUI nodes** (`WanImageToVideo`, `UNETLoader`/`UnetLoaderGGUF`, `CLIPLoader`) —
  simpler, better GGUF support, fewer dependency breaks.
- **WanVideoWrapper** (`WanVideoModelLoader`, `WanVideoSampler`, `WanVideoBlockSwap`) — more
  control, block swap, TeaCache, and it's where MultiTalk/InfiniteTalk actually live.

Mixing loaders between the two families produces confident-looking type errors. Keep them in
separate workflow files.

---

## 6. The model & encoder stack

Everything below goes in `ComfyUI/models/`. Exact filenames matter — several workflows
hardcode them.

### 6.1 Diffusion model (Q8_0 GGUF)

`models/unet/` (or `models/diffusion_models/`)

From **QuantStack** or **city96** on Hugging Face:

```
Wan2.1-I2V-14B-480P-Q8_0.gguf          ~16.0 GB   ← primary for talking-head
Wan2.1-I2V-14B-720P-Q8_0.gguf          ~16.0 GB   ← if you have 24 GB
Wan2.1-T2V-14B-Q8_0.gguf               ~16.0 GB   ← pure text-to-video
Wan2.1-VACE-14B-Q8_0.gguf              ~16.0 GB   ← control/reference
```

**Why Q8_0 specifically.** Q8_0 is effectively lossless against fp16 for this architecture —
side-by-side at fixed seed, differences are below noise. Q6_K starts showing minor texture
degradation; Q5 and below visibly softens faces and hands, which matters a great deal when the
subject is a talking human face. Q8_0 is the last quant where you are not trading identity
fidelity for VRAM. fp8_e4m3fn is a similar size to Q8_0 but *worse* quality on pre-Ada cards
(no native fp8) and only marginally faster on Ada — Q8_0 is the better default.

### 6.2 Text encoder — UMT5-XXL

`models/text_encoders/` (older ComfyUI: `models/clip/`)

```
umt5_xxl_fp8_e4m3fn_scaled.safetensors   ~6.7 GB   ← recommended, from Comfy-Org/Wan_2.1_ComfyUI_repackaged
umt5-xxl-encoder-Q8_0.gguf               ~6.0 GB   ← GGUF alt, needs ComfyUI-GGUF's CLIPLoaderGGUF
umt5_xxl_fp16.safetensors               ~11.4 GB   ← full precision, only if VRAM is free
```

Wan 2.1 uses **UMT5-XXL**, not T5-XXL and not CLIP-L. They are not interchangeable — loading a
T5 checkpoint here throws a shape mismatch on the encoder embedding.

Loader type must be set to `wan`:
`CLIPLoader → clip_name: umt5_xxl_fp8_e4m3fn_scaled.safetensors, type: wan`

### 6.3 VAE

`models/vae/`

```
wan_2.1_vae.safetensors     ~254 MB
```

Shared across every Wan 2.1 variant. Wan 2.2's 2.2 VAE is **not** compatible with 2.1 models
(different latent channel count) — mixing them gives you colored static.

### 6.4 CLIP Vision — required for I2V

`models/clip_vision/`

```
clip_vision_h.safetensors   ~1.26 GB    (CLIP-ViT-H-14)
```

Only used by the **I2V** and talking-head paths — it encodes the source image so the model knows
what the subject looks like. T2V does not need it. Missing this file is the cause of
`KeyError: 'clip_vision_output'` and of I2V output that ignores your input image.

### 6.5 Audio encoder — wav2vec2 (the piece everyone misses)

`models/wav2vec2/` — or wherever your MultiTalk/InfiniteTalk nodes expect it; kijai's wrapper
uses `models/wav2vec2/`.

```
TencentGameMate/chinese-wav2vec2-base    ~380 MB   ← what MultiTalk & InfiniteTalk were trained with
```

Download the whole HF repo folder (config.json + preprocessor_config.json + model weights), not
just the `.bin`. The node loads it as a transformers directory.

Despite the name, `chinese-wav2vec2-base` handles English and most other languages fine — it is
producing phoneme-level acoustic embeddings, not doing ASR. Substituting
`facebook/wav2vec2-base-960h` "because it's the English one" produces *worse* lip sync, because
it isn't the encoder the talking-head model was trained against. Use the one the model expects.

### 6.6 Talking-head model weights

`models/diffusion_models/` alongside the base model:

```
MultiTalk / InfiniteTalk weights — from MeiGen-AI on Hugging Face
  Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors   ~5 GB
  Wan2_1-InfiniteTalk-Multi_fp8_e4m3fn_scaled_KJ.safetensors    ~5 GB
  Wan2_1-MultiTalk_fp8_e4m3fn_scaled_KJ.safetensors             ~5 GB
```

These load *in addition to* the base I2V model — budget the VRAM. On 24 GB with Q8_0 base +
InfiniteTalk you will need block swap enabled.

### 6.7 Speed LoRAs (optional, transformative)

`models/loras/`

```
Wan21_CausVid_14B_T2V_lora_rank32.safetensors        ← 4–8 step sampling
Wan21_T2V_14B_lightx2v_cfg_step_distill_lora_rank32.safetensors
```

CausVid / LightX2V are step-distillation LoRAs. With one loaded at ~0.6–1.0 strength you drop
from **30 steps @ CFG 6** to **4–8 steps @ CFG 1** — a 4–6× speedup. Cost: slightly reduced
motion range and prompt adherence. For iterating on a shot this is the difference between a
4-minute and a 45-second feedback loop. Turn it off for the final render.

Note CFG 1 disables the negative prompt entirely (there is no unconditional pass to steer away
from). See §9 — this is relevant.

### 6.8 Directory layout, assembled

```
ComfyUI/models/
├── unet/  (or diffusion_models/)
│   ├── Wan2.1-I2V-14B-480P-Q8_0.gguf
│   ├── Wan2.1-T2V-14B-Q8_0.gguf
│   └── Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors
├── text_encoders/
│   └── umt5_xxl_fp8_e4m3fn_scaled.safetensors
├── vae/
│   └── wan_2.1_vae.safetensors
├── clip_vision/
│   └── clip_vision_h.safetensors
├── wav2vec2/
│   └── chinese-wav2vec2-base/
│       ├── config.json
│       ├── preprocessor_config.json
│       └── pytorch_model.bin
└── loras/
    └── Wan21_CausVid_14B_T2V_lora_rank32.safetensors
```

---

## 7. The launcher batch file

`run_comfyui.bat` — the tuned Windows launcher. Ships alongside this README.

```bat
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
```

**Flag-by-flag, and why each is there:**

| Flag | Effect |
|---|---|
| `--use-sage-attention` | SageAttention kernels. ~20–30% faster attention, lower VRAM. Requires triton + sageattention installed, else ComfyUI falls back with a warning. |
| `--fast` | Enables fp16 accumulation and other fast paths. Free speed on Ada/Blackwell. |
| `--disable-smart-memory` | Stops ComfyUI holding models in VRAM between runs. **Counter-intuitive but essential** on a multi-model stack — smart memory is what causes OOM on the *second* run after a clean first run. |
| `--reserve-vram 0.9` | Leaves 0.9 GB for the desktop compositor. Without it, Windows steals VRAM mid-sample and you OOM at random steps. |
| `--preview-method none` | Latent previews decode every N steps and cost real VRAM. Turn them off for 14B. |
| `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` | Lets the allocator grow segments instead of fragmenting. Fixes "tried to allocate 240 MiB, 3 GiB free" — the classic fragmentation OOM. |

**Low-VRAM variants** — add exactly one:

```bat
:: 12–16 GB: offload aggressively
  --lowvram

:: 8–10 GB: keep almost nothing resident (slow)
  --novram

:: If VAE decode is what's OOM-ing, not sampling:
  --cpu-vae
```

**Do not** use `--lowvram` on a 24 GB card. It forces offloading you don't need and roughly
halves throughput. Block swap (in-workflow, WanVideoWrapper) is the finer-grained tool — it lets
you swap *N* blocks rather than making a global on/off decision.

---

## 8. Workflows

### 8.1 Image → talking video (the main one)

```
LoadImage ──────────────┐
                        ├─→ WanVideoImageToVideoEncode ─┐
CLIPVisionLoader ───────┘                               │
  (clip_vision_h)                                       │
                                                        ├─→ WanVideoSampler ─→ WanVideoDecode ─→ VideoCombine
UnetLoaderGGUF ─────────┐                               │                                          ↑
  (I2V-14B-480P-Q8_0)   ├─→ WanVideoBlockSwap ──────────┤                                          │
InfiniteTalk weights ───┘                               │                                          │
                                                        │                                          │
LoadAudio ─→ DownloadAndLoadWav2VecModel ─→ MultiTalkWav2VecEmbeds ──┘                              │
     └─────────────────────────────────────────────────────────────────────────────────────────────┘
                                                          (audio muxed into the output file)

CLIPLoader (umt5, type=wan) ─→ CLIPTextEncode ×2 (pos/neg) ─→ WanVideoSampler
VAELoader (wan_2.1_vae) ─────────────────────────────────────→ WanVideoDecode
```

Sampler settings that work:

| Parameter | Standard | With CausVid/LightX2V LoRA |
|---|---|---|
| steps | 25–30 | 4–8 |
| cfg | 5.0–6.0 | 1.0 |
| sampler | `uni_pc` or `dpm++_2m` | `uni_pc` / `lcm` |
| scheduler | `simple` / `beta` | `simple` |
| shift (ModelSamplingSD3) | 8.0 (480p) / 5.0 (720p) | 8.0 |
| frames | 81 (= 5.06 s @ 16 fps) | 81 |
| resolution | 832×480 or 480×832 | same |

**Frame count must satisfy `4n+1`** — 81, 65, 49, 33. Wan's temporal VAE compresses 4:1 and an
off-grid count either errors or silently truncates.

For InfiniteTalk long-form, the frame window is driven by audio length; the node chunks it and
carries context across chunks. Give it a clean audio file and let it decide.

### 8.2 Post

- **RIFE interpolation** 16 fps → 32 fps. Wan's native 16 fps looks stuttery; 2× RIFE is nearly
  free and transforms perceived quality.
- **Upscale**: 480p → 1080p with a 4× ESRGAN model then downscale, or run a low-denoise
  (0.2–0.3) Wan V2V pass at 720p for a genuine detail add rather than a sharpen.
- **Audio mux**: VideoHelperSuite's `VHS_VideoCombine` takes an `audio` input directly — wire the
  same `LoadAudio` output in so the render ships with its track.

---

## 9. Unrestricted generation: what is actually gating you

Straight answer, because there is a lot of folklore here.

**There is no safety filter to remove.** Wan 2.1 in ComfyUI has no NSFW classifier, no
safety checker, no CLIP-based content scorer in the pipeline. Unlike the diffusers
`StableDiffusionSafetyChecker` (a real module you could delete), the Wan pipeline never had one.
Local weights + local sampler = no runtime moderation. Nobody is inspecting your outputs and
there is no call home to disable.

What *does* constrain output, in descending order of impact:

**1. The default negative prompt — by far the biggest lever.**

Every official Wan workflow ships with this Chinese negative prompt baked in:

```
色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，
低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，
毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走
```

Read what's actually in there: it's overwhelmingly quality terms (overexposed, blurry, worst
quality, JPEG artifacts, extra fingers, malformed limbs, three legs). Many community
copy-pastes append `色情` (pornographic) / `低俗` (vulgar) / `NSFW`. **If your negative prompt
contains those tokens, you are actively steering away from the thing you asked for.** Strip them.
Keep the anatomy/quality terms — they're doing real work on hands and faces.

Also note: with a CausVid/LightX2V LoRA at **CFG 1, the negative prompt does nothing at all** —
there's no unconditional pass. If you're distilled-sampling, the negative prompt is inert and
editing it changes nothing. Diagnose in the right place.

**2. Training distribution.** Wan 2.1's base training and caption set skew toward general-purpose,
broadly-safe footage. This isn't a filter — it's a prior. The model is *less fluent* in concepts
it saw little of, the way it's less fluent at rendering text or specific hand positions. It will
attempt anything; quality tracks representation in training data.

**3. Prompt vocabulary.** UMT5 was trained on natural language captions. Booru-style tag spam
underperforms badly on Wan compared to SDXL. Write descriptive prose — camera, subject, action,
lighting, motion — in full sentences. This matters more for out-of-distribution content, where
you need the encoder to actually resolve what you mean.

**4. Community fine-tunes and LoRAs.** The real unlock, if the base prior is fighting you.
Civitai and Hugging Face host Wan 2.1 LoRAs and full fine-tunes trained on domain-specific data.
A LoRA at 0.7–1.0 strength shifts the distribution far harder than any prompt engineering.
Train your own with `diffusion-pipe` or `musubi-tuner` — Wan LoRA training is viable on 24 GB.

**5. Custom node packs that add their own checks.** A small number of third-party node packs
ship a content check of their own. They're the exception, not the rule; if you hit one it will be
obvious (a node that returns a black frame with a warning). Whether you keep it is your call —
it's your machine and your node folder.

**Practical configuration checklist:**

- [ ] Remove NSFW/pornographic/vulgar tokens from the negative prompt; keep the quality terms
- [ ] Confirm CFG > 1 if you expect the negative prompt to do anything
- [ ] Write prose prompts, not tags
- [ ] Load a domain LoRA if the base prior resists
- [ ] Keep `--disable-smart-memory` on; extra LoRAs + talking-head weights are what pushes you OOM

---

## 10. Audio: generating or supplying the track

The talking-head models need **audio in, aligned to the video you want out**. Two paths:

**Supply your own.** 16 kHz mono WAV is the safest input format. The wav2vec2 encoder resamples
internally, but feeding it a 48 kHz stereo MP3 with a noisy floor measurably degrades lip sync.
Clean it first — `ffmpeg -i in.mp3 -ac 1 -ar 16000 out.wav`, and run a denoise pass if it's a
field recording.

**Generate it.** Local TTS options that pair well:

| Tool | Notes |
|---|---|
| **F5-TTS** | Fast, high quality, voice cloning from ~10 s reference. ComfyUI nodes exist. |
| **XTTS-v2** (Coqui) | Multilingual, good cloning. Heavier. |
| **Kokoro-82M** | Tiny, extremely fast, fixed voice set, surprisingly good. |
| **Chatterbox** (Resemble) | Strong emotion control. |

All run locally and none apply content restrictions to what you type.

**For non-speech audio** (foley, ambience, music beds), **MMAudio** does video→audio: feed it the
finished silent render and it produces a synchronized track. Useful for everything the talking-head
path doesn't cover.

**Muxing.** Keep the audio out of the diffusion path and mux at the end — `VHS_VideoCombine`
with an `audio` input, or `ffmpeg -i video.mp4 -i audio.wav -c:v copy -c:a aac -shortest out.mp4`.
Re-encoding video to attach audio is a needless quality loss.

**Voice cloning is the sharp edge here.** Cloning a real person's voice and pairing it with a
video of their face is a synthetic impersonation of a specific human being. See §13.

---

## 11. Performance tuning ladder

Apply in order. Measure after each — the interactions are not additive and some cancel out.

1. **`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`** — free, fixes fragmentation OOMs.
2. **SageAttention** (`--use-sage-attention`) — 20–30% faster attention.
3. **`--disable-smart-memory`** — stability, not speed, but stops run-2 OOMs.
4. **Block swap** (`WanVideoBlockSwap`, wrapper path) — start at 10 blocks, raise until it fits.
   Each swapped block costs a little speed and saves ~400 MB VRAM. This is the precision tool for
   trading RAM for VRAM.
5. **TeaCache / MagCache** — skips redundant steps by caching residuals. 1.5–2× speedup at
   `rel_l1_thresh` 0.15–0.25. Above ~0.3 motion starts degrading visibly.
6. **CausVid / LightX2V LoRA** — 30 steps → 4–8 steps. The single biggest win. Costs some motion
   range; disable for hero shots.
7. **torch.compile** (`WanVideoTorchCompileSettings`) — 10–20% more, but adds 1–3 min of compile
   time on first run and needs a working MSVC toolchain on Windows. Worth it for batch jobs, not
   for iteration.
8. **Tiled VAE decode** — doesn't speed anything up, but it's what lets you decode 720p×81 without
   an OOM at the very last step. Enable it preemptively.

Rough throughput reference, RTX 4090, 480p × 81 frames, Q8_0:

| Config | Time |
|---|---|
| 30 steps, CFG 6, no optimizations | ~7–9 min |
| + SageAttention + expandable_segments | ~5–6 min |
| + TeaCache 0.2 | ~3 min |
| + CausVid LoRA, 6 steps, CFG 1 | ~45–70 s |

---

## 12. Troubleshooting log

Errors actually hit during this build, and what fixed them.

**`UnetLoaderGGUF` node doesn't appear**
ComfyUI-GGUF not installed, or the `gguf` Python package is stale.
`pip install --upgrade gguf` inside the venv, then restart. Check the ComfyUI console at startup
for an import traceback — a node pack that fails to import just silently doesn't register.

**`Error(s) in loading state_dict` / unexpected key shapes**
Mismatched components. Almost always: Wan 2.2 VAE with a 2.1 model, a T5 checkpoint where UMT5
belongs, or a 720P model loaded into a workflow built for 480P. Verify all five pieces
(unet / text encoder / VAE / clip vision / talking-head weights) are from the same generation.

**`KeyError: 'clip_vision_output'` or I2V ignores the input image**
`clip_vision_h.safetensors` missing from `models/clip_vision/`, or the CLIPVisionLoader isn't
wired into the encode node.

**OOM at the very last step, after sampling completed**
That's VAE decode, not sampling. Enable tiled decode (tile 256, overlap 64), or add `--cpu-vae`.
Decoding 81 frames at once is a large single allocation.

**OOM on the *second* generation after a clean first run**
Smart memory holding the previous model. `--disable-smart-memory`. This one wastes a lot of
people's time because the first run works perfectly.

**"CUDA out of memory. Tried to allocate 240.00 MiB (GPU 0; 3.21 GiB free)"**
Fragmentation, not exhaustion — note that it claims free memory exceeds the request.
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`.

**Black or green output frames**
fp16 VAE numerical overflow. Force fp32 VAE, or use `--cpu-vae`. If only *some* frames are black,
it's usually a bad frame count — confirm `4n+1`.

**SageAttention import fails / falls back with a warning**
Triton missing or MSVC toolchain absent. On Windows: install VS Build Tools with the C++ workload,
then `pip install triton-windows`, then reinstall sageattention. Verify with
`python -c "import sageattention"` before blaming ComfyUI.

**`no kernel image is available for execution on the device` (RTX 50-series)**
Torch built for an older compute capability. Reinstall with the cu128 index and torch 2.7+.

**Lip sync is out of phase or mushy**
In order of likelihood: wrong wav2vec2 model (must be the one the talking-head model was trained
with); audio not resampled to 16 kHz mono; noisy source audio; or the talking-head weights don't
match the base model variant (Single weights with a multi-person scene).

**Face identity drifts over a long clip**
Inherent to MultiTalk beyond ~10 s. Switch to InfiniteTalk, which is built specifically for
long-form with sparse-frame reference conditioning.

**Generation is inexplicably 3× slower than yesterday**
Check whether Windows shoved the model into shared GPU memory — Crystools will show it. Close the
browser, restart ComfyUI, and confirm `--reserve-vram` is set. Also check thermals; a throttling
card looks exactly like a software regression.

---

## 13. Scope, consent, and law

This repo documents a general-purpose local video rig. It is deliberately unrestricted with
respect to *content categories* — that's the point of running open weights on your own hardware,
and adult content generated for yourself is your business.

It is **not** unrestricted with respect to *people*. Two lines, and they aren't editorial
preferences — they're where this stops being a personal tool and starts being a crime in most
jurisdictions:

**Real, identifiable people.** This stack takes a photograph of a face and a recording of a voice
and produces convincing video of that person appearing to speak. Doing that to a real person
without their consent is impersonation; doing it in sexual context is non-consensual intimate
imagery, criminalized federally in the US under the **TAKE IT DOWN Act (2025)**, across the UK
(Online Safety Act 2023), the EU, and most of Asia-Pacific. The consent that matters is explicit
and specific — "it's a public figure" and "it's obviously fake" are not defenses and have not
been treated as such in any prosecution to date.

**Minors.** Sexual content depicting minors is illegal everywhere with no synthetic-content
exception. No prompt, LoRA, or configuration in this repo is intended to produce it, and if you
find that a fine-tune you downloaded pushes in that direction, delete it — several circulating
community LoRAs have been found to be trained on scraped material that shouldn't exist.

**Also worth your attention:** synthetic media used to defraud, harass, or influence elections
carries separate liability; some jurisdictions (California, Texas, China, EU AI Act Art. 50)
require synthetic media to be labeled. If you publish output, label it.

Practical suggestions for anyone running this: use your own likeness or licensed/synthetic
faces for testing, get written consent for any real person, keep generated material out of
shared or cloud-synced folders, and don't publish anything that could be mistaken for a genuine
recording of a real person without a clear synthetic-content label.

The rig doesn't enforce any of this. You do.

---

## 14. Appendix: session notes

Space for the debugging history — paste raw session logs, dead ends, and hardware-specific
findings here as you accumulate them. Suggested structure:

```markdown
### YYYY-MM-DD — <what broke>
**Symptom:**
**Hypothesis:**
**Actually was:**
**Fix:**
**Config at time of failure:** GPU / driver / torch / ComfyUI commit / node pack versions
```

Recording the **ComfyUI commit hash and custom-node versions** alongside each finding is what
makes this appendix useful six months from now. This ecosystem breaks on update constantly, and
"it worked before I hit Update All" is only a debuggable statement if you know what "before" was.

Consider pinning a known-good state:

```bat
cd ComfyUI
git rev-parse HEAD > ..\known-good-comfyui-commit.txt
pip freeze > ..\known-good-requirements.txt
```

---

## Credits

- **Wan 2.1** — Alibaba / Wan-AI team, Apache 2.0
- **MultiTalk / InfiniteTalk** — MeiGen-AI
- **ComfyUI** — comfyanonymous and contributors
- **ComfyUI-GGUF** — city96
- **WanVideoWrapper, KJNodes** — kijai
- **GGUF quantizations** — QuantStack, city96
- **SageAttention** — thu-ml

Check each project's license before redistributing weights or output commercially. Wan 2.1 is
Apache 2.0; several community fine-tunes are not.
