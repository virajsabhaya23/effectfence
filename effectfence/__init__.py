import warnings
warnings.warn("effectfence is deprecated, use safesink (pip install safesink)", DeprecationWarning, stacklevel=2)
from .mcp_verifier import ManifestError, load_manifest, verify_manifest

__all__ = ["ManifestError", "load_manifest", "verify_manifest", "__version__"]
__version__ = "0.3.0"
