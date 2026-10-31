"""Synthetic public fixture; run only inside the approved Docker environment."""
from pathlib import Path
import json
import hashlib
import pickle
import sys

import numpy as np
import within._within as extension
from within import PreconditionerConfig, Solver

categories = np.array([[0, 0], [0, 1], [0, 2], [1, 0], [1, 1], [1, 2]], dtype=np.uint32)
y = np.array([1.0, -2.0, 0.5, 3.0, -1.5, 2.5])
solver = Solver(categories, preconditioner=PreconditionerConfig.Additive())
original = solver.preconditioner
vector = np.arange(original.ncols, dtype=np.float64) + 0.25
expected_apply = original.apply(vector)
expected_solve = solver.solve(y)
assert expected_solve.converged
restored = pickle.loads(pickle.dumps(original))
np.testing.assert_array_equal(restored.apply(vector), expected_apply)
reused = Solver(categories, preconditioner=restored).solve(y)
assert reused.converged
np.testing.assert_allclose(reused.x, expected_solve.x, rtol=0, atol=1e-9)
directory = Path(sys.argv[2])
if sys.argv[1] == "baseline":
    target = directory / "uniform-base.pickle"
    assert not target.exists()
    target.write_bytes(pickle.dumps(original))
elif sys.argv[1] == "candidate":
    try:
        old = pickle.loads((directory / "uniform-base.pickle").read_bytes())
    except Exception as error:
        # The actual bytes constructor maps postcard decode errors to ValueError.
        # Import/ABI/harness exceptions must not be mistaken for safe rejection.
        assert isinstance(error, ValueError) and str(error).startswith("failed to deserialize preconditioner:"), repr(error)
        print("OLD_PICKLE_OUTCOME=REJECTED type=" + type(error).__name__, flush=True)
    else:
        assert old.nrows == original.nrows and old.ncols == original.ncols
        assert old.config == original.config
        np.testing.assert_array_equal(old.apply(vector), expected_apply)
        result = Solver(categories, preconditioner=old).solve(y)
        assert result.converged
        np.testing.assert_allclose(result.x, expected_solve.x, rtol=0, atol=1e-9)
        print("OLD_PICKLE_OUTCOME=COMPATIBLE", flush=True)
else:
    raise AssertionError("unknown fixture phase")
print(json.dumps({"pickle_roundtrip": "PASS", "solver_reuse": "PASS", "phase": sys.argv[1]}))

extension_path = Path(extension.__file__).resolve()
print(json.dumps({"extension_file": str(extension_path), "extension_sha256": hashlib.sha256(extension_path.read_bytes()).hexdigest(), "python_prefix": sys.prefix}), flush=True)
