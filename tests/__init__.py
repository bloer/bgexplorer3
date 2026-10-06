import importlib.util
from pathlib import Path

# examples live at the top level of the repo and are not installed
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / 'examples'


def load_example(name):
    """ Import examples/<name>/<name>.py as a module """
    path = EXAMPLES_DIR / name / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f'examples.{name}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
