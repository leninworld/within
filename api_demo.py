"""Public synthetic API and positive persistence witness; Docker only."""
from pathlib import Path
import hashlib
import json
import pickle
import sys

import numpy as np
import within._within as extension
from within import PreconditionerConfig, Solver
from within.config import ApproxSchurConfig, LocalSolverConfig, Schur

for split in (1, 2, 2**32 - 1):
    config = ApproxSchurConfig(seed=17, split=split)
    assert config.seed == 17 and config.split == split
    assert repr(Schur.approximate(config)) == f"Schur.approximate(seed=17, split={split})"
    try:
        config.split = 0
    except AttributeError:
        pass
    else:
        raise AssertionError("split must remain read-only")
try:
    ApproxSchurConfig(split=0)
except ValueError as error:
    assert str(error) == "split must be >= 1"
else:
    raise AssertionError("Python must reject zero")
assert ApproxSchurConfig().split == 1
categories = np.asfortranarray([[0, 0], [0, 1], [0, 2], [1, 0], [1, 1], [1, 2]], dtype=np.uint32)
y = np.array([1.0, -2.0, 0.5, 3.0, -1.5, 2.5])
requested = PreconditionerConfig.Additive(local_solver=LocalSolverConfig(
    schur=Schur.approximate(ApproxSchurConfig(seed=17, split=2)), dense_threshold=0))
solver = Solver(categories, preconditioner=requested)
original = solver.preconditioner
assert original is not None and original.config == requested
vector = np.arange(original.ncols, dtype=np.float64) + .25
expected_apply = original.apply(vector)
expected_solve = solver.solve(y)
assert expected_solve.converged
restored = pickle.loads(pickle.dumps(original))
assert restored.config == requested
np.testing.assert_array_equal(restored.apply(vector), expected_apply)
reused = Solver(categories, preconditioner=restored).solve(y)
assert reused.converged
np.testing.assert_allclose(reused.x, expected_solve.x, rtol=0, atol=1e-9)
target = Path(sys.argv[2]) / 'positive-split-base.pickle'
if sys.argv[1] == 'baseline':
    assert not target.exists()
    target.write_bytes(pickle.dumps(original))
elif sys.argv[1] == 'candidate':
    old = pickle.loads(target.read_bytes())
    assert old.config == requested
    np.testing.assert_array_equal(old.apply(vector), expected_apply)
    result = Solver(categories, preconditioner=old).solve(y)
    assert result.converged
    np.testing.assert_allclose(result.x, expected_solve.x, rtol=0, atol=1e-9)
else:
    raise AssertionError('unknown phase')
path = Path(extension.__file__).resolve()
print(json.dumps({'phase':sys.argv[1], 'zero_rejected':True, 'positive_getters':[1,2,2**32-1],
    'read_only':True,'tuned_schur':repr(requested.local_solver.schur),
    'pickle_and_solver_reuse':'PASS', 'old_positive_pickle':'CREATED' if sys.argv[1]=='baseline' else 'COMPATIBLE',
    'extension_file':str(path),'extension_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
    'python_prefix':sys.prefix}),flush=True)
