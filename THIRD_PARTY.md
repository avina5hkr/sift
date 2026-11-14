# Third-party software

Sift.app bundles the software below, each under its own licence (from each package's metadata; the full texts are in each project's source repository). Sift is built for personal use; because pillow-heif's wheel includes the GPL-2.0 x265 encoder, a redistributed build is covered by GPL-2.0.

## Downloaded on first use (with your consent)

| Item | Source | Terms |
| --- | --- | --- |
| Android SDK Platform-Tools (adb) | dl.google.com | Android Software Development Kit License |
| CLIP ViT-B/32 ONNX model and tokenizer | pinned URLs in the app | MIT (OpenAI CLIP) |

## Runtime and native libraries

| Library | Version | Licence |
| --- | --- | --- |
| CPython | 3.14 | PSF-2.0 |
| PyInstaller bootloader | 6.22.3 | GPL-2.0 with the bootloader exception |
| ONNX Runtime native library | via onnxruntime | MIT |
| libheif, libde265 | via pillow-heif | LGPL-3.0 |
| x265 | via pillow-heif | GPL-2.0 |
| libjpeg-turbo, libpng, libtiff, libwebp, zlib, freetype | via Pillow | BSD-style, zlib, FreeType (permissive) |
| Hugging Face tokenizers (Rust) | via tokenizers | Apache-2.0 |

## Python packages

| Package | Version | Licence |
| --- | --- | --- |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.16.0 | MIT |
| bottle | 0.13.4 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| fastapi | 0.162.0 | MIT |
| filelock | 4.2.10 | MIT |
| flatbuffers | 25.12.19 | Apache 2.0 |
| fsspec | 2026.9.0 | BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| hf-xet | 1.8.0 | Apache-2.0 |
| httpcore2 | 2.16.0 | BSD-3-Clause |
| httpx2 | 2.16.0 | BSD-3-Clause |
| huggingface-hub | 2.4.0 | Apache-2.0 |
| idna | 3.20 | BSD-3-Clause |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| onnxruntime | 1.32.0 | MIT License |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pillow | 13.0.0 | MIT-CMU |
| pillow-heif | 1.8.1 | BSD-3-Clause; its macOS wheel bundles libheif and libde265 (LGPL-3.0) and the x265 encoder (GPL-2.0), so a build containing it is GPL-2.0 as a whole |
| protobuf | 7.36.2 | 3-Clause BSD License |
| proxy-tools | 0.1.0 | MIT |
| pydantic | 2.14.3 | MIT |
| pydantic-core | 2.50.4 | MIT |
| pyobjc-core | 13.0 | MIT |
| pyobjc-framework-avfoundation | 13.0 | MIT |
| pyobjc-framework-cocoa | 13.0 | MIT |
| pyobjc-framework-coreaudio | 13.0 | MIT |
| pyobjc-framework-coremedia | 13.0 | MIT |
| pyobjc-framework-coreml | 13.0 | MIT |
| pyobjc-framework-quartz | 13.0 | MIT |
| pyobjc-framework-security | 13.0 | MIT |
| pyobjc-framework-uniformtypeidentifiers | 13.0 | MIT |
| pyobjc-framework-usernotifications | 13.0 | MIT |
| pyobjc-framework-vision | 13.0 | MIT |
| pyobjc-framework-webkit | 13.0 | MIT |
| pywebview | 6.2.1 | BSD License |
| pyyaml | 6.0.3 | MIT |
| starlette | 1.10.0 | BSD-3-Clause |
| tokenizers | 0.23.3 | Apache Software License |
| tomlkit | 0.15.2 | MIT |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| truststore | 0.10.5 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.5 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |

## Web packages

| Package | Version | Licence |
| --- | --- | --- |
| @tanstack/query-core | 5.106.1 | MIT |
| @tanstack/react-query | 5.106.1 | MIT |
| cookie | 1.1.1 | MIT |
| js-tokens | 4.0.0 | MIT |
| loose-envify | 1.4.0 | MIT |
| react | 18.3.1 | MIT |
| react-dom | 18.3.1 | MIT |
| react-router | 7.18.5 | MIT |
| scheduler | 0.23.2 | MIT |
| set-cookie-parser | 2.7.3 | MIT |
| zustand | 5.0.16 | MIT |
