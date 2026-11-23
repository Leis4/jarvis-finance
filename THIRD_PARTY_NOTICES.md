# Third-party notices

This repository contains original application code. It does not vendor third-party
source code. Runtime dependencies are installed from their upstream packages and
keep their own licenses.

Main dependencies (check the installed package metadata for the exact version and
license text):

| Package | License |
|---|---|
| FastAPI / Starlette / Pydantic | MIT / BSD-3-Clause |
| SQLModel | MIT |
| pandas | BSD-3-Clause |
| NumPy | BSD-3-Clause |
| DuckDB | MIT |
| scikit-learn | BSD-3-Clause |
| CatBoost | Apache-2.0 |
| PyTorch / torchvision / torchaudio | BSD-3-Clause |
| sentence-transformers | Apache-2.0 |
| ChromaDB | Apache-2.0 |
| SetFit | Apache-2.0 |
| pypdf | BSD-3-Clause |
| pytest / ruff / mypy | MIT |

The MIT license in `LICENSE` applies to the original code in this repository only.
It does not relicense third-party dependencies. If you redistribute a bundled
build, keep the upstream license and notice files of every dependency you ship.
