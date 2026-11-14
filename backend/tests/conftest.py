import os
import tempfile

# Configurar ANTES de importar la app: modo mock y caché temporal.
os.environ["SPHEREX_MODE"] = "mock"
os.environ["CACHE_DIR"] = tempfile.mkdtemp(prefix="skyapi-test-")
os.environ["RATE_LIMIT_PER_MIN"] = "0"
