import importlib.metadata

__version__: str = importlib.metadata.version("sift")

import os as _os

# Sift is local only: ONNX Runtime's telemetry (an uploader and a device ID) is switched off before
# it can start. It must be set before onnxruntime is first imported, so it lives here.
_os.environ["ORT_DISABLE_TELEMETRY"] = "1"
