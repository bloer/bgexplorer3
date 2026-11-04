""" The versioning package depends on nothing else in bgexplorer """
import ast
import pathlib
import sys
import unittest

PACKAGE = pathlib.Path(__file__).parent.parent / 'src/bgexplorer/versioning'
ALLOWED_EXTERNAL = {'mongoengine', 'bson', 'pymongo', 'blinker'}

# modules outside the package that it still imports, to be emptied
LEAKS = {
    'bgexplorer.models', 'bgexplorer.models.component',
    'bgexplorer.models.cosmogenic', 'bgexplorer.models.emissionspec',
    'bgexplorer.models.hiteff', 'bgexplorer.models.settings',
    'bgexplorer.models.sourceterm'}


def imported_modules(path: pathlib.Path):
    """ (absolute module name, line) of every import in the file """
    module = ['bgexplorer', 'versioning', path.stem]
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module[:len(module) - node.level]
                name = '.'.join(base + ([node.module] if node.module else []))
            else:
                name = node.module
            yield name, node.lineno
            if node.module is None:
                # `from . import x` imports the module x
                for alias in node.names:
                    yield f'{name}.{alias.name}', node.lineno


def outside_imports():
    found = {}
    for path in sorted(PACKAGE.glob('*.py')):
        for name, line in imported_modules(path):
            top = name.split('.')[0]
            if name.startswith('bgexplorer.versioning'):
                continue
            if top in sys.stdlib_module_names or top in ALLOWED_EXTERNAL:
                continue
            found.setdefault(name, []).append(f'{path.name}:{line}')
    return found


class TestIsolation(unittest.TestCase):
    def test_no_unexpected_imports(self):
        found = outside_imports()
        unexpected = {k: v for k, v in found.items() if k not in LEAKS}
        self.assertEqual(unexpected, {})

    def test_leaks_are_current(self):
        # a leak that was fixed must be removed from the list
        self.assertEqual(sorted(LEAKS - set(outside_imports())), [])
